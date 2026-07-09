from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from locales import en, my
from services.free_users import is_free_user
from utils.ttl_cache import TTLCache

log = logging.getLogger(__name__)
router = Router(name="force_join")

_join_cache: TTLCache[str, bool] = TTLCache(50000, settings.force_join_positive_cache_seconds)
_prompt_cache: TTLCache[str, bool] = TTLCache(100000, settings.force_join_prompt_throttle_seconds)


def _join_key(user_id: int) -> str:
    return f"forcejoin:{user_id}:{'|'.join(settings.force_join_channels)}"


def _prompt_key(message: Message) -> str:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    return f"forcejoin_prompt:{chat_id}:{user_id}"


def _channel_button(channel: str) -> InlineKeyboardButton:
    if channel.startswith("@"):
        return InlineKeyboardButton(text=f"📢 {channel}", url=f"https://t.me/{channel.lstrip('@')}")
    return InlineKeyboardButton(text="📢 Join Channel", url=str(channel))


async def bot_username(bot: Bot) -> str:
    if settings.bot_username:
        return settings.bot_username
    me = await bot.get_me()
    return me.username or ""


async def _check_all_channels(bot: Bot, user_id: int) -> bool:
    for channel in settings.force_join_channels:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                return False
        except Exception:
            log.warning("force join live check failed for %s", channel, exc_info=True)
            return False
    return True


async def has_joined(bot: Bot, user_id: int, *, force_refresh: bool = False) -> bool:
    if not settings.enable_force_join or not settings.force_join_channels:
        return True
    key = _join_key(user_id)
    if not force_refresh and _join_cache.get(key) is True:
        return True
    if await is_free_user(user_id):
        return True
    joined = await _check_all_channels(bot, user_id)
    if joined:
        _join_cache.set(key, True)
    return joined


def dm_force_join_keyboard() -> InlineKeyboardMarkup:
    rows = [[_channel_button(channel)] for channel in settings.force_join_channels]
    if settings.support_group_username:
        rows.append([InlineKeyboardButton(text="👥 Support Group", url=f"https://t.me/{settings.support_group_username.lstrip('@')}")])
    rows.append([InlineKeyboardButton(text="✅ Joined / Check Again", callback_data="force_join_check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def group_dm_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    username = await bot_username(bot)
    link = f"https://t.me/{username}?start={settings.force_join_dm_start_param}" if username else "https://t.me/"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Open Bot DM", url=link)]])


def dm_force_join_text() -> str:
    return f"{my.FORCE_JOIN_TEXT}\n\n{en.FORCE_JOIN_TEXT}"


def group_force_join_text() -> str:
    return f"{my.GROUP_FORCE_JOIN_TEXT}\n{en.GROUP_FORCE_JOIN_TEXT}"


async def _safe_send_force_join(message: Message) -> None:
    key = _prompt_key(message)
    if _prompt_cache.get(key):
        return
    _prompt_cache.set(key, True)
    try:
        if message.chat.type == "private":
            await message.answer(dm_force_join_text(), reply_markup=dm_force_join_keyboard())
        else:
            await message.reply(group_force_join_text(), reply_markup=await group_dm_keyboard(message.bot))
    except TelegramRetryAfter as exc:
        await asyncio.sleep(min(int(getattr(exc, "retry_after", 5) or 5), 10))
    except (TelegramForbiddenError, TelegramBadRequest):
        log.warning("force join prompt failed", exc_info=True)
    except Exception:
        log.exception("force join prompt failed")


async def require_join(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    if await has_joined(message.bot, user_id):
        return True
    await _safe_send_force_join(message)
    return False


@router.callback_query(F.data == "force_join_check")
async def force_join_check_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    if not user_id:
        await callback.answer("User not found", show_alert=True)
        return
    if await has_joined(callback.bot, user_id, force_refresh=True):
        await callback.answer("✅ Verified")
        if callback.message:
            try:
                await callback.message.edit_text(f"{my.JOIN_OK}\n{en.JOIN_OK}")
            except Exception:
                pass
        return
    await callback.answer("မ join ရသေးပါ။ Channel ကို join ပြီးမှ ပြန်နှိပ်ပါ။", show_alert=True)

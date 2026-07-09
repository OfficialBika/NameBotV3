from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from locales import en, my
from services.force_join import dm_force_join_keyboard, dm_force_join_text, has_joined
from services.group_access import remember_user

router = Router(name="start")


@router.message(CommandStart())
async def start_cmd(message: Message) -> None:
    if message.from_user:
        await remember_user(message.from_user.id, message.from_user.username)
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].strip().lower() == "forcejoin":
        await message.answer(dm_force_join_text(), reply_markup=dm_force_join_keyboard())
        return
    await message.answer(f"{my.START_TEXT}\n\n{en.START_TEXT}")


@router.callback_query(F.data == "force_join_check")
async def force_join_check(call: CallbackQuery) -> None:
    if await has_joined(call.bot, call.from_user.id, force_refresh=True):
        await call.message.edit_text(f"{my.JOIN_OK}\n{en.JOIN_OK}")
    else:
        await call.answer("Please join first", show_alert=True)
        await call.message.answer(f"{my.JOIN_FAIL}\n{en.JOIN_FAIL}", reply_markup=dm_force_join_keyboard())

from __future__ import annotations

import re
import time

from aiogram import F, Router
from aiogram.types import Message

from config import settings
from locales import en, my
from services.force_join import require_join
from services.lookup_service import lookup_service
from services.result_formatter import format_result, result_buttons
from services.source_blocker import blocked_source_text, is_blocked_source
from utils.telegram_safe import safe_reply

router = Router(name="manual_lookup")
MANUAL_RE = re.compile(r"^(?P<cmd>/waifu|/w|\.wa|\.w|/name|\.name|/loot|/bika|/pick|/ziceko|/orin|/dao)(?:@(?P<bot>[A-Za-z0-9_]+))?(?:\s|$)", re.I)
_seen: dict[str, float] = {}


def _already_processed(message: Message) -> bool:
    now = time.monotonic()
    for key, ts in list(_seen.items()):
        if now - ts > 20:
            _seen.pop(key, None)
    key = f"{message.chat.id}:{message.message_id}"
    if key in _seen:
        return True
    _seen[key] = now
    return False


async def _targets_this_bot(message: Message) -> bool:
    match = MANUAL_RE.match(message.text or "")
    if not match:
        return False
    mention = match.group("bot")
    if not mention:
        return True
    configured = settings.bot_username.lower().lstrip("@")
    if configured:
        return mention.lower() == configured
    me = await message.bot.get_me()
    return mention.lower() == (me.username or "").lower().lstrip("@")


@router.message(F.text.regexp(MANUAL_RE))
async def manual_lookup(message: Message) -> None:
    if not await _targets_this_bot(message) or _already_processed(message):
        return
    target = message.reply_to_message or message
    if is_blocked_source(target):
        await safe_reply(message, blocked_source_text())
        return
    if not await require_join(message):
        return
    result = await lookup_service.lookup_message(message.bot, message, manual=True)
    if result.reason == "blocked_source":
        await safe_reply(message, blocked_source_text())
    elif result.reason == "no_media":
        if message.chat.type == "private":
            await safe_reply(message, f"{my.NO_MEDIA}\n{en.NO_MEDIA}")
    elif not result.item:
        await safe_reply(message, f"{my.NOT_FOUND}\n{en.NOT_FOUND}")
    else:
        await safe_reply(message, format_result(result.item), reply_markup=result_buttons(result.item), disable_web_page_preview=True)

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import F, Router
from aiogram.types import Message

from config import settings
from locales import en, my
from services.force_join import require_join
from services.group_access import can_auto_lookup, remember_user
from services.lookup_service import lookup_service
from services.result_formatter import format_result, result_buttons
from services.source_blocker import blocked_source_text, is_blocked_source
from utils.telegram_safe import safe_reply

router = Router(name="auto_lookup")
log = logging.getLogger(__name__)
_seen: dict[str, float] = {}


def _cleanup(now: float) -> None:
    ttl = settings.auto_lookup_dedupe_ttl_seconds
    for key, ts in list(_seen.items()):
        if now - ts > ttl:
            _seen.pop(key, None)


def _key(message: Message) -> str:
    chat_id = int(getattr(message.chat, "id", 0) or 0)
    sender_id = int(message.from_user.id) if message.from_user else int(getattr(getattr(message, "sender_chat", None), "id", 0) or 0)
    group_id = getattr(message, "media_group_id", None)
    if settings.auto_lookup_dedupe_album and group_id:
        return f"{chat_id}:{sender_id}:album:{group_id}"
    return f"{chat_id}:{sender_id}:msg:{message.message_id}"


def _already_processed(message: Message) -> bool:
    now = time.monotonic()
    _cleanup(now)
    key = _key(message)
    if key in _seen:
        return True
    _seen[key] = now
    return False


def _supported_document(message: Message) -> bool:
    document = getattr(message, "document", None)
    if not document:
        return False
    mime = str(getattr(document, "mime_type", "") or "").lower()
    name = str(getattr(document, "file_name", "") or "").lower()
    return mime.startswith(("image/", "video/")) or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mkv", ".mov", ".webm"))


def _media_filter(message: Message) -> bool:
    return bool(getattr(message, "photo", None) or getattr(message, "video", None) or getattr(message, "animation", None) or _supported_document(message))


@router.message(F.func(_media_filter))
async def auto_lookup(message: Message) -> None:
    started = time.perf_counter()
    if message.from_user:
        asyncio.create_task(remember_user(message.from_user.id, message.from_user.username))
    if not await can_auto_lookup(message):
        return
    if is_blocked_source(message):
        if not _already_processed(message):
            await safe_reply(message, blocked_source_text())
        return
    if not await require_join(message):
        return
    if _already_processed(message):
        return
    try:
        result = await lookup_service.lookup_message(message.bot, message, manual=False)
    except Exception:
        log.exception("auto lookup failed")
        if settings.auto_lookup_reply_not_found:
            await safe_reply(message, f"{my.NOT_FOUND}\n{en.NOT_FOUND}")
        return
    if result.item:
        await safe_reply(message, format_result(result.item), reply_markup=result_buttons(result.item), disable_web_page_preview=True)
    elif result.reason == "blocked_source":
        await safe_reply(message, blocked_source_text())
    elif settings.auto_lookup_reply_not_found and result.reason != "no_media":
        await safe_reply(message, f"{my.NOT_FOUND}\n{en.NOT_FOUND}")
    log.debug("lookup reason=%s confidence=%.3f elapsed=%.1fms", result.reason, result.confidence, (time.perf_counter() - started) * 1000)

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from config import settings

log = logging.getLogger(__name__)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "-"))


def _user_label(message: Message) -> str:
    user = message.from_user
    if not user:
        return _esc(getattr(getattr(message, "sender_chat", None), "title", None) or "unknown")
    username = getattr(user, "username", None)
    name = " ".join(
        part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        if part
    ).strip()
    if username:
        return f"@{_esc(username)}"
    return _esc(name or user.id)


def _forward_source(message: Message) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender_user = getattr(origin, "sender_user", None)
        sender_chat = getattr(origin, "sender_chat", None)
        if sender_user is not None:
            username = getattr(sender_user, "username", None)
            return f"@{_esc(username)}" if username else _esc(getattr(sender_user, "id", "-"))
        if sender_chat is not None:
            username = getattr(sender_chat, "username", None)
            title = getattr(sender_chat, "title", None)
            if username:
                return f"@{_esc(username)}"
            return _esc(title or getattr(sender_chat, "id", "-"))

    old_user = getattr(message, "forward_from", None)
    old_chat = getattr(message, "forward_from_chat", None)
    if old_user is not None:
        username = getattr(old_user, "username", None)
        return f"@{_esc(username)}" if username else _esc(getattr(old_user, "id", "-"))
    if old_chat is not None:
        username = getattr(old_chat, "username", None)
        return f"@{_esc(username)}" if username else _esc(getattr(old_chat, "title", None) or getattr(old_chat, "id", "-"))
    return "-"


def _media_info(message: Message) -> tuple[str, str]:
    if getattr(message, "photo", None):
        media = message.photo[-1]
        return "photo", str(getattr(media, "file_unique_id", "") or "-")
    if getattr(message, "video", None):
        media = message.video
        return "video", str(getattr(media, "file_unique_id", "") or "-")
    if getattr(message, "animation", None):
        media = message.animation
        return "animation/video", str(getattr(media, "file_unique_id", "") or "-")
    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "").lower()
        media_type = "video/document" if mime.startswith("video/") else "photo/document" if mime.startswith("image/") else "document"
        return media_type, str(getattr(document, "file_unique_id", "") or "-")
    return "unknown", "-"


async def send_lookup_miss(
    bot: Bot,
    message: Message,
    *,
    reason: str,
    confidence: float = 0.0,
    elapsed_ms: float = 0.0,
    error: str | None = None,
) -> None:
    chat_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
    user_id = int(getattr(getattr(message, "from_user", None), "id", 0) or 0)
    media_type, file_uid = _media_info(message)

    lines = [
        "🔎 <b>NAMEBOT V3 — LOOKUP MISS</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>Reason:</b> <code>{_esc(reason)}</code>",
        f"<b>Confidence:</b> <code>{confidence:.3f}</code>",
        f"<b>Elapsed:</b> <code>{elapsed_ms:.1f} ms</code>",
        f"<b>Media:</b> <code>{_esc(media_type)}</code>",
        f"<b>File UID:</b> <code>{_esc(file_uid)}</code>",
        f"<b>Chat ID:</b> <code>{chat_id}</code>",
        f"<b>Message ID:</b> <code>{_esc(getattr(message, 'message_id', '-'))}</code>",
        f"<b>User:</b> {_user_label(message)}",
        f"<b>User ID:</b> <code>{user_id or '-'}</code>",
        f"<b>Forward Source:</b> {_forward_source(message)}",
    ]

    if error:
        lines.append(f"<b>Error:</b> <code>{_esc(error)[:1000]}</code>")

    log_chat_id = settings.lookup_log_group_id
    if not log_chat_id:
        return

    try:
        await bot.send_message(
            chat_id=log_chat_id,
            text="\n".join(lines),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("failed to send lookup diagnostic log to group %s", log_chat_id)

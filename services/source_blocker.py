from __future__ import annotations

import re
import unicodedata

from aiogram.types import Message

from config import settings
from locales import en, my


def _clean(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower().strip().replace("_", " ")
    value = re.sub(r"[^0-9a-z\u1000-\u109f\s]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _norm_username(value: str | None) -> str:
    value = (value or "").strip().lower().lstrip("@")
    return "@" + value if value else ""


def _source_origin_chat(message: Message | None):
    if message is None:
        return None
    origin = getattr(message, "forward_origin", None)
    if origin:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat:
            return chat
    return getattr(message, "forward_from_chat", None) or getattr(message, "sender_chat", None)


def _source_user_candidates(message: Message | None) -> list:
    if message is None:
        return []
    users = []
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "sender_user", None):
        users.append(origin.sender_user)
    for attr in ("from_user", "forward_from", "via_bot"):
        obj = getattr(message, attr, None)
        if obj is not None:
            users.append(obj)
    return users


def is_blocked_source(message: Message | None) -> bool:
    if message is None:
        return False
    blocked_ids = set(settings.blocked_source_user_ids or set())
    blocked_usernames = set(settings.blocked_source_usernames or set())
    blocked_titles = [_clean(x) for x in settings.blocked_source_titles if _clean(x)]
    chat = _source_origin_chat(message)
    if chat is not None:
        try:
            if int(getattr(chat, "id", 0) or 0) in blocked_ids:
                return True
        except Exception:
            pass
        if _norm_username(getattr(chat, "username", None)) in blocked_usernames:
            return True
        if _clean(getattr(chat, "title", None)) in blocked_titles:
            return True
    for user in _source_user_candidates(message):
        try:
            if int(getattr(user, "id", 0) or 0) in blocked_ids:
                return True
        except Exception:
            pass
        if _norm_username(getattr(user, "username", None)) in blocked_usernames:
            return True
        name = getattr(user, "full_name", None) or " ".join(
            x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x
        )
        if _clean(name) in blocked_titles:
            return True
    origin = getattr(message, "forward_origin", None)
    hidden = _clean(getattr(origin, "sender_user_name", None) if origin else None)
    return bool(hidden and hidden in blocked_titles)


def blocked_source_text() -> str:
    return f"{my.BLOCKED_SOURCE}\n\n{en.BLOCKED_SOURCE}"

from __future__ import annotations

import time

from aiogram.types import Message

from config import settings
from database.mongo import get_db
from utils.ttl_cache import TTLCache

_group_cache: TTLCache[int, bool] = TTLCache(10000, settings.gapprove_cache_seconds)
_user_touch_cache: TTLCache[int, bool] = TTLCache(100000, 600)
_group_touch_cache: TTLCache[int, bool] = TTLCache(50000, 600)


def is_owner_or_sudo(user_id: int | None) -> bool:
    return bool(user_id and (user_id in settings.owner_ids or user_id in settings.sudo_ids))


async def remember_user(user_id: int | None, username: str | None = None) -> None:
    if not user_id or _user_touch_cache.get(int(user_id)):
        return
    _user_touch_cache.set(int(user_id), True)
    db = get_db()
    await db["known_users"].update_one(
        {"user_id": user_id},
        {
            "$set": {"user_id": user_id, "username": username or "", "updated_at": time.time()},
            "$setOnInsert": {"created_at": time.time()},
        },
        upsert=True,
    )


async def remember_group_from_message(message: Message | None) -> None:
    if not message or not getattr(message, "chat", None):
        return
    chat = message.chat
    chat_type = str(getattr(chat, "type", "") or "")
    if chat_type == "private":
        return
    chat_id = int(getattr(chat, "id", 0) or 0)
    if not chat_id or _group_touch_cache.get(chat_id):
        return
    _group_touch_cache.set(chat_id, True)
    db = get_db()
    await db["known_groups"].update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "chat_id": chat_id,
                "title": getattr(chat, "title", "") or "",
                "username": getattr(chat, "username", "") or "",
                "type": chat_type,
                "updated_at": time.time(),
            },
            "$setOnInsert": {"created_at": time.time()},
        },
        upsert=True,
    )


async def is_approved_group(chat_id: int) -> bool:
    if not settings.enable_gapprove:
        return True
    if settings.support_group_id and chat_id == settings.support_group_id:
        return True
    cached = _group_cache.get(chat_id)
    if cached is not None:
        return cached
    db = get_db()
    doc = await db["settings"].find_one({"key": f"gapprove:{chat_id}"})
    value = bool(doc and doc.get("enabled", True))
    _group_cache.set(chat_id, value)
    return value


async def set_group_approved(chat_id: int, enabled: bool = True, message: Message | None = None) -> None:
    db = get_db()
    if message is not None:
        _group_touch_cache.delete(chat_id)
        await remember_group_from_message(message)
    await db["settings"].update_one(
        {"key": f"gapprove:{chat_id}"},
        {"$set": {"enabled": enabled, "updated_at": time.time()}},
        upsert=True,
    )
    _group_cache.set(chat_id, enabled)


async def can_auto_lookup(message: Message) -> bool:
    await remember_group_from_message(message)
    if not settings.auto_lookup_enabled:
        return False
    if message.chat.type == "private":
        return settings.auto_lookup_in_dm
    if settings.support_group_id and message.chat.id == settings.support_group_id and settings.auto_lookup_in_support_group:
        return True
    if settings.auto_lookup_only_approved_groups:
        return await is_approved_group(message.chat.id)
    return True

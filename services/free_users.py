from __future__ import annotations

from datetime import datetime, timezone

from database.mongo import get_db
from utils.ttl_cache import TTLCache

COLLECTION_NAME = "free_users"
_cache: TTLCache[int, bool] = TTLCache(100000, 300)


async def is_free_user(user_id: int) -> bool:
    if not user_id:
        return False
    cached = _cache.get(int(user_id))
    if cached is not None:
        return cached
    db = get_db()
    row = await db[COLLECTION_NAME].find_one({"user_id": int(user_id)}, {"_id": 1})
    value = bool(row)
    _cache.set(int(user_id), value)
    return value


async def add_free_user(user_id: int, added_by: int, reason: str = "") -> None:
    db = get_db()
    await db[COLLECTION_NAME].update_one(
        {"user_id": int(user_id)},
        {
            "$set": {
                "user_id": int(user_id),
                "added_by": int(added_by),
                "reason": reason,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )
    _cache.set(int(user_id), True)


async def remove_free_user(user_id: int) -> bool:
    db = get_db()
    result = await db[COLLECTION_NAME].delete_one({"user_id": int(user_id)})
    _cache.set(int(user_id), False)
    return result.deleted_count > 0

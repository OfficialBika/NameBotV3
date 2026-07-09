from __future__ import annotations

import asyncio
import os
import re
import time

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "waifu_adding_v2").strip()


async def main() -> None:
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is required")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    inserted = 0
    async for doc in db["settings"].find({"key": {"$regex": r"^gapprove:"}, "enabled": True}, {"key": 1}):
        match = re.match(r"^gapprove:(-?\d+)$", str(doc.get("key") or ""))
        if not match:
            continue
        chat_id = int(match.group(1))
        result = await db["known_groups"].update_one(
            {"chat_id": chat_id},
            {
                "$set": {"chat_id": chat_id, "updated_at": time.time(), "source": "gapprove_backfill"},
                "$setOnInsert": {"created_at": time.time(), "title": "", "username": "", "type": "group"},
            },
            upsert=True,
        )
        if result.upserted_id:
            inserted += 1
    print(f"Backfill complete. Inserted: {inserted}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())

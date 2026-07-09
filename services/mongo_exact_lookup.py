from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from config import COLLECTION_TO_OUTPUT_COMMAND, settings
from database.mongo import get_db
from services.snapshot_cache import ItemSnapshot, LOOKUP_PROJECTION, parse_item

log = logging.getLogger(__name__)


class MongoExactLookup:
    """Read-only exact lookup backend for SQLite mode.

    Queries only exact fields that the Adding Bot V3 indexes. The lookup bot does
    not create/update/delete MongoDB records.
    """

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(8)

    @staticmethod
    def _collections(collections: list[str] | None) -> list[str]:
        return list(collections) if collections else list(COLLECTION_TO_OUTPUT_COMMAND.keys())

    async def _find_in_collection(self, collection: str, query: dict[str, Any]) -> ItemSnapshot | None:
        default_command = COLLECTION_TO_OUTPUT_COMMAND.get(collection, settings.default_command)
        try:
            async with self._sem:
                doc = await get_db()[collection].find_one(
                    query,
                    projection=LOOKUP_PROJECTION,
                    max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                )
            return parse_item(collection, default_command, doc) if doc else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Mongo exact lookup failed collection=%s error=%s", collection, exc)
            return None

    async def _find_first(self, query: dict[str, Any], collections: list[str] | None) -> ItemSnapshot | None:
        selected = self._collections(collections)
        if not selected:
            return None

        # Scoped lookups are almost always one collection, so avoid task overhead.
        if len(selected) == 1:
            return await self._find_in_collection(selected[0], query)

        tasks = [asyncio.create_task(self._find_in_collection(name, query)) for name in selected]
        try:
            for future in asyncio.as_completed(tasks):
                item = await future
                if item:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    return item
            return None
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def exact_uid(self, uid: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not uid:
            return None
        return await self._find_first(
            {"$or": [{"file_unique_id": uid}, {"file_unique_ids": uid}]}, collections
        )

    async def exact_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not sha:
            return None
        return await self._find_first(
            {"$or": [{"sha256": sha}, {"sha256_aliases": sha}]}, collections
        )

    async def exact_pixel_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not sha:
            return None
        return await self._find_first({"photo_fingerprint.pixel_sha256": sha}, collections)

    async def exact_video_signature(
        self, signature: str, collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if not signature:
            return None
        return await self._find_first({"video_fingerprint.video_signature": signature}, collections)

    async def exact_origin(
        self, key: tuple[int, int], collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        chat_id, message_id = key
        return await self._find_first(
            {
                "source_origin.chat_id": int(chat_id),
                "source_origin.message_id": int(message_id),
            },
            collections,
        )

    async def fetch_items_by_ids(
        self, keys: Iterable[tuple[str, str]]
    ) -> list[ItemSnapshot]:
        grouped: dict[str, list[str]] = {}
        for collection, mongo_id in keys:
            grouped.setdefault(collection, []).append(str(mongo_id))

        out: list[ItemSnapshot] = []
        for collection, raw_ids in grouped.items():
            # SQLite stores ObjectId strings. Query both string and ObjectId when available.
            values: list[Any] = list(raw_ids)
            try:
                from bson import ObjectId

                values.extend(ObjectId(value) for value in raw_ids if ObjectId.is_valid(value))
            except Exception:
                pass
            try:
                cursor = get_db()[collection].find(
                    {"_id": {"$in": values}}, projection=LOOKUP_PROJECTION
                ).batch_size(max(1, settings.sqlite_batch_size))
                default_command = COLLECTION_TO_OUTPUT_COMMAND.get(collection, settings.default_command)
                async for doc in cursor:
                    item = parse_item(collection, default_command, doc)
                    if item:
                        out.append(item)
            except Exception as exc:
                log.warning("Mongo candidate fetch failed collection=%s error=%s", collection, exc)
        return out


mongo_exact_lookup = MongoExactLookup()

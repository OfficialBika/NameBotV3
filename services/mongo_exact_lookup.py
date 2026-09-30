from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from config import COLLECTION_TO_OUTPUT_COMMAND, settings
from database.mongo import get_db
from services.snapshot_cache import ItemSnapshot, LOOKUP_PROJECTION, parse_item
from services.unified_adding_db import unified_adding_db

log = logging.getLogger(__name__)


class MongoExactLookup:
    """Read-only exact lookup backend for SQLite mode.

    Queries only exact fields that the Adding Bot V3 indexes. The lookup bot does
    not create/update/delete MongoDB records.
    """

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(8)

    @staticmethod
    def _uid_query(uids: str | Iterable[str]) -> dict[str, Any]:
        """Match the canonical Adding schema first, then known legacy layouts."""
        if isinstance(uids, str):
            values = [uids.strip()] if uids.strip() else []
        else:
            values = []
            for raw in uids:
                value = str(raw or "").strip()
                if value and value not in values:
                    values.append(value)
        if not values:
            return {"$expr": {"$eq": [1, 0]}}

        # Canonical Adding-Helperbot fields are flat and indexed:
        #   file_unique_ids[]           -> idx_*_file_uid
        #   telegram_file_unique_id     -> idx_*_telegram_file_uid
        # Keep the legacy branches below so older imported records remain searchable.
        return {
            "$or": [
                {"file_unique_ids": {"$in": values}},
                {"telegram_file_unique_id": {"$in": values}},
                {"telegram_file_unique_ids": {"$in": values}},
                {"file_unique_id": {"$in": values}},
                {"photo_file_unique_id": {"$in": values}},
                {"video_file_unique_id": {"$in": values}},
                {"media.file_unique_id": {"$in": values}},
                {"media.file_unique_ids": {"$in": values}},
                {"media.telegram_file_unique_id": {"$in": values}},
                {"media.telegram_file_unique_ids": {"$in": values}},
                {"file.unique_id": {"$in": values}},
                {"file.file_unique_id": {"$in": values}},
                {"file_unique_ids.file_unique_id": {"$in": values}},
                {"file_unique_ids.unique_id": {"$in": values}},
            ]
        }

    @staticmethod
    def _collections(collections: list[str] | None) -> list[str]:
        return list(collections) if collections else list(COLLECTION_TO_OUTPUT_COMMAND.keys())

    async def _find_in_collection(self, collection: str, query: dict[str, Any]) -> ItemSnapshot | None:
        default_command = COLLECTION_TO_OUTPUT_COMMAND.get(collection, settings.default_command)
        try:
            async with self._sem:
                if unified_adding_db.enabled:
                    scoped_query = unified_adding_db.scoped_query(query, [collection])
                    doc = await unified_adding_db.collection().find_one(
                        scoped_query,
                        projection=LOOKUP_PROJECTION,
                        max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                    )
                    resolved_source = unified_adding_db.source_key(doc, collection) if doc else collection
                else:
                    doc = await get_db()[collection].find_one(
                        query,
                        projection=LOOKUP_PROJECTION,
                        max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                    )
                    resolved_source = collection
            return parse_item(
                resolved_source,
                COLLECTION_TO_OUTPUT_COMMAND.get(resolved_source, default_command),
                doc,
            ) if doc else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Mongo exact lookup failed collection=%s error=%s", collection, exc)
            return None

    async def _find_first(self, query: dict[str, Any], collections: list[str] | None) -> ItemSnapshot | None:
        selected = self._collections(collections)
        if not selected:
            return None

        # Adding-Helperbot stores all records in one physical collection. Query
        # source_key directly instead of opening one Mongo query per legacy
        # logical collection.
        if unified_adding_db.enabled:
            try:
                scoped_query = unified_adding_db.scoped_query(query, selected if collections else None)
                async with self._sem:
                    doc = await unified_adding_db.collection().find_one(
                        scoped_query,
                        projection=LOOKUP_PROJECTION,
                        max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                    )
                if not doc:
                    return None
                source = unified_adding_db.source_key(doc)
                default_command = COLLECTION_TO_OUTPUT_COMMAND.get(
                    source,
                    COLLECTION_TO_OUTPUT_COMMAND.get(selected[0], settings.default_command),
                )
                return parse_item(source or selected[0], default_command, doc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Mongo unified exact lookup failed sources=%s error=%s", selected, exc)
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
        return await self.exact_uids([uid], collections)

    async def exact_uids(
        self, uids: Iterable[str], collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        values = [str(uid or "").strip() for uid in uids if str(uid or "").strip()]
        values = list(dict.fromkeys(values))
        if not values:
            return None
        return await self._find_first(self._uid_query(values), collections)

    async def global_exact_uid(
        self,
        uid: str,
        *,
        preferred_collection: str = "items_character_catcher",
        preferred_collections: list[str] | None = None,
        limit: int = 20,
    ) -> ItemSnapshot | None:
        return await self.global_exact_uids(
            [uid],
            preferred_collection=preferred_collection,
            preferred_collections=preferred_collections,
            limit=limit,
        )

    async def global_exact_uids(
        self,
        uids: Iterable[str],
        *,
        preferred_collection: str = "items_character_catcher",
        preferred_collections: list[str] | None = None,
        limit: int = 20,
    ) -> ItemSnapshot | None:
        """Global exact UID lookup against the canonical unified characters collection."""
        values = [str(uid or "").strip() for uid in uids if str(uid or "").strip()]
        values = list(dict.fromkeys(values))
        if not values:
            return None

        if unified_adding_db.enabled:
            try:
                # Canonical indexed query. No source_key restriction is applied here:
                # this is the deliberate global fallback after source-scope miss.
                cursor = unified_adding_db.collection().find(
                    self._uid_query(values),
                    projection=LOOKUP_PROJECTION,
                ).limit(max(2, limit))
                items: list[ItemSnapshot] = []
                raw_matches = 0
                parsed_matches = 0
                unknown_sources: list[str] = []

                async for doc in cursor:
                    raw_matches += 1
                    source = unified_adding_db.source_key(doc)
                    if source not in COLLECTION_TO_OUTPUT_COMMAND:
                        if source not in unknown_sources:
                            unknown_sources.append(source or "<empty>")
                        continue
                    item = parse_item(
                        source,
                        COLLECTION_TO_OUTPUT_COMMAND.get(source, settings.default_command),
                        doc,
                    )
                    if item:
                        parsed_matches += 1
                        items.append(item)

                if not items:
                    log.warning(
                        "Global UID miss db=%s collection=%s uids=%s raw_matches=%s "
                        "parsed_matches=%s unknown_sources=%s",
                        unified_adding_db.db_name,
                        unified_adding_db.collection_name,
                        values,
                        raw_matches,
                        parsed_matches,
                        unknown_sources,
                    )
                    return None

                preferences: list[str] = []
                for value in (preferred_collections or []):
                    value = str(value).strip().lower()
                    if value and value not in preferences:
                        preferences.append(value)
                preferred = str(preferred_collection or "").strip().lower()
                if preferred and preferred not in preferences:
                    preferences.append(preferred)

                for preferred_name in preferences:
                    preferred_item = next(
                        (item for item in items if item.collection == preferred_name),
                        None,
                    )
                    if preferred_item:
                        return preferred_item

                if len(items) == 1:
                    return items[0]

                log.warning(
                    "Global UID ambiguous db=%s collection=%s uids=%s candidates=%s sources=%s",
                    unified_adding_db.db_name,
                    unified_adding_db.collection_name,
                    values,
                    len(items),
                    [item.collection for item in items],
                )
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "Unified global UID lookup failed db=%s collection=%s uids=%s error=%s",
                    unified_adding_db.db_name,
                    unified_adding_db.collection_name,
                    values,
                    exc,
                )
                return None

        # Legacy physical-collection mode.
        for value in values:
            item = await self.exact_uid(value, None)
            if item:
                return item
        return None


    @staticmethod
    def _file_id_query(file_ids: Iterable[str]) -> dict[str, Any]:
        values = list(dict.fromkeys(
            str(value or "").strip()
            for value in file_ids
            if str(value or "").strip()
        ))
        if not values:
            return {"$expr": {"$eq": [1, 0]}}
        return {
            "$or": [
                {"telegram_file_id": {"$in": values}},
                {"file_id": {"$in": values}},
                {"file_ids": {"$in": values}},
                {"media.file_id": {"$in": values}},
                {"file.id": {"$in": values}},
            ]
        }

    @staticmethod
    def _character_id_query(character_id: str | int | None) -> dict[str, Any]:
        raw = str(character_id or "").strip()
        if not raw:
            return {"$expr": {"$eq": [1, 0]}}
        values: list[Any] = [raw]
        try:
            values.append(int(raw))
        except Exception:
            pass
        return {
            "$or": [
                {"character_id": {"$in": values}},
                {"card_id": {"$in": values}},
                {"char_id": {"$in": values}},
                {"item_id": {"$in": values}},
                {"id": {"$in": values}},
            ]
        }

    async def catch_exact_compat(
        self,
        *,
        uids: Iterable[str] = (),
        file_ids: Iterable[str] = (),
        character_id: str | int | None = None,
    ) -> ItemSnapshot | None:
        """Catch-only exact compatibility path.

        It never broadens to unrelated sources. It checks the canonical unified
        characters collection first, then legacy physical Catch collections.
        Exact Telegram UID remains the first key, with file_id and Catch's numeric
        character ID as migration/legacy recovery keys.
        """
        catch_collections = ["items_character_catcher", "items_character_catcher_fw"]

        queries: list[tuple[str, dict[str, Any]]] = []
        uid_values = [str(value or "").strip() for value in uids if str(value or "").strip()]
        if uid_values:
            queries.append(("uid", self._uid_query(uid_values)))
        file_values = [str(value or "").strip() for value in file_ids if str(value or "").strip()]
        if file_values:
            queries.append(("file_id", self._file_id_query(file_values)))
        if str(character_id or "").strip():
            queries.append(("character_id", self._character_id_query(character_id)))

        if not queries:
            return None

        def parse_doc(source: str, doc: dict[str, Any] | None) -> ItemSnapshot | None:
            if not doc:
                return None
            resolved = unified_adding_db.source_key(doc, source) if unified_adding_db.enabled else source
            return parse_item(
                resolved,
                COLLECTION_TO_OUTPUT_COMMAND.get(resolved, "/catch"),
                doc,
            )

        if unified_adding_db.enabled:
            for reason, query in queries:
                try:
                    scoped = unified_adding_db.scoped_query(query, catch_collections)
                    async with self._sem:
                        doc = await unified_adding_db.collection().find_one(
                            scoped,
                            projection=LOOKUP_PROJECTION,
                            max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                        )
                    item = parse_doc("items_character_catcher", doc)
                    if item:
                        log.info(
                            "Catch compatibility match tier=canonical key=%s source=%s name=%s",
                            reason,
                            item.collection,
                            item.name,
                        )
                        return item
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Catch canonical compatibility lookup failed key=%s error=%s",
                        reason,
                        exc,
                    )

        for collection in catch_collections:
            for reason, query in queries:
                try:
                    async with self._sem:
                        doc = await get_db()[collection].find_one(
                            query,
                            projection=LOOKUP_PROJECTION,
                            max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                        )
                    item = parse_doc(collection, doc)
                    if item:
                        log.info(
                            "Catch compatibility match tier=legacy key=%s collection=%s name=%s",
                            reason,
                            collection,
                            item.name,
                        )
                        return item
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Catch legacy compatibility lookup failed collection=%s key=%s error=%s",
                        collection,
                        reason,
                        exc,
                    )
        return None

    async def exact_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not sha:
            return None
        return await self._find_first(
            {"$or": [{"sha256": sha}, {"sha256_aliases": sha}, {"media.sha256": sha}, {"file.sha256": sha}, {"media_sha256": sha}, {"hash": sha}, {"file_hash": sha}]}, collections
        )

    async def exact_pixel_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not sha:
            return None
        return await self._find_first(
            {"$or": [{"pixel_sha256": sha}, {"photo_fingerprint.pixel_sha256": sha}]},
            collections,
        )

    async def exact_video_signature(
        self, signature: str, collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if not signature:
            return None
        return await self._find_first(
            {
                "$or": [
                    {"video_signature": signature},
                    {"video_fingerprint.video_signature": signature},
                ]
            },
            collections,
        )

    async def exact_origin(
        self, key: tuple[int, int], collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        chat_id, message_id = key
        return await self._find_first(
            {
                "$or": [
                    {
                        "source_origin.chat_id": int(chat_id),
                        "source_origin.message_id": int(message_id),
                    },
                    {
                        "origin_chat_id": int(chat_id),
                        "origin_message_id": int(message_id),
                    },
                ]
            },
            collections,
        )

    async def photo_candidates(
        self, collections: list[str] | None, max_candidates: int
    ) -> list[ItemSnapshot]:
        """Small fallback candidate scan used only while the SQLite index is not ready."""
        selected = self._collections(collections)
        if not selected or max_candidates <= 0:
            return []
        query = {
            "$or": [
                {"photo_fingerprint.phash": {"$exists": True}},
                {"phash": {"$exists": True}},
                {"photo_phash": {"$exists": True}},
                {"image_phash": {"$exists": True}},
            ]
        }
        out: list[ItemSnapshot] = []
        if unified_adding_db.enabled:
            try:
                cursor = unified_adding_db.collection().find(
                    unified_adding_db.scoped_query(query, selected if collections else None),
                    projection=LOOKUP_PROJECTION,
                ).limit(max_candidates)
                async for doc in cursor:
                    source = unified_adding_db.source_key(doc)
                    item = parse_item(
                        source,
                        COLLECTION_TO_OUTPUT_COMMAND.get(source, settings.default_command),
                        doc,
                    )
                    if item:
                        out.append(item)
                        if len(out) >= max_candidates:
                            return out
            except Exception as exc:
                log.warning("Mongo unified photo candidate fallback failed sources=%s error=%s", selected, exc)
            return out

        per_collection = max(1, max_candidates // max(1, len(selected)))
        for collection in selected:
            try:
                cursor = get_db()[collection].find(
                    query,
                    projection=LOOKUP_PROJECTION,
                ).limit(per_collection)
                default_command = COLLECTION_TO_OUTPUT_COMMAND.get(collection, settings.default_command)
                async for doc in cursor:
                    item = parse_item(collection, default_command, doc)
                    if item:
                        out.append(item)
                        if len(out) >= max_candidates:
                            return out
            except Exception as exc:
                log.warning("Mongo photo candidate fallback failed collection=%s error=%s", collection, exc)
        return out

    async def video_candidates(
        self, collections: list[str] | None, duration_ms: int, tolerance_seconds: int, max_candidates: int
    ) -> list[ItemSnapshot]:
        """Small fallback candidate scan used only while the SQLite index is not ready."""
        selected = self._collections(collections)
        if not selected or max_candidates <= 0:
            return []
        query: dict[str, Any] = {
            "$or": [
                {"video_fingerprint.sample_hashes": {"$exists": True}},
                {"video_fingerprint.video_signature": {"$exists": True}},
                {"frame_hashes": {"$exists": True}},
                {"video_frame_hashes": {"$exists": True}},
                {"frames": {"$exists": True}},
            ]
        }
        if duration_ms > 0:
            lo = max(0, int(duration_ms - max(0, tolerance_seconds) * 1000))
            hi = int(duration_ms + max(0, tolerance_seconds) * 1000)
            query = {
                "$and": [
                    query,
                    {"$or": [
                        {"video_fingerprint.duration_ms": {"$gte": lo, "$lte": hi}},
                        {"media_geometry.duration_ms": {"$gte": lo, "$lte": hi}},
                    ]},
                ]
            }
        out: list[ItemSnapshot] = []
        if unified_adding_db.enabled:
            try:
                cursor = unified_adding_db.collection().find(
                    unified_adding_db.scoped_query(query, selected if collections else None),
                    projection=LOOKUP_PROJECTION,
                ).limit(max_candidates)
                async for doc in cursor:
                    source = unified_adding_db.source_key(doc)
                    item = parse_item(
                        source,
                        COLLECTION_TO_OUTPUT_COMMAND.get(source, settings.default_command),
                        doc,
                    )
                    if item:
                        out.append(item)
                        if len(out) >= max_candidates:
                            return out
            except Exception as exc:
                log.warning("Mongo unified video candidate fallback failed sources=%s error=%s", selected, exc)
            return out

        per_collection = max(1, max_candidates // max(1, len(selected)))
        for collection in selected:
            try:
                cursor = get_db()[collection].find(
                    query,
                    projection=LOOKUP_PROJECTION,
                ).limit(per_collection)
                default_command = COLLECTION_TO_OUTPUT_COMMAND.get(collection, settings.default_command)
                async for doc in cursor:
                    item = parse_item(collection, default_command, doc)
                    if item:
                        out.append(item)
                        if len(out) >= max_candidates:
                            return out
            except Exception as exc:
                log.warning("Mongo video candidate fallback failed collection=%s error=%s", collection, exc)
        return out

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
                base = {"_id": {"$in": values}}
                if unified_adding_db.enabled:
                    query = unified_adding_db.scoped_query(base, [collection])
                    cursor = unified_adding_db.collection().find(
                        query,
                        projection=LOOKUP_PROJECTION,
                    ).batch_size(max(1, settings.sqlite_batch_size))
                else:
                    cursor = get_db()[collection].find(
                        base,
                        projection=LOOKUP_PROJECTION,
                    ).batch_size(max(1, settings.sqlite_batch_size))
                async for doc in cursor:
                    source = unified_adding_db.source_key(doc, collection) if unified_adding_db.enabled else collection
                    item = parse_item(
                        source,
                        COLLECTION_TO_OUTPUT_COMMAND.get(source, settings.default_command),
                        doc,
                    )
                    if item:
                        out.append(item)
            except Exception as exc:
                log.warning("Mongo candidate fetch failed collection=%s error=%s", collection, exc)
        return out


mongo_exact_lookup = MongoExactLookup()

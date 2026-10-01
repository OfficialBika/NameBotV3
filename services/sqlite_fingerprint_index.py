from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from config import COLLECTION_TO_OUTPUT_COMMAND, settings
from database.mongo import get_db
from services.hash_service import VideoSampleHash
from services.snapshot_cache import HashChunkIndex, ItemSnapshot, SQLITE_LOOKUP_PROJECTION, parse_item
from services.unified_adding_db import unified_adding_db

log = logging.getLogger(__name__)

# SQLite is a derived lookup index. Keep only fields required to identify, verify,
# and format a lookup result; never mirror the full MongoDB document into SQLite.
SQLITE_ITEM_FIELDS = (
    "mongo_id", "collection", "command", "name", "media_type",
    "file_unique_id", "file_unique_ids", "sha256", "sha256_aliases",
    "phash", "pixel_sha256", "phash_large", "dhash", "whash", "colorhash",
    "crop_hash", "frame_hashes", "video_samples", "video_signature", "duration_ms",
    "origin_chat_id", "origin_message_id",
)
SQLITE_INDEX_SCHEMA_VERSION = "4"


class SQLiteFingerprintIndex:
    """Persistent, rebuildable local similarity index.

    MongoDB remains the source of truth. This database stores only lookup-ready
    fingerprints and a compact serialized ItemSnapshot payload so candidate
    verification does not need a second MongoDB round trip.
    """

    def __init__(self) -> None:
        self.db: aiosqlite.Connection | None = None
        self.path = settings.sqlite_index_path
        self.ready = False
        # Exact-key lookups can safely use a clean, partially rebuilt index.
        # Similarity candidates still wait for the complete index.
        self.exact_ready = False
        self.building = False
        self.opened_at = 0.0
        self.last_sync_at: datetime | None = None
        self.last_full_build_monotonic = 0.0
        self._build_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        if self.db is not None:
            return
        path = Path(self.path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(str(path))
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        # Keep SQLite's private page cache bounded on Render Free. This is a
        # disk-backed L2 index, so Python RAM stays focused on request work and
        # the tiny UID->name L1 cache.
        await self.db.execute("PRAGMA cache_size=-4096")
        await self.db.execute("PRAGMA mmap_size=0")
        await self.db.execute("PRAGMA temp_store=MEMORY")
        await self.db.execute(f"PRAGMA busy_timeout={max(100, settings.sqlite_busy_timeout_ms)}")
        await self.db.execute("PRAGMA wal_autocheckpoint=1000")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fingerprint_items (
                collection TEXT NOT NULL,
                mongo_id TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT '',
                phash TEXT,
                dhash TEXT,
                duration_bucket INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                item_json TEXT NOT NULL,
                PRIMARY KEY (collection, mongo_id)
            );

            CREATE INDEX IF NOT EXISTS idx_fp_media_collection
                ON fingerprint_items(collection, media_type);
            CREATE INDEX IF NOT EXISTS idx_fp_video_duration
                ON fingerprint_items(collection, media_type, duration_bucket);

            CREATE TABLE IF NOT EXISTS exact_keys (
                collection TEXT NOT NULL,
                key_type TEXT NOT NULL,
                key_value TEXT NOT NULL,
                mongo_id TEXT NOT NULL,
                PRIMARY KEY (collection, key_type, key_value, mongo_id)
            );

            CREATE INDEX IF NOT EXISTS idx_exact_keys_lookup
                ON exact_keys(key_type, key_value, collection);

            CREATE TABLE IF NOT EXISTS hash_chunks (
                collection TEXT NOT NULL,
                field TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                position INTEGER NOT NULL,
                chunk_value TEXT NOT NULL,
                mongo_id TEXT NOT NULL,
                PRIMARY KEY (
                    collection, field, chunk_count, position, chunk_value, mongo_id
                )
            );

            CREATE INDEX IF NOT EXISTS idx_hash_chunk_lookup
                ON hash_chunks(collection, field, chunk_count, position, chunk_value);
            """
        )
        await self.db.commit()
        self.opened_at = time.time()
        self.last_sync_at = await self._load_watermark()
        count = await self.count()
        schema_version = await self._schema_version()
        # An empty index has no stale rows, so exact lookups may run immediately
        # while the background builder fills it. Non-empty persisted indexes remain
        # blocked until ensure_built() validates or cleanly rebuilds them.
        self.exact_ready = count == 0
        # Never serve a persisted SQLite snapshot as authoritative immediately
        # after restart. ensure_built() validates completeness first; until then
        # callers fall back to MongoDB, which remains the source of truth.
        self.ready = False
        log.info(
            "SQLite fingerprint index opened path=%s items=%s ready=%s schema=%s fields=%s",
            path, count, self.ready, schema_version or "legacy", len(SQLITE_ITEM_FIELDS),
        )

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
        self.db = None
        self.ready = False
        self.exact_ready = False

    async def _load_watermark(self) -> datetime | None:
        if self.db is None:
            return None
        cursor = await self.db.execute("SELECT value FROM index_meta WHERE key='last_sync_at'")
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        try:
            value = datetime.fromisoformat(str(row[0]))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    async def _schema_version(self) -> str | None:
        if self.db is None:
            return None
        cursor = await self.db.execute("SELECT value FROM index_meta WHERE key='schema_version'")
        row = await cursor.fetchone()
        await cursor.close()
        return str(row[0]) if row else None

    async def _save_schema_version(self) -> None:
        if self.db is None:
            return
        await self.db.execute(
            "INSERT INTO index_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SQLITE_INDEX_SCHEMA_VERSION,),
        )

    async def _save_watermark(self, value: datetime) -> None:
        if self.db is None:
            return
        await self.db.execute(
            "INSERT INTO index_meta(key, value) VALUES('last_sync_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value.isoformat(),),
        )
        self.last_sync_at = value

    @staticmethod
    def _item_to_json(item: ItemSnapshot) -> str:
        # Do not serialize asdict(item): ItemSnapshot contains compatibility/output
        # fields that are not needed for lookup and would unnecessarily duplicate
        # MongoDB data inside the local SQLite file.
        data = asdict(item)
        compact = {key: data.get(key) for key in SQLITE_ITEM_FIELDS}
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _item_from_json(raw: str) -> ItemSnapshot | None:
        try:
            data = json.loads(raw)
            data["name_aliases"] = tuple(data.get("name_aliases") or ())
            data["file_unique_ids"] = tuple(data.get("file_unique_ids") or ())
            data["sha256_aliases"] = tuple(data.get("sha256_aliases") or ())
            data["frame_hashes"] = tuple(data.get("frame_hashes") or ())
            data["video_samples"] = tuple(
                VideoSampleHash(
                    float(value.get("position", 0.0)),
                    int(value.get("frame_index", 0) or 0),
                    str(value.get("phash") or ""),
                    str(value.get("dhash") or ""),
                )
                for value in (data.get("video_samples") or [])
                if isinstance(value, dict) and value.get("phash") and value.get("dhash")
            )
            allowed = {field.name for field in fields(ItemSnapshot)}
            return ItemSnapshot(**{key: value for key, value in data.items() if key in allowed})
        except Exception:
            log.exception("Failed to decode SQLite item payload")
            return None

    async def count(self) -> int:
        if self.db is None:
            return 0
        cursor = await self.db.execute("SELECT COUNT(*) FROM fingerprint_items")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0] if row else 0)

    async def stats(self) -> dict[str, Any]:
        if self.db is None:
            return {"ready": False, "building": self.building, "items": 0, "photos": 0, "videos": 0, "age_seconds": -1, "path": self.path}
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN media_type='photo' THEN 1 ELSE 0 END) AS photos, "
            "SUM(CASE WHEN media_type='video' THEN 1 ELSE 0 END) AS videos "
            "FROM fingerprint_items"
        )
        row = await cursor.fetchone()
        await cursor.close()
        age = -1
        if self.last_sync_at:
            age = max(0, int((datetime.now(timezone.utc) - self.last_sync_at).total_seconds()))
        return {
            "ready": self.ready,
            "building": self.building,
            "items": int(row["total"] or 0) if row else 0,
            "photos": int(row["photos"] or 0) if row else 0,
            "videos": int(row["videos"] or 0) if row else 0,
            "age_seconds": age,
            "path": self.path,
        }

    @staticmethod
    def _indexable_mongo_query() -> dict[str, Any]:
        # Count only documents that parse_item() can actually materialize into
        # the SQLite lookup index. System/invalid records are intentionally ignored.
        return {
            "$or": [
                {"name": {"$nin": [None, ""]}},
                {"character_name": {"$nin": [None, ""]}},
                {"char_name": {"$nin": [None, ""]}},
                {"item_name": {"$nin": [None, ""]}},
                {"card_name": {"$nin": [None, ""]}},
                {"display_name": {"$nin": [None, ""]}},
                {"title": {"$nin": [None, ""]}},
                {"media.name": {"$nin": [None, ""]}},
                {"character.name": {"$nin": [None, ""]}},
            ]
        }

    async def _matches_mongo_counts(self) -> bool:
        """Verify that the local secondary index covers the canonical data set."""
        if self.db is None:
            return False
        try:
            indexed_total = await self.count()

            if unified_adding_db.enabled:
                mongo_total = int(
                    await unified_adding_db.collection().count_documents(
                        self._indexable_mongo_query()
                    )
                )
                if indexed_total != mongo_total:
                    log.warning(
                        "SQLite completeness mismatch canonical db=%s collection=%s "
                        "sqlite=%s mongo=%s",
                        unified_adding_db.db_name,
                        unified_adding_db.collection_name,
                        indexed_total,
                        mongo_total,
                    )
                    return False
                return True

            async def mongo_count(collection: str) -> tuple[str, int]:
                return collection, int(
                    await get_db()[collection].count_documents(
                        self._indexable_mongo_query()
                    )
                )

            cursor = await self.db.execute(
                "SELECT collection, COUNT(*) AS n "
                "FROM fingerprint_items GROUP BY collection"
            )
            rows = await cursor.fetchall()
            await cursor.close()
            indexed = {
                str(row["collection"]): int(row["n"] or 0)
                for row in rows
            }

            counts = await asyncio.gather(
                *(mongo_count(collection) for collection in COLLECTION_TO_OUTPUT_COMMAND)
            )
            expected_total = 0
            for collection, mongo_count_value in counts:
                expected_total += mongo_count_value
                if indexed.get(collection, 0) != mongo_count_value:
                    log.warning(
                        "SQLite completeness mismatch collection=%s sqlite=%s mongo=%s",
                        collection,
                        indexed.get(collection, 0),
                        mongo_count_value,
                    )
                    return False
            return indexed_total == expected_total
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SQLite completeness check failed")
            return False

    async def ensure_built(self) -> None:
        await self.open()
        await unified_adding_db.detect()
        if not settings.sqlite_build_on_start:
            return
        existing = await self.count()
        schema_version = await self._schema_version()
        if (
            existing > 0
            and schema_version == SQLITE_INDEX_SCHEMA_VERSION
            and self.last_sync_at is not None
            and not settings.sqlite_rebuild_on_start
            and await self._matches_mongo_counts()
        ):
            self.ready = True
            self.exact_ready = True
            return
        if existing > 0 and schema_version != SQLITE_INDEX_SCHEMA_VERSION:
            log.info(
                "SQLite index schema upgrade %s -> %s; rebuilding compact lookup index only",
                schema_version or "legacy", SQLITE_INDEX_SCHEMA_VERSION,
            )
        # A persisted partial/stale index must be discarded before rebuilding.
        # Reusing it can leave deleted Mongo documents behind and keep the
        # completeness check failing forever.
        await self.build_full(clear_existing=True)

    async def build_full(self, *, clear_existing: bool = True) -> int:
        await self.open()
        assert self.db is not None
        async with self._build_lock:
            self.building = True
            self.ready = False
            build_watermark = datetime.now(timezone.utc)
            total = 0
            failed_collections: list[str] = []
            try:
                if clear_existing:
                    async with self._write_lock:
                        await self.db.execute("DELETE FROM exact_keys")
                        await self.db.execute("DELETE FROM hash_chunks")
                        await self.db.execute("DELETE FROM fingerprint_items")
                        await self.db.execute("DELETE FROM index_meta WHERE key='last_sync_at'")
                        await self.db.commit()
                    self.last_sync_at = None
                    # The old persisted rows are now gone; partial exact-key reads
                    # are safe and fall back to Mongo when a record is not built yet.
                    self.exact_ready = True

                if unified_adding_db.enabled:
                    batch: list[ItemSnapshot] = []
                    try:
                        # Index every canonical document. Source-aware lookup
                        # can use known source keys, while global lookup must also
                        # retain records from newly added/previously unknown sources.
                        cursor = unified_adding_db.collection().find(
                            {},
                            projection=SQLITE_LOOKUP_PROJECTION,
                        ).batch_size(max(1, settings.sqlite_batch_size))
                        async for doc in cursor:
                            source = unified_adding_db.source_key(doc)
                            default_command = COLLECTION_TO_OUTPUT_COMMAND.get(
                                source,
                                settings.default_command,
                            )
                            item = parse_item(source, default_command, doc)
                            if not item:
                                continue
                            batch.append(item)
                            if len(batch) >= max(1, settings.sqlite_batch_size):
                                await self.upsert_items(batch)
                                total += len(batch)
                                batch.clear()
                        if batch:
                            await self.upsert_items(batch)
                            total += len(batch)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failed_collections.append(unified_adding_db.collection_name)
                        log.exception(
                            "SQLite unified Adding DB initial index load failed for %s",
                            unified_adding_db.collection_name,
                        )
                else:
                    for collection, default_command in COLLECTION_TO_OUTPUT_COMMAND.items():
                        batch: list[ItemSnapshot] = []
                        try:
                            cursor = get_db()[collection].find(
                                {}, projection=SQLITE_LOOKUP_PROJECTION
                            ).batch_size(max(1, settings.sqlite_batch_size))
                            async for doc in cursor:
                                item = parse_item(collection, default_command, doc)
                                if not item:
                                    continue
                                batch.append(item)
                                if len(batch) >= max(1, settings.sqlite_batch_size):
                                    await self.upsert_items(batch)
                                    total += len(batch)
                                    batch.clear()
                            if batch:
                                await self.upsert_items(batch)
                                total += len(batch)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            failed_collections.append(collection)
                            log.exception("SQLite initial index load failed for %s", collection)

                async with self._write_lock:
                    if not failed_collections:
                        await self._save_schema_version()
                        await self._save_watermark(build_watermark)
                    await self.db.commit()
                self.last_full_build_monotonic = time.monotonic()
                self.ready = (await self.count()) > 0 and not failed_collections
                self.exact_ready = self.exact_ready and not failed_collections
                if failed_collections:
                    log.warning(
                        "SQLite fingerprint build incomplete items=%s failed_collections=%s; retry will run",
                        total,
                        failed_collections,
                    )
                else:
                    log.info("SQLite fingerprint full build complete items=%s ready=%s", total, self.ready)
                return total
            finally:
                self.building = False

    async def upsert_items(self, items: list[ItemSnapshot]) -> None:
        if not items:
            return
        await self.open()
        assert self.db is not None
        async with self._write_lock:
            for item in items:
                bucket = int(round(item.duration_ms / 1000)) if item.duration_ms > 0 else 0
                await self.db.execute(
                    "INSERT INTO fingerprint_items(" 
                    "collection,mongo_id,media_type,phash,dhash,duration_bucket,updated_at,item_json" 
                    ") VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(collection,mongo_id) DO UPDATE SET "
                    "media_type=excluded.media_type, phash=excluded.phash, dhash=excluded.dhash, "
                    "duration_bucket=excluded.duration_bucket, updated_at=excluded.updated_at, item_json=excluded.item_json",
                    (
                        item.collection,
                        item.mongo_id,
                        item.media_type or "",
                        item.phash,
                        item.dhash,
                        bucket,
                        datetime.now(timezone.utc).isoformat(),
                        self._item_to_json(item),
                    ),
                )
                await self.db.execute(
                    "DELETE FROM exact_keys WHERE collection=? AND mongo_id=?",
                    (item.collection, item.mongo_id),
                )
                exact_rows = []
                for key_type, values in (
                    ("uid", item.all_uids),
                    ("sha", item.all_shas),
                    ("pixel_sha", (item.pixel_sha256,) if item.pixel_sha256 else ()),
                    ("video_signature", (item.video_signature,) if item.video_signature else ()),
                ):
                    for value in values:
                        if value:
                            exact_rows.append((item.collection, key_type, str(value), item.mongo_id))
                if item.origin_chat_id is not None and item.origin_message_id is not None:
                    exact_rows.append(
                        (item.collection, "origin",
                         f"{item.origin_chat_id}:{item.origin_message_id}", item.mongo_id)
                    )
                if exact_rows:
                    await self.db.executemany(
                        "INSERT OR REPLACE INTO exact_keys(collection,key_type,key_value,mongo_id) "
                        "VALUES(?,?,?,?)",
                        exact_rows,
                    )
                await self.db.execute(
                    "DELETE FROM hash_chunks WHERE collection=? AND mongo_id=?",
                    (item.collection, item.mongo_id),
                )
                chunk_rows: list[tuple[Any, ...]] = []
                for field_name, value in (("phash", item.phash), ("dhash", item.dhash)):
                    if not value:
                        continue
                    for count in HashChunkIndex.COUNTS:
                        chunks = HashChunkIndex._chunks(value, count)
                        if not chunks:
                            continue
                        for position, chunk in enumerate(chunks):
                            chunk_rows.append(
                                (item.collection, field_name, count, position, str(chunk), item.mongo_id)
                            )
                if chunk_rows:
                    await self.db.executemany(
                        "INSERT OR REPLACE INTO hash_chunks(" 
                        "collection,field,chunk_count,position,chunk_value,mongo_id" 
                        ") VALUES(?,?,?,?,?,?)",
                        chunk_rows,
                    )
            await self.db.commit()

    async def _exact_lookup(self, key_type: str, key_value: str,
                            collections: list[str] | None = None) -> ItemSnapshot | None:
        if not self.exact_ready or self.db is None or not key_value:
            return None
        selected = list(collections) if collections else None
        if selected == []:
            return None
        sql = (
            "SELECT fi.item_json FROM exact_keys ek "
            "JOIN fingerprint_items fi "
            "ON fi.collection=ek.collection AND fi.mongo_id=ek.mongo_id "
            "WHERE ek.key_type=? AND ek.key_value=?"
        )
        params: list[Any] = [key_type, str(key_value)]
        if selected is not None:
            marks = ",".join("?" for _ in selected)
            sql += f" AND ek.collection IN ({marks})"
            params.extend(selected)
        sql += " LIMIT 1"
        cursor = await self.db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return self._item_from_json(str(row["item_json"])) if row else None

    async def exact_uids(
        self,
        uids: list[str] | tuple[str, ...],
        collections: list[str] | None = None,
        preferred_collections: list[str] | None = None,
    ) -> ItemSnapshot | None:
        """Bulk exact UID lookup from the validated SQLite index.

        This is a single local SQL query for all Telegram PhotoSize UIDs. When
        multiple sources contain the same UID, source preference is honored and
        ambiguous results are rejected rather than choosing randomly.
        """
        if not self.exact_ready or self.db is None:
            return None
        values = list(dict.fromkeys(
            str(uid or "").strip() for uid in uids if str(uid or "").strip()
        ))
        if not values:
            return None

        selected = list(collections) if collections else None
        if selected == []:
            return None

        marks = ",".join("?" for _ in values)
        sql = (
            "SELECT fi.collection, fi.mongo_id, fi.item_json "
            "FROM exact_keys ek "
            "JOIN fingerprint_items fi "
            "ON fi.collection=ek.collection AND fi.mongo_id=ek.mongo_id "
            f"WHERE ek.key_type='uid' AND ek.key_value IN ({marks})"
        )
        params: list[Any] = list(values)
        if selected is not None:
            collection_marks = ",".join("?" for _ in selected)
            sql += f" AND ek.collection IN ({collection_marks})"
            params.extend(selected)
        sql += " LIMIT 100"
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        if not rows:
            return None

        items: list[ItemSnapshot] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["collection"]), str(row["mongo_id"]))
            if key in seen:
                continue
            seen.add(key)
            item = self._item_from_json(str(row["item_json"]))
            if item:
                items.append(item)

        if not items:
            return None

        preferences: list[str] = []
        for value in (preferred_collections or collections or []):
            value = str(value or "").strip()
            if value and value not in preferences:
                preferences.append(value)

        for preferred in preferences:
            item = next((candidate for candidate in items if candidate.collection == preferred), None)
            if item:
                return item

        if len(items) == 1:
            return items[0]
        return None

    async def exact_uid(self, uid: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        return await self.exact_uids([uid], collections=collections)

    async def exact_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        return await self._exact_lookup("sha", sha, collections)

    async def exact_pixel_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        return await self._exact_lookup("pixel_sha", sha, collections)

    async def exact_video_signature(self, signature: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        return await self._exact_lookup("video_signature", signature, collections)

    async def exact_origin(self, key: tuple[int, int], collections: list[str] | None = None) -> ItemSnapshot | None:
        return await self._exact_lookup("origin", f"{int(key[0])}:{int(key[1])}", collections)

    async def sync_event_items(self, keys: list[tuple[str, str]]) -> int:
        """Refresh only documents named by Adding Bot change events."""
        if not keys:
            return 0
        await self.open()
        grouped: dict[str, list[str]] = {}
        for collection, mongo_id in keys:
            grouped.setdefault(collection, []).append(str(mongo_id))
        changed = 0
        async with self._build_lock:
            for collection, raw_ids in grouped.items():
                values: list[Any] = list(dict.fromkeys(raw_ids))
                try:
                    from bson import ObjectId
                    values.extend(ObjectId(value) for value in values if ObjectId.is_valid(value))
                except Exception:
                    pass
                base_query = {"_id": {"$in": values}}
                if unified_adding_db.enabled:
                    cursor = unified_adding_db.collection().find(
                        unified_adding_db.scoped_query(base_query, [collection]),
                        projection=LOOKUP_PROJECTION,
                    )
                else:
                    cursor = get_db()[collection].find(
                        base_query,
                        projection=LOOKUP_PROJECTION,
                    )
                items: list[ItemSnapshot] = []
                async for doc in cursor:
                    source = (
                        unified_adding_db.source_key(doc, collection)
                        if unified_adding_db.enabled
                        else collection
                    )
                    default_command = COLLECTION_TO_OUTPUT_COMMAND.get(
                        source,
                        settings.default_command,
                    )
                    item = parse_item(source, default_command, doc)
                    if item:
                        items.append(item)
                if items:
                    await self.upsert_items(items)
                    changed += len(items)
        return changed

    async def delete_event_items(self, keys: list[tuple[str, str]]) -> int:
        """Remove only documents named by delete events; no full rebuild required."""
        if not keys:
            return 0
        await self.open()
        grouped: dict[str, list[str]] = {}
        for collection, mongo_id in keys:
            grouped.setdefault(collection, []).append(str(mongo_id))
        removed = 0
        async with self._build_lock:
            for collection, raw_ids in grouped.items():
                ids = list(dict.fromkeys(raw_ids))
                marks = ",".join("?" for _ in ids)
                cursor = await self.db.execute(
                    f"SELECT COUNT(*) FROM fingerprint_items WHERE collection=? AND mongo_id IN ({marks})",
                    [collection, *ids],
                )
                row = await cursor.fetchone()
                await cursor.close()
                removed += int(row[0] or 0) if row else 0
                await self.db.execute(
                    f"DELETE FROM exact_keys WHERE collection=? AND mongo_id IN ({marks})",
                    [collection, *ids],
                )
                await self.db.execute(
                    f"DELETE FROM hash_chunks WHERE collection=? AND mongo_id IN ({marks})",
                    [collection, *ids],
                )
                await self.db.execute(
                    f"DELETE FROM fingerprint_items WHERE collection=? AND mongo_id IN ({marks})",
                    [collection, *ids],
                )
            await self.db.commit()
        return removed

    async def incremental_sync(self) -> int:
        await self.open()
        if self.building:
            return 0
        start = self.last_sync_at
        if start is None:
            # No safe watermark means a full build is required before delta sync.
            if settings.sqlite_build_on_start:
                await self.ensure_built()
            return 0

        next_watermark = datetime.now(timezone.utc)
        changed_total = 0
        failed = False
        if unified_adding_db.enabled:
            try:
                # Delta-sync the whole canonical collection so newly
                # introduced source_key values are indexed too.
                cursor = unified_adding_db.collection().find(
                    {"updated_at": {"$gt": start, "$lte": next_watermark}},
                    projection=LOOKUP_PROJECTION,
                ).batch_size(max(1, settings.sqlite_batch_size))
                batch: list[ItemSnapshot] = []
                async for doc in cursor:
                    source = unified_adding_db.source_key(doc)
                    item = parse_item(
                        source,
                        COLLECTION_TO_OUTPUT_COMMAND.get(source, settings.default_command),
                        doc,
                    )
                    if not item:
                        continue
                    batch.append(item)
                    if len(batch) >= max(1, settings.sqlite_batch_size):
                        await self.upsert_items(batch)
                        changed_total += len(batch)
                        batch.clear()
                if batch:
                    await self.upsert_items(batch)
                    changed_total += len(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
                log.exception("SQLite unified Adding DB delta sync failed")
        else:
            for collection, default_command in COLLECTION_TO_OUTPUT_COMMAND.items():
                batch: list[ItemSnapshot] = []
                try:
                    cursor = get_db()[collection].find(
                        {"updated_at": {"$gt": start, "$lte": next_watermark}},
                        projection=LOOKUP_PROJECTION,
                    ).batch_size(max(1, settings.sqlite_batch_size))
                    async for doc in cursor:
                        item = parse_item(collection, default_command, doc)
                        if not item:
                            continue
                        batch.append(item)
                        if len(batch) >= max(1, settings.sqlite_batch_size):
                            await self.upsert_items(batch)
                            changed_total += len(batch)
                            batch.clear()
                    if batch:
                        await self.upsert_items(batch)
                        changed_total += len(batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed = True
                    log.exception("SQLite delta sync failed for %s", collection)

        assert self.db is not None
        async with self._write_lock:
            # Never advance the global watermark if any collection failed; replaying
            # successful deltas is safe and prevents silent gaps in the failed source.
            if not failed:
                await self._save_watermark(next_watermark)
            await self.db.commit()
        if changed_total:
            self.ready = True
            self.exact_ready = True
            log.info("SQLite fingerprint delta sync changed=%s", changed_total)
        return changed_total

    async def ensure_ready_loop(self) -> None:
        """Retry an incomplete startup build without enabling periodic full scans."""
        while True:
            try:
                await asyncio.sleep(30)
                if not settings.sqlite_build_on_start or self.ready or self.building:
                    continue
                await self.ensure_built()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SQLite readiness retry failed")
                await asyncio.sleep(30)

    async def sync_loop(self) -> None:
        while True:
            try:
                if settings.sqlite_build_on_start and not self.ready and not self.building:
                    await self.ensure_built()
                await asyncio.sleep(max(2, settings.sqlite_sync_seconds))
                if self.building:
                    continue
                if (
                    settings.sqlite_full_rebuild_seconds > 0
                    and self.last_full_build_monotonic > 0
                    and time.monotonic() - self.last_full_build_monotonic >= settings.sqlite_full_rebuild_seconds
                ):
                    await self.build_full(clear_existing=True)
                else:
                    await self.incremental_sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SQLite fingerprint sync loop failed")
                await asyncio.sleep(max(2, settings.sqlite_sync_seconds))

    async def _photo_rows_for_hash(
        self,
        collections: list[str] | None,
        field_name: str,
        value: str | None,
        threshold: int,
        limit: int,
    ) -> list[aiosqlite.Row]:
        if self.db is None or not value:
            return []
        count = min(HashChunkIndex.COUNTS, key=lambda number: abs(number - (threshold + 1)))
        chunks = HashChunkIndex._chunks(value, count)
        if not chunks:
            return []
        chunk_clauses = " OR ".join("(hc.position=? AND hc.chunk_value=?)" for _ in chunks)
        sql = (
            "SELECT DISTINCT fi.collection, fi.mongo_id, fi.item_json "
            "FROM hash_chunks hc JOIN fingerprint_items fi "
            "ON fi.collection=hc.collection AND fi.mongo_id=hc.mongo_id "
            "WHERE hc.field=? AND hc.chunk_count=? "
            f"AND ({chunk_clauses})"
        )
        params: list[Any] = [field_name, count]
        for position, chunk in enumerate(chunks):
            params.extend([position, str(chunk)])
        if collections:
            collection_marks = ",".join("?" for _ in collections)
            sql += f" AND hc.collection IN ({collection_marks})"
            params.extend(collections)
        params.append(max(1, limit))
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def photo_candidates(
        self,
        collections: list[str] | None,
        phash: str | None,
        dhash: str | None,
        phash_threshold: int,
        dhash_threshold: int,
        max_candidates: int,
    ) -> list[ItemSnapshot]:
        if self.db is None:
            return []
        selected = list(collections) if collections else None
        rows = await self._photo_rows_for_hash(
            selected, "phash", phash, phash_threshold, max_candidates
        )
        if len(rows) < max_candidates:
            rows += await self._photo_rows_for_hash(
                selected, "dhash", dhash, dhash_threshold, max_candidates - len(rows)
            )
        items: dict[tuple[str, str], ItemSnapshot] = {}
        for row in rows:
            item = self._item_from_json(str(row["item_json"]))
            if item:
                items[(item.collection, item.mongo_id)] = item
                if len(items) >= max_candidates:
                    break

        # Match Snapshot Mode behavior for scoped legacy records when hash buckets return none.
        if not items and collections:
            marks = ",".join("?" for _ in selected)
            cursor = await self.db.execute(
                f"SELECT item_json FROM fingerprint_items WHERE collection IN ({marks}) "
                "AND media_type='photo' LIMIT ?",
                [*selected, max(1, max_candidates)],
            )
            for row in await cursor.fetchall():
                item = self._item_from_json(str(row["item_json"]))
                if item:
                    items[(item.collection, item.mongo_id)] = item
            await cursor.close()
        return list(items.values())[:max_candidates]

    async def video_candidates(
        self,
        collections: list[str] | None,
        duration_ms: int,
        tolerance_seconds: int,
    ) -> list[ItemSnapshot]:
        if self.db is None:
            return []
        selected = list(collections) if collections else None
        marks = ",".join("?" for _ in selected) if selected else ""
        params: list[Any] = list(selected or [])
        if duration_ms > 0:
            second = int(round(duration_ms / 1000))
            sql = "SELECT item_json FROM fingerprint_items WHERE "
            if selected:
                sql += f"collection IN ({marks}) AND "
            sql += "media_type='video' AND duration_bucket BETWEEN ? AND ? LIMIT ?"
            params.extend([
                max(0, second - tolerance_seconds),
                second + tolerance_seconds,
                max(1, settings.video_max_candidates),
            ])
        else:
            sql = "SELECT item_json FROM fingerprint_items WHERE "
            if selected:
                sql += f"collection IN ({marks}) AND "
            sql += "media_type='video' LIMIT ?"
            params.append(max(1, settings.video_max_candidates))
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()

        # Preserve V2 compatibility when duration metadata is absent/mismatched in a scoped search.
        if not rows and selected:
            cursor = await self.db.execute(
                f"SELECT item_json FROM fingerprint_items WHERE collection IN ({marks}) "
                "AND media_type='video' LIMIT ?",
                [*selected, max(1, settings.video_max_candidates)],
            )
            rows = await cursor.fetchall()
            await cursor.close()

        out: list[ItemSnapshot] = []
        for row in rows:
            item = self._item_from_json(str(row["item_json"]))
            if item:
                out.append(item)
        return out


sqlite_index = SQLiteFingerprintIndex()

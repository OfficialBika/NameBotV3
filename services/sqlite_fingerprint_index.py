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
from services.snapshot_cache import HashChunkIndex, ItemSnapshot, LOOKUP_PROJECTION, parse_item

log = logging.getLogger(__name__)


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
        await self.db.execute("PRAGMA temp_store=MEMORY")
        await self.db.execute(f"PRAGMA busy_timeout={max(100, settings.sqlite_busy_timeout_ms)}")
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
        self.ready = count > 0
        log.info("SQLite fingerprint index opened path=%s items=%s ready=%s", path, count, self.ready)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
        self.db = None
        self.ready = False

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
        return json.dumps(asdict(item), ensure_ascii=False, separators=(",", ":"))

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

    async def ensure_built(self) -> None:
        await self.open()
        if not settings.sqlite_build_on_start:
            return
        existing = await self.count()
        if existing > 0 and self.last_sync_at is not None and not settings.sqlite_rebuild_on_start:
            self.ready = True
            return
        await self.build_full(clear_existing=settings.sqlite_rebuild_on_start or existing == 0)

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
                        await self.db.execute("DELETE FROM hash_chunks")
                        await self.db.execute("DELETE FROM fingerprint_items")
                        await self.db.execute("DELETE FROM index_meta WHERE key='last_sync_at'")
                        await self.db.commit()
                    self.last_sync_at = None

                for collection, default_command in COLLECTION_TO_OUTPUT_COMMAND.items():
                    batch: list[ItemSnapshot] = []
                    try:
                        cursor = get_db()[collection].find(
                            {}, projection=LOOKUP_PROJECTION
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
                        await self._save_watermark(build_watermark)
                    await self.db.commit()
                self.last_full_build_monotonic = time.monotonic()
                self.ready = (await self.count()) > 0
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
            log.info("SQLite fingerprint delta sync changed=%s", changed_total)
        return changed_total

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
        collections: list[str],
        field_name: str,
        value: str | None,
        threshold: int,
        limit: int,
    ) -> list[aiosqlite.Row]:
        if self.db is None or not value or not collections:
            return []
        count = min(HashChunkIndex.COUNTS, key=lambda number: abs(number - (threshold + 1)))
        chunks = HashChunkIndex._chunks(value, count)
        if not chunks:
            return []
        collection_marks = ",".join("?" for _ in collections)
        chunk_clauses = " OR ".join("(hc.position=? AND hc.chunk_value=?)" for _ in chunks)
        sql = (
            "SELECT DISTINCT fi.collection, fi.mongo_id, fi.item_json "
            "FROM hash_chunks hc JOIN fingerprint_items fi "
            "ON fi.collection=hc.collection AND fi.mongo_id=hc.mongo_id "
            f"WHERE hc.collection IN ({collection_marks}) AND hc.field=? AND hc.chunk_count=? "
            f"AND ({chunk_clauses}) LIMIT ?"
        )
        params: list[Any] = list(collections) + [field_name, count]
        for position, chunk in enumerate(chunks):
            params.extend([position, str(chunk)])
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
        if self.db is None or not self.ready:
            return []
        selected = list(collections) if collections else list(COLLECTION_TO_OUTPUT_COMMAND.keys())
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
        if self.db is None or not self.ready:
            return []
        selected = list(collections) if collections else list(COLLECTION_TO_OUTPUT_COMMAND.keys())
        marks = ",".join("?" for _ in selected)
        params: list[Any] = list(selected)
        if duration_ms > 0:
            second = int(round(duration_ms / 1000))
            sql = (
                f"SELECT item_json FROM fingerprint_items WHERE collection IN ({marks}) "
                "AND media_type='video' AND duration_bucket BETWEEN ? AND ? LIMIT ?"
            )
            params.extend([
                max(0, second - tolerance_seconds),
                second + tolerance_seconds,
                max(1, settings.video_max_candidates),
            ])
        else:
            sql = (
                f"SELECT item_json FROM fingerprint_items WHERE collection IN ({marks}) "
                "AND media_type='video' LIMIT ?"
            )
            params.append(max(1, settings.video_max_candidates))
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()

        # Preserve V2 compatibility when duration metadata is absent/mismatched in a scoped search.
        if not rows and collections:
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

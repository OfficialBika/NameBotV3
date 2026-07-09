from __future__ import annotations

import logging
from typing import Any

from config import settings
from services.mongo_exact_lookup import mongo_exact_lookup
from services.snapshot_cache import ItemSnapshot, snapshot
from services.sqlite_fingerprint_index import sqlite_index

log = logging.getLogger(__name__)


class LookupBackend:
    """Single async interface over Snapshot and SQLite lookup engines."""

    @property
    def mode(self) -> str:
        value = (settings.lookup_engine_mode or "snapshot").strip().lower()
        if value not in {"snapshot", "sqlite"}:
            log.warning("Unknown LOOKUP_ENGINE_MODE=%s; falling back to snapshot", value)
            return "snapshot"
        return value

    async def exact_origin(
        self, key: tuple[int, int], collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            return await mongo_exact_lookup.exact_origin(key, collections)
        return snapshot.exact_origin(key, collections)

    async def exact_uid(self, uid: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            return await mongo_exact_lookup.exact_uid(uid, collections)
        return snapshot.exact_uid(uid, collections)

    async def exact_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            return await mongo_exact_lookup.exact_sha(sha, collections)
        return snapshot.exact_sha(sha, collections)

    async def exact_pixel_sha(
        self, sha: str, collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            return await mongo_exact_lookup.exact_pixel_sha(sha, collections)
        return snapshot.exact_pixel_sha(sha, collections)

    async def exact_video_signature(
        self, signature: str, collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            return await mongo_exact_lookup.exact_video_signature(signature, collections)
        return snapshot.exact_video_signature(signature, collections)

    async def photo_candidates(
        self,
        collections: list[str] | None,
        phash: str | None,
        dhash: str | None,
        phash_threshold: int,
        dhash_threshold: int,
        max_candidates: int,
    ) -> list[ItemSnapshot]:
        if self.mode == "sqlite":
            return await sqlite_index.photo_candidates(
                collections,
                phash,
                dhash,
                phash_threshold,
                dhash_threshold,
                max_candidates,
            )
        return snapshot.photo_candidates(
            collections,
            phash,
            dhash,
            phash_threshold,
            dhash_threshold,
            max_candidates,
        )

    async def video_candidates(
        self,
        collections: list[str] | None,
        duration_ms: int,
        tolerance_seconds: int,
    ) -> list[ItemSnapshot]:
        if self.mode == "sqlite":
            return await sqlite_index.video_candidates(collections, duration_ms, tolerance_seconds)
        return snapshot.video_candidates(collections, duration_ms, tolerance_seconds)

    async def stats(self) -> dict[str, Any]:
        if self.mode == "sqlite":
            data = await sqlite_index.stats()
            data["mode"] = "sqlite"
            return data
        return {
            "mode": "snapshot",
            "ready": snapshot.count > 0,
            "building": False,
            "items": snapshot.count,
            "photos": sum(len(values) for values in snapshot.photos_by_collection.values()),
            "videos": sum(len(values) for values in snapshot.videos_by_collection.values()),
            "age_seconds": snapshot.age_seconds(),
            "path": "RAM",
        }


lookup_backend = LookupBackend()

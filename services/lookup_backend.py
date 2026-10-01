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
            item = await sqlite_index.exact_origin(key, collections)
            return item or await mongo_exact_lookup.exact_origin(key, collections)
        item = snapshot.exact_origin(key, collections)
        return item or await mongo_exact_lookup.exact_origin(key, collections)

    async def fast_exact_uids(
        self,
        uids: list[str] | tuple[str, ...],
        preferred_collections: list[str] | None = None,
    ) -> ItemSnapshot | None:
        """Single global exact UID lookup with source preference.

        Avoid the old source-query -> global-query waterfall. A single indexed
        SQLite/Mongo lookup can return a preferred source when available and
        otherwise falls back to the unique global match.
        """
        values = list(uids)
        preferred = list(preferred_collections or [])
        if self.mode == "sqlite":
            item = await sqlite_index.exact_uids(
                values,
                collections=None,
                preferred_collections=preferred,
            )
            if item:
                return item
            return await mongo_exact_lookup.global_exact_uids(
                values,
                preferred_collection=preferred[0] if preferred else "items_character_catcher",
                preferred_collections=preferred,
            )

        for collection in preferred:
            item = await snapshot.exact_uids(values, [collection])
            if item:
                return item
        return await mongo_exact_lookup.global_exact_uids(
            values,
            preferred_collection=preferred[0] if preferred else "items_character_catcher",
            preferred_collections=preferred,
        )

    async def exact_uids(self, uids: list[str] | tuple[str, ...], collections: list[str] | None = None) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            # One local SQL lookup for all PhotoSize UIDs. Mongo is the authoritative
            # fallback only when SQLite has no validated result.
            item = await sqlite_index.exact_uids(uids, collections=collections, preferred_collections=collections)
            return item or await mongo_exact_lookup.exact_uids(uids, collections)
        # Snapshot mode already indexes every alias in RAM, then falls back to Mongo.
        for uid in uids:
            item = snapshot.exact_uid(uid, collections)
            if item:
                return item
        return await mongo_exact_lookup.exact_uids(uids, collections)

    async def exact_file_ids(
        self, file_ids: list[str] | tuple[str, ...], collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        # file_id is a legacy compatibility key, not a global identity. Keep it
        # source-scoped and use Mongo's existing source/global file-id indexes.
        return await mongo_exact_lookup.exact_file_ids(file_ids, collections)


    async def exact_uid(self, uid: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            item = await sqlite_index.exact_uid(uid, collections)
            return item or await mongo_exact_lookup.exact_uid(uid, collections)
        item = snapshot.exact_uid(uid, collections)
        return item or await mongo_exact_lookup.exact_uid(uid, collections)

    async def global_exact_uids(
        self,
        uids: list[str] | tuple[str, ...],
        *,
        preferred_collection: str = "items_character_catcher",
        preferred_collections: list[str] | None = None,
    ) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            # Global exact lookup is SQLite-first. Mongo is only the authoritative
            # fallback when the validated local index has no result.
            preferred = list(preferred_collections or [])
            if preferred_collection and preferred_collection not in preferred:
                preferred.append(preferred_collection)
            item = await sqlite_index.exact_uids(
                uids,
                collections=None,
                preferred_collections=preferred,
            )
            if item:
                log.info(
                    "UID DEBUG global_sqlite_hit name=%s source=%s uids=%s",
                    item.name,
                    item.collection,
                    list(uids),
                )
                return item
            log.info("UID DEBUG global_sqlite_miss uids=%s; mongo_fallback=true", list(uids))
        item = await mongo_exact_lookup.global_exact_uids(
            uids,
            preferred_collection=preferred_collection,
            preferred_collections=preferred_collections,
        )
        if item:
            log.info(
                "UID DEBUG global_mongo_hit name=%s source=%s uids=%s",
                item.name,
                item.collection,
                list(uids),
            )
        return item

    async def global_exact_uid(
        self,
        uid: str,
        *,
        preferred_collection: str = "items_character_catcher",
        preferred_collections: list[str] | None = None,
    ) -> ItemSnapshot | None:
        # Global UID is always authoritative to MongoDB in the unified-DB path.
        return await mongo_exact_lookup.global_exact_uid(
            uid,
            preferred_collection=preferred_collection,
            preferred_collections=preferred_collections,
        )

    async def catch_exact_compat(
        self,
        *,
        uids: list[str] | tuple[str, ...] = (),
        file_ids: list[str] | tuple[str, ...] = (),
        character_id: str | int | None = None,
    ) -> ItemSnapshot | None:
        # Deliberately Catch-only. The compatibility path never broadens into
        # another source collection.
        return await mongo_exact_lookup.catch_exact_compat(
            uids=uids,
            file_ids=file_ids,
            character_id=character_id,
        )

    async def exact_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            item = await sqlite_index.exact_sha(sha, collections)
            return item or await mongo_exact_lookup.exact_sha(sha, collections)
        item = snapshot.exact_sha(sha, collections)
        return item or await mongo_exact_lookup.exact_sha(sha, collections)

    async def exact_pixel_sha(
        self, sha: str, collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            item = await sqlite_index.exact_pixel_sha(sha, collections)
            return item or await mongo_exact_lookup.exact_pixel_sha(sha, collections)
        item = snapshot.exact_pixel_sha(sha, collections)
        return item or await mongo_exact_lookup.exact_pixel_sha(sha, collections)

    async def exact_video_signature(
        self, signature: str, collections: list[str] | None = None
    ) -> ItemSnapshot | None:
        if self.mode == "sqlite":
            item = await sqlite_index.exact_video_signature(signature, collections)
            return item or await mongo_exact_lookup.exact_video_signature(signature, collections)
        item = snapshot.exact_video_signature(signature, collections)
        return item or await mongo_exact_lookup.exact_video_signature(signature, collections)

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
            # SQLite is only the fast candidate index. The service performs the
            # correctness fallback to MongoDB after candidate verification.
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

    async def mongo_photo_candidates_fallback(
        self,
        collections: list[str] | None,
        max_candidates: int,
        *,
        phash: str | None = None,
        dhash: str | None = None,
        phash_threshold: int | None = None,
        dhash_threshold: int | None = None,
    ) -> list[ItemSnapshot]:
        return await mongo_exact_lookup.photo_candidates(
            collections,
            max_candidates,
            phash=phash,
            dhash=dhash,
            phash_threshold=phash_threshold,
            dhash_threshold=dhash_threshold,
        )

    async def mongo_video_candidates_fallback(
        self, collections: list[str] | None, duration_ms: int, tolerance_seconds: int, max_candidates: int
    ) -> list[ItemSnapshot]:
        return await mongo_exact_lookup.video_candidates(
            collections, duration_ms, tolerance_seconds, max_candidates
        )

    async def video_candidates(
        self,
        collections: list[str] | None,
        duration_ms: int,
        tolerance_seconds: int,
    ) -> list[ItemSnapshot]:
        if self.mode == "sqlite":
            # SQLite is only the fast candidate index. The service performs the
            # correctness fallback to MongoDB after candidate verification.
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

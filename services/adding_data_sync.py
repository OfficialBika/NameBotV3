from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from config import settings
from database.mongo import get_db
from services.snapshot_cache import snapshot
from services.sqlite_fingerprint_index import sqlite_index
from services.lookup_service import lookup_service

log = logging.getLogger(__name__)

STATE_ID = "adding_bot_items"
STATE_COLLECTION = "data_sync_state"
EVENT_COLLECTION = "data_sync_events"
DEFAULT_POLL_SECONDS = 2


class AddingDataSync:
    """Consume Adding-Helperbot's MongoDB change signal.

    MongoDB remains the source of truth. Events only tell NameBot V3 that its
    rebuildable RAM/SQLite secondary indexes may be stale.
    """

    def __init__(self) -> None:
        self.last_version = 0
        self.last_event_at: datetime | None = None

    async def ensure_indexes(self) -> None:
        db = get_db()
        await db[STATE_COLLECTION].create_index("updated_at")
        await db[EVENT_COLLECTION].create_index([("version", 1)], unique=True)
        await db[EVENT_COLLECTION].create_index("created_at", expireAfterSeconds=604800)

    async def _state_version(self) -> int:
        doc = await get_db()[STATE_COLLECTION].find_one({"_id": STATE_ID}, {"version": 1})
        try:
            return int((doc or {}).get("version", 0))
        except (TypeError, ValueError):
            return 0

    async def _sync_after_events(self, events: list[dict]) -> None:
        if not events:
            return
        has_delete = any(event.get("operation") == "delete" for event in events)

        # Events identify the exact MongoDB documents that changed, so normal
        # updates never scan every collection. Deletes are removed directly from
        # the secondary index; full rebuild is reserved for an event-history gap.
        keys = [
            (str(event.get("collection") or ""), str(event.get("document_id") or ""))
            for event in events
            if event.get("collection") and event.get("document_id")
        ]
        delete_keys = [
            (str(event.get("collection") or ""), str(event.get("document_id") or ""))
            for event in events
            if event.get("operation") == "delete" and event.get("collection") and event.get("document_id")
        ]
        upsert_keys = [
            (str(event.get("collection") or ""), str(event.get("document_id") or ""))
            for event in events
            if event.get("operation") != "delete" and event.get("collection") and event.get("document_id")
        ]

        if settings.lookup_engine_mode == "sqlite":
            if delete_keys:
                await sqlite_index.delete_event_items(delete_keys)
            if upsert_keys:
                await sqlite_index.sync_event_items(upsert_keys)
        else:
            if delete_keys:
                await snapshot.delete_event_items(delete_keys)
            if upsert_keys:
                await snapshot.sync_event_items(upsert_keys)

        # A write can change a UID/SHA/name result that is already cached.
        # Invalidate both hits and misses immediately after applying the event.
        lookup_service.invalidate_lookup_cache()

        self.last_event_at = datetime.now(timezone.utc)
        log.info(
            "Adding→NameBot sync applied events=%s delete=%s engine=%s",
            len(events),
            has_delete,
            settings.lookup_engine_mode,
        )

    async def poll_once(self) -> bool:
        version = await self._state_version()
        if version <= self.last_version:
            return False

        # If the consumer was offline long enough for the TTL event log to lose
        # its history, do not guess what changed: rebuild from MongoDB.
        oldest = await get_db()[EVENT_COLLECTION].find_one(
            {}, sort=[("version", 1)], projection={"version": 1}
        )
        oldest_version = int((oldest or {}).get("version", version))
        if self.last_version and oldest_version > self.last_version + 1:
            log.warning(
                "Adding sync event gap detected last=%s oldest=%s current=%s; rebuilding",
                self.last_version,
                oldest_version,
                version,
            )
            if settings.lookup_engine_mode == "sqlite":
                await sqlite_index.build_full(clear_existing=True)
            else:
                await snapshot.refresh()
            self.last_version = version
            return True

        cursor = get_db()[EVENT_COLLECTION].find(
            {"version": {"$gt": self.last_version, "$lte": version}},
            projection={"version": 1, "operation": 1, "collection": 1, "document_id": 1},
            sort=[("version", 1)],
        )
        events = await cursor.to_list(length=max(1000, settings.snapshot_batch_size * 10))
        if not events:
            # State was advanced but event insertion failed. Rebuild rather than
            # returning stale lookup results.
            log.warning("Adding sync state advanced without events; rebuilding")
            if settings.lookup_engine_mode == "sqlite":
                await sqlite_index.build_full(clear_existing=True)
            else:
                await snapshot.refresh()
            self.last_version = version
            return True

        await self._sync_after_events(events)
        self.last_version = max(int(event.get("version", 0)) for event in events)
        return True

    async def run(self) -> None:
        await self.ensure_indexes()
        # Never trust an old local cursor after process restart. The current
        # MongoDB state is authoritative and the normal startup load already
        # populated the selected lookup engine.
        self.last_version = 0
        while True:
            try:
                await asyncio.sleep(max(1, int(os.getenv("ADDING_DATA_SYNC_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)) or DEFAULT_POLL_SECONDS))
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Adding→NameBot data sync loop failed")
                await asyncio.sleep(max(2, DEFAULT_POLL_SECONDS))


adding_data_sync = AddingDataSync()

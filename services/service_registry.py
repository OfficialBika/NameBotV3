from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from database.mongo import get_db

log = logging.getLogger(__name__)

SERVICE_ID = "namebotv3"
HEARTBEAT_SECONDS = max(5, int(os.getenv("SERVICE_HEARTBEAT_SECONDS", "15") or 15))
VERSION = os.getenv("SERVICE_VERSION", "main").strip() or "main"
GIT_COMMIT = os.getenv("GIT_COMMIT", "").strip()


async def publish_status(*, lookup_stats: dict[str, Any] | None = None) -> None:
    """Publish a small runtime contract so sibling services can detect NameBot updates."""
    now = datetime.now(timezone.utc)
    doc = {
        "service": SERVICE_ID,
        "version": VERSION,
        "git_commit": GIT_COMMIT,
        "status": "ready",
        "lookup_engine": (lookup_stats or {}).get("mode"),
        "lookup_stats": lookup_stats or {},
        "heartbeat_at": now,
        "heartbeat_unix": int(time.time()),
        "pid": os.getpid(),
        "updated_at": now,
    }
    await get_db()["service_registry"].update_one(
        {"_id": SERVICE_ID},
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def heartbeat_loop(lookup_backend) -> None:
    """Keep the service registry fresh; failures never stop the lookup service."""
    while True:
        try:
            stats = await lookup_backend.stats()
            await publish_status(lookup_stats=stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("NameBot service registry heartbeat failed")
        await asyncio.sleep(HEARTBEAT_SECONDS)

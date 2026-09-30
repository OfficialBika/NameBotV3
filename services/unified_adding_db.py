from __future__ import annotations

import logging
from typing import Any, Iterable

from config import COLLECTION_TO_OUTPUT_COMMAND, settings
from database.mongo import get_db

log = logging.getLogger(__name__)


class UnifiedAddingDB:
    """Read-only adapter for Adding-Helperbot's unified Mongo collection.

    Adding-Helperbot stores every lookup record in one physical collection
    (normally "characters") and separates sources with "source_key".
    NameBotV3 keeps its existing logical collection names, but maps them to
    source_key values here so the rest of the lookup engine stays unchanged.

    This adapter never inserts, updates, deletes, or creates Mongo indexes.
    """

    def __init__(self) -> None:
        self.collection_name = (
            getattr(settings, "unified_adding_collection", "characters").strip()
            or "characters"
        )
        self._enabled = False
        self._checked = False
        self._db = None
        self.db_name = ""

    async def detect(self, *, force: bool = False) -> bool:
        if self._checked and not force:
            return self._enabled

        self._enabled = False
        self._db = None
        self.db_name = ""

        try:
            enabled_by_config = bool(
                getattr(settings, "unified_adding_db_enabled", True)
            )
            if not enabled_by_config:
                self._checked = True
                log.info("Unified Adding DB adapter disabled by configuration")
                return False

            base_db = get_db()
            configured_name = str(
                getattr(settings, "unified_adding_db_name", "") or ""
            ).strip()

            # Explicit database name is authoritative. Without it, first inspect
            # the current DB, then safely discover another DB on the same Mongo
            # server that actually contains the unified characters + source_key data.
            if configured_name:
                candidate_names = [configured_name]
            else:
                candidate_names = [settings.db_name]
                try:
                    for name in await base_db.client.list_database_names():
                        if name not in candidate_names:
                            candidate_names.append(name)
                except Exception as exc:
                    log.info("Unified Adding DB auto-discovery list failed: %s", exc)

            known_sources = set(COLLECTION_TO_OUTPUT_COMMAND) - {"items_unknown"}

            # When DB_NAME is not explicit, several databases may contain a
            # collection named "characters". Never blindly take the first one:
            # an older/stale NameBot database can contain a few source records
            # while the real Adding DB contains the complete unified dataset.
            # Rank candidates by source coverage and document count, then use the
            # best candidate as the read-only source of truth.
            candidates: list[tuple[int, int, str]] = []

            for db_name in candidate_names:
                target_db = base_db.client[db_name]
                try:
                    names = await target_db.list_collection_names()
                except Exception:
                    continue
                if self.collection_name not in names:
                    continue

                collection = target_db[self.collection_name]

                if not configured_name:
                    try:
                        source_rows = await collection.aggregate([
                            {"$match": {"source_key": {"$in": sorted(known_sources)}}},
                            {"$group": {"_id": "$source_key", "n": {"$sum": 1}}},
                            {"$group": {"_id": None, "source_count": {"$sum": 1}, "known_docs": {"$sum": "$n"}}},
                        ], maxTimeMS=max(100, settings.mongo_exact_query_timeout_ms)).to_list(length=1)
                        stats = source_rows[0] if source_rows else {}
                        source_count = int(stats.get("source_count", 0) or 0)
                        known_docs = int(stats.get("known_docs", 0) or 0)
                    except Exception:
                        source_count = 0
                        known_docs = 0

                    if source_count <= 0:
                        continue

                    try:
                        total_docs = int(await collection.estimated_document_count())
                    except Exception:
                        total_docs = known_docs

                    candidates.append((source_count, total_docs, db_name))
                    log.info(
                        "Unified Adding DB candidate db=%s collection=%s known_sources=%s known_docs=%s total_docs=%s",
                        db_name,
                        self.collection_name,
                        source_count,
                        known_docs,
                        total_docs,
                    )
                    continue

                # Explicit UNIFIED_ADDING_DB_NAME is authoritative.
                self._db = target_db
                self.db_name = db_name
                self._enabled = True
                self._checked = True
                log.info(
                    "Unified Adding DB detected db=%s collection=%s (read-only source of truth)",
                    db_name,
                    self.collection_name,
                )
                return True

            if candidates:
                source_count, total_docs, db_name = max(
                    candidates,
                    key=lambda row: (row[0], row[1]),
                )
                self._db = base_db.client[db_name]
                self.db_name = db_name
                self._enabled = True
                self._checked = True
                log.info(
                    "Unified Adding DB selected db=%s collection=%s known_sources=%s total_docs=%s (read-only source of truth)",
                    db_name,
                    self.collection_name,
                    source_count,
                    total_docs,
                )
                return True

            self._checked = True
            log.info(
                "Unified Adding DB not detected collection=%s; using legacy collection mode",
                self.collection_name,
            )
        except Exception as exc:
            self._checked = True
            log.warning("Unified Adding DB detection failed: %s", exc)
        return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def scoped_query(
        self,
        query: dict[str, Any] | None = None,
        source_keys: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        base = dict(query or {})
        selected = [
            str(value).strip().lower()
            for value in (source_keys or [])
            if str(value).strip()
        ]
        selected = list(dict.fromkeys(selected))
        if not selected:
            return base

        if len(selected) == 1:
            source_filter: dict[str, Any] = {"source_key": selected[0]}
        else:
            source_filter = {"source_key": {"$in": selected}}

        if not base:
            return source_filter
        return {"$and": [source_filter, base]}

    def source_key(self, doc: dict[str, Any], fallback: str | None = None) -> str:
        value = str(doc.get("source_key") or "").strip().lower()
        return value or str(fallback or "").strip().lower()

    def collection(self):
        # PyMongo Database objects intentionally do not support truth-value testing.
        # Compare with None explicitly so unified lookups never fail here.
        db = self._db if self._db is not None else get_db()
        return db[self.collection_name]


unified_adding_db = UnifiedAddingDB()

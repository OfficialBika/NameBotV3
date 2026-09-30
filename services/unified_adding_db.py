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
        """Bind to the canonical Adding DB without touching its documents.

        The unified schema is:
          database.<characters collection>
              source_key
              name
              file_unique_ids[]
              telegram_file_unique_id
              sha256 / sha256_aliases
              photo/video fingerprint fields
              source_origin
              updated_at

        An explicit UNIFIED_ADDING_DB_NAME (or ADDING_DB_NAME/DB_NAME fallback)
        is authoritative. Auto-discovery is only used when no database name is
        configured at all.
        """
        if self._checked and not force:
            return self._enabled

        self._enabled = False
        self._db = None
        self.db_name = ""

        try:
            if not bool(getattr(settings, "unified_adding_db_enabled", True)):
                self._checked = True
                log.info("Unified Adding DB adapter disabled by configuration")
                return False

            base_db = get_db()
            configured_name = str(
                getattr(settings, "unified_adding_db_name", "") or ""
            ).strip()

            if configured_name:
                candidate_names = [configured_name]
            else:
                # First probe the configured DB directly. This avoids relying on
                # MongoDB's listDatabaseNames/listCollections privileges when the
                # Adding and NameBot services intentionally share the same DB.
                candidate_names = [settings.db_name]

            known_sources = set(COLLECTION_TO_OUTPUT_COMMAND) - {"items_unknown"}
            candidates: list[tuple[int, int, str]] = []

            for db_name in candidate_names:
                target_db = base_db.client[db_name]
                collection = target_db[self.collection_name]

                # Explicit DB names are authoritative. Do not silently fall back to
                # legacy collections just because MongoDB hides listCollections.
                if configured_name:
                    try:
                        collections = await target_db.list_collection_names()
                        if self.collection_name not in collections:
                            log.error(
                                "Unified Adding DB missing collection db=%s collection=%s",
                                db_name,
                                self.collection_name,
                            )
                    except Exception as exc:
                        log.warning(
                            "Unified Adding DB collection listing unavailable db=%s: %s",
                            db_name,
                            exc,
                        )

                    self._db = target_db
                    self.db_name = db_name
                    self._enabled = True
                    self._checked = True
                    await self._log_schema_state()
                    return True

                if not configured_name and db_name == settings.db_name:
                    try:
                        sample = await collection.find_one(
                            {
                                "$or": [
                                    {"source_key": {"$in": sorted(known_sources)}},
                                    {"file_unique_ids": {"$exists": True}},
                                    {"telegram_file_unique_id": {"$exists": True}},
                                ]
                            },
                            projection={
                                "_id": 1,
                                "source_key": 1,
                                "file_unique_ids": 1,
                                "telegram_file_unique_id": 1,
                            },
                            max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
                        )
                    except Exception as exc:
                        sample = None
                        log.warning(
                            "Unified Adding DB direct probe failed db=%s collection=%s error=%s",
                            db_name,
                            self.collection_name,
                            exc,
                        )

                    if sample:
                        self._db = target_db
                        self.db_name = db_name
                        self._enabled = True
                        self._checked = True
                        log.info(
                            "Unified Adding DB bound to current DB db=%s collection=%s "
                            "via canonical schema probe",
                            db_name,
                            self.collection_name,
                        )
                        await self._log_schema_state()
                        return True

                try:
                    source_rows = await collection.aggregate(
                        [
                            {"$match": {"source_key": {"$in": sorted(known_sources)}}},
                            {
                                "$group": {
                                    "_id": "$source_key",
                                    "n": {"$sum": 1},
                                }
                            },
                            {
                                "$group": {
                                    "_id": None,
                                    "source_count": {"$sum": 1},
                                    "known_docs": {"$sum": "$n"},
                                }
                            },
                        ],
                        maxTimeMS=max(100, settings.mongo_exact_query_timeout_ms),
                    ).to_list(length=1)
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
                    "Unified Adding DB selected db=%s collection=%s known_sources=%s total_docs=%s",
                    db_name,
                    self.collection_name,
                    source_count,
                    total_docs,
                )
                await self._log_schema_state()
                return True

            self._checked = True
            log.error(
                "Unified Adding DB NOT FOUND collection=%s configured_db=%s; "
                "legacy lookup mode would miss the canonical characters collection",
                self.collection_name,
                configured_name or settings.db_name,
            )
        except Exception as exc:
            self._checked = True
            log.exception("Unified Adding DB detection failed: %s", exc)
        return False

    async def _log_schema_state(self) -> None:
        """Log the canonical collection/index state for deployment diagnostics."""
        if not self._db:
            return
        try:
            collection = self._db[self.collection_name]
            sample = await collection.find_one(
                {
                    "$or": [
                        {"file_unique_ids": {"$exists": True}},
                        {"telegram_file_unique_id": {"$exists": True}},
                        {"source_key": {"$exists": True}},
                    ]
                },
                projection={
                    "_id": 1,
                    "source_key": 1,
                    "name": 1,
                    "file_unique_ids": 1,
                    "telegram_file_unique_id": 1,
                    "sha256": 1,
                    "updated_at": 1,
                },
                max_time_ms=max(100, settings.mongo_exact_query_timeout_ms),
            )
            try:
                index_names = []
                async for info in collection.list_indexes():
                    name = info.get("name")
                    if name:
                        index_names.append(str(name))
                index_names.sort()
            except Exception as exc:
                index_names = [f"<index-list-failed:{exc}>"]

            required = {
                "idx_source_file_uid",
                "idx_global_file_uid",
                "idx_source_telegram_file_uid",
                "idx_global_telegram_file_uid",
                "idx_source_sha256",
                "idx_global_sha256",
                "idx_updated_at",
            }
            missing = sorted(required.difference(index_names))
            if sample:
                log.info(
                    "Unified schema OK db=%s collection=%s sample_source=%s sample_name=%s "
                    "uid_count=%s indexes=%s missing=%s",
                    self.db_name,
                    self.collection_name,
                    sample.get("source_key"),
                    sample.get("name"),
                    len(sample.get("file_unique_ids") or []),
                    len(index_names),
                    missing,
                )
            else:
                log.warning(
                    "Unified schema EMPTY db=%s collection=%s indexes=%s missing=%s",
                    self.db_name,
                    self.collection_name,
                    len(index_names),
                    missing,
                )
        except Exception as exc:
            log.warning(
                "Unified schema inspection failed db=%s collection=%s error=%s",
                self.db_name,
                self.collection_name,
                exc,
            )

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

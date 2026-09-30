from __future__ import annotations

import logging
from typing import Any, Iterable

from config import settings
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

    async def detect(self, *, force: bool = False) -> bool:
        if self._checked and not force:
            return self._enabled

        try:
            enabled_by_config = bool(
                getattr(settings, "unified_adding_db_enabled", True)
            )
            if not enabled_by_config:
                self._enabled = False
                self._checked = True
                log.info("Unified Adding DB adapter disabled by configuration")
                return False

            db = get_db()
            names = await db.list_collection_names()
            self._enabled = self.collection_name in names
            self._checked = True
            if self._enabled:
                log.info(
                    "Unified Adding DB detected collection=%s (read-only source of truth)",
                    self.collection_name,
                )
            else:
                log.info(
                    "Unified Adding DB not detected collection=%s; using legacy collection mode",
                    self.collection_name,
                )
        except Exception as exc:
            self._enabled = False
            self._checked = True
            log.warning("Unified Adding DB detection failed: %s", exc)
        return self._enabled

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
        return get_db()[self.collection_name]


unified_adding_db = UnifiedAddingDB()

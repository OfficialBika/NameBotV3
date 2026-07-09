from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from config import COLLECTION_TO_OUTPUT_COMMAND, settings
from database.mongo import get_db
from services.hash_service import VideoSampleHash
from utils.text import normalize_name

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItemSnapshot:
    mongo_id: str
    collection: str
    command: str
    name: str
    card_id: str | int | None = None
    rarity: str | None = None
    anime_name: str | None = None
    media_type: str | None = None
    item_key: str | None = None
    name_aliases: tuple[str, ...] = ()
    file_unique_id: str | None = None
    file_unique_ids: tuple[str, ...] = ()
    sha256: str | None = None
    sha256_aliases: tuple[str, ...] = ()
    phash: str | None = None
    pixel_sha256: str | None = None
    phash_large: str | None = None
    dhash: str | None = None
    whash: str | None = None
    colorhash: str | None = None
    crop_hash: str | None = None
    frame_hashes: tuple[str, ...] = ()
    video_samples: tuple[VideoSampleHash, ...] = ()
    video_signature: str | None = None
    duration_ms: int = 0
    fps: float = 0.0
    frame_count: int = 0
    width: int = 0
    height: int = 0
    origin_chat_id: int | None = None
    origin_message_id: int | None = None
    archive_chat_id: int | None = None
    archive_message_id: int | None = None
    fingerprint_version: str | None = None

    @property
    def is_waifux(self) -> bool:
        return self.collection == "items_waifux_grab"

    @property
    def all_uids(self) -> tuple[str, ...]:
        values = list(self.file_unique_ids)
        if self.file_unique_id and self.file_unique_id not in values:
            values.append(self.file_unique_id)
        return tuple(values)

    @property
    def all_shas(self) -> tuple[str, ...]:
        values = list(self.sha256_aliases)
        if self.sha256 and self.sha256 not in values:
            values.append(self.sha256)
        return tuple(values)


class HashChunkIndex:
    """Pure-Python multi-index hashing candidate index.

    Splitting a 64-bit hash into threshold+1 chunks guarantees that two hashes
    within the threshold share at least one exact chunk (pigeonhole principle).
    """

    COUNTS = (9, 13, 17)

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, dict[int, dict[tuple[int, int], set[str]]]]] = {}

    @staticmethod
    def _chunks(value: str, count: int) -> tuple[int, ...] | None:
        try:
            text = str(value).strip().lower()
            number = int(text, 16)
            bits = len(text) * 4
        except Exception:
            return None
        if bits <= 0 or count <= 0 or count > bits:
            return None
        base, extra = divmod(bits, count)
        sizes = [base + (1 if i < extra else 0) for i in range(count)]
        out: list[int] = []
        consumed = 0
        for size in sizes:
            shift = bits - consumed - size
            mask = (1 << size) - 1
            out.append((number >> shift) & mask)
            consumed += size
        return tuple(out)

    def add(self, item: ItemSnapshot) -> None:
        for field_name, value in (("phash", item.phash), ("dhash", item.dhash)):
            if not value:
                continue
            for count in self.COUNTS:
                chunks = self._chunks(value, count)
                if not chunks:
                    continue
                bucket = self.buckets.setdefault(item.collection, {}).setdefault(field_name, {}).setdefault(count, {})
                for idx, chunk in enumerate(chunks):
                    bucket.setdefault((idx, chunk), set()).add(item.mongo_id)

    def remove(self, item: ItemSnapshot) -> None:
        for field_name, value in (("phash", item.phash), ("dhash", item.dhash)):
            if not value:
                continue
            for count in self.COUNTS:
                chunks = self._chunks(value, count)
                if not chunks:
                    continue
                bucket = self.buckets.get(item.collection, {}).get(field_name, {}).get(count, {})
                for idx, chunk in enumerate(chunks):
                    ids = bucket.get((idx, chunk))
                    if ids:
                        ids.discard(item.mongo_id)
                        if not ids:
                            bucket.pop((idx, chunk), None)

    def candidates(self, collections: Iterable[str], field_name: str, value: str | None, threshold: int) -> set[str]:
        if not value:
            return set()
        count = min(self.COUNTS, key=lambda n: abs(n - (threshold + 1)))
        chunks = self._chunks(value, count)
        if not chunks:
            return set()
        result: set[str] = set()
        for collection in collections:
            bucket = self.buckets.get(collection, {}).get(field_name, {}).get(count, {})
            for idx, chunk in enumerate(chunks):
                result.update(bucket.get((idx, chunk), ()))
        return result



def _nested(doc: dict, path: str):
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_present(doc: dict, names: Iterable[str]):
    for name in names:
        value = _nested(doc, name) if "." in name else doc.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _clean(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _strings(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for raw in value:
            text = _clean(raw)
            if text and text not in out:
                out.append(text)
        return tuple(out)
    return ()


def _frame_hashes(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    out: list[str] = []
    if isinstance(value, (list, tuple)):
        for raw in value:
            if isinstance(raw, dict):
                raw = raw.get("hash") or raw.get("phash") or raw.get("value")
            text = _clean(raw)
            if text:
                out.append(text)
    return tuple(out)


def _video_samples(value) -> tuple[VideoSampleHash, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[VideoSampleHash] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        try:
            position = float(raw.get("position", 0.0))
            frame_index = int(raw.get("frame_index", 0) or 0)
        except Exception:
            continue
        phash = _clean(raw.get("phash"))
        dhash = _clean(raw.get("dhash"))
        if phash and dhash:
            out.append(VideoSampleHash(position, frame_index, phash, dhash))
    return tuple(out)


NAME_FIELDS = ["name", "character_name", "char_name", "item_name", "card_name", "display_name", "title", "media.name", "character.name"]
ANIME_FIELDS = ["anime_name", "anime", "series", "movie", "category", "media.series", "character.series"]
ID_FIELDS = ["card_id", "id", "item_id", "char_id", "character_id", "media.id", "character.id"]
RARITY_FIELDS = ["rarity", "rank", "tier", "class", "media.rarity", "character.rarity"]
FILE_UID_FIELDS = ["file_unique_id", "photo_file_unique_id", "video_file_unique_id", "media.file_unique_id", "file.unique_id"]
SHA_FIELDS = ["sha256", "media_sha256", "hash", "file_hash", "media.sha256", "file.sha256"]
PHASH_FIELDS = ["photo_fingerprint.phash", "phash", "photo_phash", "image_phash", "media.phash", "file.phash"]
FRAME_HASH_FIELDS = ["frame_hashes", "video_frame_hashes", "frames", "media.frame_hashes", "file.frame_hashes"]

# Read only fields required by lookup. This materially reduces Mongo network traffic
# compared with loading complete Adding Bot documents (raw captions, archive extras, etc.).
LOOKUP_PROJECTION: dict[str, int] = {
    "name": 1,
    "character_name": 1,
    "char_name": 1,
    "item_name": 1,
    "card_name": 1,
    "display_name": 1,
    "title": 1,
    "anime_name": 1,
    "anime": 1,
    "series": 1,
    "movie": 1,
    "category": 1,
    "rarity": 1,
    "rank": 1,
    "tier": 1,
    "class": 1,
    "card_id": 1,
    "id": 1,
    "item_id": 1,
    "char_id": 1,
    "character_id": 1,
    "command_name": 1,
    "source_key": 1,
    "source_collection": 1,
    "item_key": 1,
    "name_aliases": 1,
    "media_type": 1,
    "type": 1,
    "file_unique_id": 1,
    "file_unique_ids": 1,
    "photo_file_unique_id": 1,
    "video_file_unique_id": 1,
    "sha256": 1,
    "sha256_aliases": 1,
    "media_sha256": 1,
    "hash": 1,
    "file_hash": 1,
    "phash": 1,
    "photo_phash": 1,
    "image_phash": 1,
    "frame_hashes": 1,
    "video_frame_hashes": 1,
    "frames": 1,
    "photo_fingerprint": 1,
    "video_fingerprint": 1,
    "media_geometry": 1,
    "source_origin": 1,
    "archive": 1,
    "archive_chat_id": 1,
    "archive_message_id": 1,
    "fingerprint_version": 1,
    "updated_at": 1,
    "media": 1,
    "file": 1,
    "character": 1,
}


def parse_item(collection: str, default_command: str, doc: dict) -> ItemSnapshot | None:
    name = normalize_name(_first_present(doc, NAME_FIELDS))
    if not name:
        return None
    photo_fp = doc.get("photo_fingerprint") if isinstance(doc.get("photo_fingerprint"), dict) else {}
    video_fp = doc.get("video_fingerprint") if isinstance(doc.get("video_fingerprint"), dict) else {}
    geometry = doc.get("media_geometry") if isinstance(doc.get("media_geometry"), dict) else {}
    source_origin = doc.get("source_origin") if isinstance(doc.get("source_origin"), dict) else {}
    archive = doc.get("archive") if isinstance(doc.get("archive"), dict) else {}

    phash = _clean(photo_fp.get("phash")) or _clean(_first_present(doc, PHASH_FIELDS))
    frames = _frame_hashes(_first_present(doc, FRAME_HASH_FIELDS))
    media_type = str(_first_present(doc, ["media_type", "type", "media.type", "file_type"]) or "").lower().strip()
    if media_type in {"image", "pic", "picture"}:
        media_type = "photo"
    elif media_type in {"animation", "gif"}:
        media_type = "video"
    elif not media_type:
        media_type = "video" if (frames or video_fp) else "photo" if (phash or photo_fp) else ""

    def _int_or_none(value):
        try:
            return int(value) if value is not None and value != "" else None
        except Exception:
            return None

    return ItemSnapshot(
        mongo_id=str(doc.get("_id")),
        collection=collection,
        command=_clean(doc.get("command_name")) or default_command,
        name=name,
        card_id=_clean(_first_present(doc, ID_FIELDS)),
        rarity=_clean(_first_present(doc, RARITY_FIELDS)),
        anime_name=_clean(_first_present(doc, ANIME_FIELDS)),
        media_type=media_type or None,
        item_key=_clean(doc.get("item_key")),
        name_aliases=_strings(doc.get("name_aliases")),
        file_unique_id=_clean(_first_present(doc, FILE_UID_FIELDS)),
        file_unique_ids=_strings(doc.get("file_unique_ids")),
        sha256=_clean(_first_present(doc, SHA_FIELDS)),
        sha256_aliases=_strings(doc.get("sha256_aliases")),
        phash=phash,
        pixel_sha256=_clean(photo_fp.get("pixel_sha256")),
        phash_large=_clean(photo_fp.get("phash_large")),
        dhash=_clean(photo_fp.get("dhash")),
        whash=_clean(photo_fp.get("whash")),
        colorhash=_clean(photo_fp.get("colorhash")),
        crop_hash=_clean(photo_fp.get("crop_hash")),
        frame_hashes=frames,
        video_samples=_video_samples(video_fp.get("sample_hashes")),
        video_signature=_clean(video_fp.get("video_signature")),
        duration_ms=int(video_fp.get("duration_ms") or geometry.get("duration_ms") or 0),
        fps=float(video_fp.get("fps") or geometry.get("fps") or 0.0),
        frame_count=int(video_fp.get("frame_count") or geometry.get("frame_count") or 0),
        width=int(video_fp.get("width") or geometry.get("width") or 0),
        height=int(video_fp.get("height") or geometry.get("height") or 0),
        origin_chat_id=_int_or_none(source_origin.get("chat_id")),
        origin_message_id=_int_or_none(source_origin.get("message_id")),
        archive_chat_id=_int_or_none(archive.get("chat_id") or doc.get("archive_chat_id")),
        archive_message_id=_int_or_none(archive.get("message_id") or doc.get("archive_message_id")),
        fingerprint_version=_clean(doc.get("fingerprint_version")),
    )


class SnapshotCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.loaded_at = 0.0
        self.count = 0
        self.last_incremental_sync_at: datetime | None = None
        self.last_full_refresh_monotonic = 0.0
        self.items_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.by_collection: dict[str, list[ItemSnapshot]] = {}
        self.file_uid: dict[str, ItemSnapshot] = {}
        self.sha256: dict[str, ItemSnapshot] = {}
        self.pixel_sha256: dict[str, ItemSnapshot] = {}
        self.video_signature: dict[str, ItemSnapshot] = {}
        self.origin: dict[tuple[int, int], ItemSnapshot] = {}
        self.file_uid_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.sha256_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.pixel_sha_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.video_signature_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.origin_by_collection: dict[str, dict[tuple[int, int], ItemSnapshot]] = {}
        self.photos_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.videos_by_collection: dict[str, dict[str, ItemSnapshot]] = {}
        self.video_duration_buckets: dict[str, dict[int, set[str]]] = {}
        self.chunk_index = HashChunkIndex()

    def _clear_indexes(self) -> None:
        self.by_collection = {}
        self.file_uid = {}
        self.sha256 = {}
        self.pixel_sha256 = {}
        self.video_signature = {}
        self.origin = {}
        self.file_uid_by_collection = {}
        self.sha256_by_collection = {}
        self.pixel_sha_by_collection = {}
        self.video_signature_by_collection = {}
        self.origin_by_collection = {}
        self.photos_by_collection = {}
        self.videos_by_collection = {}
        self.video_duration_buckets = {}
        self.chunk_index = HashChunkIndex()

    def _index_add(self, item: ItemSnapshot) -> None:
        collection = item.collection
        self.by_collection.setdefault(collection, []).append(item)
        uid_map = self.file_uid_by_collection.setdefault(collection, {})
        for uid in item.all_uids:
            self.file_uid.setdefault(uid, item)
            uid_map[uid] = item
        sha_map = self.sha256_by_collection.setdefault(collection, {})
        for sha in item.all_shas:
            self.sha256.setdefault(sha, item)
            sha_map[sha] = item
        if item.pixel_sha256:
            self.pixel_sha256.setdefault(item.pixel_sha256, item)
            self.pixel_sha_by_collection.setdefault(collection, {})[item.pixel_sha256] = item
        if item.video_signature:
            self.video_signature.setdefault(item.video_signature, item)
            self.video_signature_by_collection.setdefault(collection, {})[item.video_signature] = item
        if item.origin_chat_id is not None and item.origin_message_id is not None:
            key = (item.origin_chat_id, item.origin_message_id)
            self.origin.setdefault(key, item)
            self.origin_by_collection.setdefault(collection, {})[key] = item
        if item.media_type == "photo" or item.phash or item.dhash:
            self.photos_by_collection.setdefault(collection, {})[item.mongo_id] = item
            self.chunk_index.add(item)
        if item.media_type == "video" or item.frame_hashes or item.video_samples:
            self.videos_by_collection.setdefault(collection, {})[item.mongo_id] = item
            if item.duration_ms > 0:
                second = int(round(item.duration_ms / 1000))
                self.video_duration_buckets.setdefault(collection, {}).setdefault(second, set()).add(item.mongo_id)

    def _rebuild_indexes(self) -> None:
        self._clear_indexes()
        for collection, items in self.items_by_collection.items():
            for item in items.values():
                self._index_add(item)
            self.by_collection.setdefault(collection, [])
            self.file_uid_by_collection.setdefault(collection, {})
            self.sha256_by_collection.setdefault(collection, {})
            self.pixel_sha_by_collection.setdefault(collection, {})
            self.video_signature_by_collection.setdefault(collection, {})
            self.origin_by_collection.setdefault(collection, {})
            self.photos_by_collection.setdefault(collection, {})
            self.videos_by_collection.setdefault(collection, {})

    async def refresh(self) -> None:
        db = get_db()
        refresh_started_at = datetime.now(timezone.utc)
        new_items: dict[str, dict[str, ItemSnapshot]] = {}
        total = 0
        failed_collections: list[str] = []
        for collection, default_command in COLLECTION_TO_OUTPUT_COMMAND.items():
            collection_items: dict[str, ItemSnapshot] = {}
            try:
                cursor = db[collection].find({}, projection=LOOKUP_PROJECTION).batch_size(max(1, settings.snapshot_batch_size))
                async for doc in cursor:
                    item = parse_item(collection, default_command, doc)
                    if item:
                        collection_items[item.mongo_id] = item
            except Exception:
                failed_collections.append(collection)
                log.exception("snapshot load failed for %s", collection)
                # Never erase a previously healthy collection because of a transient Mongo timeout.
                collection_items = dict(self.items_by_collection.get(collection, {}))
            new_items[collection] = collection_items
            total += len(collection_items)

        async with self._lock:
            self.items_by_collection = new_items
            self._rebuild_indexes()
            self.loaded_at = time.time()
            self.count = total
            if not failed_collections:
                self.last_incremental_sync_at = refresh_started_at
            self.last_full_refresh_monotonic = time.monotonic()
        if failed_collections:
            log.warning("V3 snapshot refresh partial items=%s failed_collections=%s", total, failed_collections)
        else:
            log.info("V3 snapshot refreshed: %s items", total)

    async def incremental_sync(self) -> int:
        if self.last_incremental_sync_at is None:
            await self.refresh()
            return self.count
        db = get_db()
        start_watermark = self.last_incremental_sync_at
        next_watermark = datetime.now(timezone.utc)
        changed: list[ItemSnapshot] = []
        failed = False
        for collection, default_command in COLLECTION_TO_OUTPUT_COMMAND.items():
            try:
                cursor = db[collection].find(
                    {"updated_at": {"$gt": start_watermark, "$lte": next_watermark}},
                    projection=LOOKUP_PROJECTION,
                ).batch_size(max(1, settings.snapshot_batch_size))
                async for doc in cursor:
                    item = parse_item(collection, default_command, doc)
                    if item:
                        changed.append(item)
            except Exception:
                failed = True
                log.exception("incremental snapshot sync failed for %s", collection)
        if not changed:
            if not failed:
                self.last_incremental_sync_at = next_watermark
            return 0
        async with self._lock:
            for item in changed:
                self.items_by_collection.setdefault(item.collection, {})[item.mongo_id] = item
            # Rebuild from RAM only. This avoids another Mongo full scan while keeping all alias indexes consistent.
            self._rebuild_indexes()
            self.count = sum(len(items) for items in self.items_by_collection.values())
            self.loaded_at = time.time()
            if not failed:
                self.last_incremental_sync_at = next_watermark
        log.info("V3 incremental sync: %s changed items failed=%s", len(changed), failed)
        return len(changed)

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(max(2, settings.snapshot_incremental_sync_seconds))
            try:
                if time.monotonic() - self.last_full_refresh_monotonic >= max(60, settings.snapshot_full_rebuild_seconds):
                    await self.refresh()
                else:
                    await self.incremental_sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("snapshot V3 refresh loop failed")

    def age_seconds(self) -> int:
        return int(time.time() - self.loaded_at) if self.loaded_at else -1

    def exact_uid(self, uid: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not uid:
            return None
        if collections:
            for collection in collections:
                item = self.file_uid_by_collection.get(collection, {}).get(uid)
                if item:
                    return item
            return None
        return self.file_uid.get(uid)

    def exact_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not sha:
            return None
        if collections:
            for collection in collections:
                item = self.sha256_by_collection.get(collection, {}).get(sha)
                if item:
                    return item
            return None
        return self.sha256.get(sha)

    def exact_pixel_sha(self, sha: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not sha:
            return None
        if collections:
            for collection in collections:
                item = self.pixel_sha_by_collection.get(collection, {}).get(sha)
                if item:
                    return item
            return None
        return self.pixel_sha256.get(sha)

    def exact_video_signature(self, signature: str, collections: list[str] | None = None) -> ItemSnapshot | None:
        if not signature:
            return None
        if collections:
            for collection in collections:
                item = self.video_signature_by_collection.get(collection, {}).get(signature)
                if item:
                    return item
            return None
        return self.video_signature.get(signature)

    def exact_origin(self, key: tuple[int, int], collections: list[str] | None = None) -> ItemSnapshot | None:
        if collections:
            for collection in collections:
                item = self.origin_by_collection.get(collection, {}).get(key)
                if item:
                    return item
            return None
        return self.origin.get(key)

    def photo_candidates(self, collections: list[str] | None, phash: str | None, dhash: str | None, phash_threshold: int, dhash_threshold: int, max_candidates: int) -> list[ItemSnapshot]:
        selected = collections or list(COLLECTION_TO_OUTPUT_COMMAND.keys())
        ids = self.chunk_index.candidates(selected, "phash", phash, phash_threshold)
        ids.update(self.chunk_index.candidates(selected, "dhash", dhash, dhash_threshold))
        out: list[ItemSnapshot] = []
        for collection in selected:
            mapping = self.photos_by_collection.get(collection, {})
            for item_id in ids:
                item = mapping.get(item_id)
                if item:
                    out.append(item)
                    if len(out) >= max_candidates:
                        return out
        # Backward compatibility: old documents may only have pHash and chunk index still covers them.
        # When query hashes cannot be indexed, scoped fallback scan is allowed.
        if not out and collections:
            for collection in selected:
                out.extend(self.photos_by_collection.get(collection, {}).values())
                if len(out) >= max_candidates:
                    return out[:max_candidates]
        return out[:max_candidates]

    def video_candidates(self, collections: list[str] | None, duration_ms: int, tolerance_seconds: int) -> list[ItemSnapshot]:
        selected = collections or list(COLLECTION_TO_OUTPUT_COMMAND.keys())
        out: list[ItemSnapshot] = []
        seen: set[str] = set()
        if duration_ms > 0:
            sec = int(round(duration_ms / 1000))
            for collection in selected:
                mapping = self.videos_by_collection.get(collection, {})
                buckets = self.video_duration_buckets.get(collection, {})
                for candidate_sec in range(max(0, sec - tolerance_seconds), sec + tolerance_seconds + 1):
                    for item_id in buckets.get(candidate_sec, ()):
                        if item_id not in seen and item_id in mapping:
                            out.append(mapping[item_id])
                            seen.add(item_id)
        if out:
            return out
        for collection in selected:
            for item in self.videos_by_collection.get(collection, {}).values():
                if item.mongo_id not in seen:
                    out.append(item)
                    seen.add(item.mongo_id)
        return out


snapshot = SnapshotCache()

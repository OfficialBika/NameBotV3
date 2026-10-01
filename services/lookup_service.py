from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass, replace

from aiogram import Bot
from aiogram.types import Message

from config import settings
from services.hash_service import MediaHash, hamming_hex, hash_photo, hash_video, normalized_hamming, sha256_bytes
from services.lookup_backend import lookup_backend
from services.sqlite_fingerprint_index import sqlite_index
from services.snapshot_cache import ItemSnapshot
from services.source_resolver import (
    is_character_catcher_spawn,
    output_command_from_message,
    resolve_lookup_scope,
    source_origin_key,
)
from utils.media import extract_media
from utils.perf import perf
from utils.ttl_cache import TTLCache

try:
    from services.source_blocker import is_blocked_source
except Exception:  # pragma: no cover
    def is_blocked_source(message: Message | None) -> bool:
        return False

log = logging.getLogger(__name__)


def _telegram_file_ids(message: Message, media) -> list[str]:
    values: list[str] = []
    if media.media_type == "photo":
        for photo in (getattr(message, "photo", None) or []):
            file_id = str(getattr(photo, "file_id", "") or "").strip()
            if file_id and file_id not in values:
                values.append(file_id)
    file_id = str(getattr(media.obj, "file_id", "") or "").strip()
    if file_id and file_id not in values:
        values.append(file_id)
    return values


CATCH_CHARACTER_ID_RE = re.compile(
    r"(?:^|[\n\r])\s*(\d+)\s*:\s*.+?(?=$|[\n\r])"
    r"|(?:character\s*)?(?:id|item\s*id|card\s*id)\s*[:#\-]?\s*(\d+)",
    re.I | re.M,
)


def _catch_character_id(message: Message) -> str | None:
    parts: list[str] = []
    for obj in (
        message,
        getattr(message, "external_reply", None),
        getattr(message, "reply_to_message", None),
    ):
        if obj is None:
            continue
        for attr in ("caption", "text", "html_text", "md_text"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    text = "\n".join(parts)
    match = CATCH_CHARACTER_ID_RE.search(text)
    if not match:
        return None
    return (match.group(1) or match.group(2) or "").strip() or None
def _telegram_uids(message: Message, media) -> list[str]:
    """Return every native Telegram file_unique_id exposed by the media."""
    values: list[str] = []
    if media.media_type == "photo":
        for photo in (getattr(message, "photo", None) or []):
            uid = str(getattr(photo, "file_unique_id", "") or "").strip()
            if uid and uid not in values:
                values.append(uid)
    uid = str(getattr(media.obj, "file_unique_id", "") or "").strip()
    if uid and uid not in values:
        values.append(uid)
    return values


@dataclass(frozen=True)
class LookupResult:
    item: ItemSnapshot | None
    reason: str = ""
    elapsed_ms: float = 0.0
    confidence: float = 0.0


class LookupService:
    """V3 source-aware, alias-aware, multi-fingerprint lookup engine."""

    def __init__(self) -> None:
        # RAM L1 stores only Telegram UID -> character name.
        self.uid_name_cache: TTLCache[str, str] = TTLCache(
            settings.result_cache_max_items, settings.result_cache_ttl_seconds
        )
        self.download_sem = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.lookup_sem = asyncio.Semaphore(settings.max_concurrent_lookups)
        # OpenCV/Pillow hashing can temporarily allocate large decoded pixel buffers.
        # Keep that heavy media stage bounded independently from lightweight exact UID
        # lookups so a burst of misses cannot exhaust the 512 MB Render instance.
        self.hash_sem = asyncio.Semaphore(
            max(1, min(2, settings.max_concurrent_lookups))
        )
        # Share only currently-running lookups for the same source media. This is
        # an in-flight singleflight map, not a persistent/result cache, so RAM is
        # released as soon as the request completes.
        self._inflight: dict[str, asyncio.Task[LookupResult]] = {}

    @staticmethod
    def _inflight_key(message: Message) -> str | None:
        media = extract_media(message)
        if not media:
            return None
        source = media.source_message
        uids = _telegram_uids(source, media)
        origin = source_origin_key(source)
        if origin:
            identity = f"origin:{origin[0]}:{origin[1]}"
        else:
            identity = f"chat:{getattr(source, 'chat', None).id if getattr(source, 'chat', None) else ''}:msg:{getattr(source, 'message_id', None)}"
        uid_part = ",".join(uids)
        file_id = str(getattr(media.obj, "file_id", "") or "").strip()
        return f"{identity}|{media.media_type}|uids:{uid_part}|fid:{file_id}"

    async def lookup_message(self, bot: Bot, message: Message, *, manual: bool = False) -> LookupResult:
        key = self._inflight_key(message)
        if key:
            existing = self._inflight.get(key)
            if existing is not None:
                log.info(
                    "LOOKUP SINGLEFLIGHT join message=%s key=%s",
                    getattr(message, "message_id", None),
                    key,
                )
                return await asyncio.shield(existing)

            task = asyncio.create_task(
                self._lookup_message_impl(bot, message, manual=manual),
                name=f"lookup:{key}",
            )
            self._inflight[key] = task
            try:
                return await asyncio.shield(task)
            finally:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

        return await self._lookup_message_impl(bot, message, manual=manual)

    async def _lookup_message_impl(self, bot: Bot, message: Message, *, manual: bool = False) -> LookupResult:
        started = time.perf_counter()
        hit = False
        error = False
        try:
            async with self.lookup_sem:
                media = extract_media(message)
                if not media:
                    return self._done(None, "no_media", started)
                source_message = media.source_message
                if is_blocked_source(source_message) or (manual and is_blocked_source(message)):
                    return self._done(None, "blocked_source", started)

                scope = resolve_lookup_scope(source_message)
                if scope.mode == "blocked":
                    return self._done(None, "blocked_source", started)
                if manual and not scope.collections:
                    manual_scope = resolve_lookup_scope(message)
                    if manual_scope.collections:
                        scope = manual_scope

                collections = scope.collections
                # One canonical fallback pipeline is shared by Auto and Manual:
                # source UID -> global UID -> source hash/similarity -> global hash/similarity.
                # An unknown source therefore proceeds directly to the global stages.
                lookup_collections = collections if collections is not None else None
                filter_tag = self._filter_tag(collections)
                output_command = output_command_from_message(
                    source_message,
                    collections[0] if collections and len(collections) == 1 else None,
                )

                file_uids = _telegram_uids(source_message, media)
                file_uid = file_uids[0] if file_uids else ""
                log.info(
                    "UID DEBUG message=%s source_message=%s media_type=%s collections=%s uids=%s",
                    getattr(message, "message_id", None),
                    getattr(source_message, "message_id", None),
                    media.media_type,
                    collections,
                    file_uids,
                )

                # 1) Fast exact Telegram UID lookup.
                # Query the local/global index once and prefer the detected source.
                # This replaces the old source-scope -> global waterfall while
                # preserving source preference and global fallback.
                if file_uids:
                    cached_name = None
                    if collections:
                        for candidate_uid in file_uids:
                            cached = self.uid_name_cache.get(f"uid:{filter_tag}:{candidate_uid}")
                            if cached:
                                cached_name = cached
                                break
                    if cached_name:
                        hit = True
                        cached_collection = (
                            collections[0]
                            if len(collections) == 1
                            else (scope.source_collection or collections[0])
                        )
                        cached_item = ItemSnapshot(
                            mongo_id="",
                            collection=cached_collection,
                            command=output_command or scope.command or settings.default_command,
                            name=cached_name,
                        )
                        return self._done(
                            self._with_command(cached_item, output_command, source_message),
                            "uid_cache",
                            started,
                            1.0,
                        )

                    item = await lookup_backend.fast_exact_uids(
                        file_uids,
                        preferred_collections=collections,
                    )
                    if item:
                        hit = True
                        # Only cache a hit in the detected source scope. Global
                        # fallback results stay uncached to avoid cross-source RAM
                        # identity collisions.
                        if collections and item.collection in collections:
                            for candidate_uid in file_uids:
                                self.uid_name_cache.set(
                                    f"uid:{filter_tag}:{candidate_uid}",
                                    item.name,
                                )
                            reason = "uid"
                        else:
                            reason = "uid_global"
                        log.info(
                            "UID DEBUG exact_hit reason=%s message=%s source=%s name=%s uids=%s",
                            reason,
                            getattr(message, "message_id", None),
                            item.collection,
                            item.name,
                            file_uids,
                        )
                        return self._done(
                            self._with_command(
                                item,
                                output_command_from_message(source_message, item.collection),
                                source_message,
                            ),
                            reason,
                            started,
                            1.0,
                        )

                    log.info(
                        "UID DEBUG exact_miss message=%s requested_sources=%s uids=%s",
                        getattr(message, "message_id", None),
                        collections,
                        file_uids,
                    )

                    # 3) Source-scoped file_id compatibility fallback.
                    # Adding-Helperbot also persists file_ids/file_id alongside the
                    # exact Telegram UID. This recovers older records whose UID
                    # aliases were not stored in the canonical array.
                    file_ids = _telegram_file_ids(source_message, media)
                    if collections and file_ids:
                        file_item = await lookup_backend.exact_file_ids(file_ids, collections)
                        if file_item:
                            hit = True
                            for candidate_uid in file_uids:
                                self.uid_name_cache.set(
                                    f"uid:{filter_tag}:{candidate_uid}",
                                    file_item.name,
                                )
                            log.info(
                                "FILE_ID DEBUG source_match message=%s source=%s name=%s file_ids=%s",
                                getattr(message, "message_id", None),
                                file_item.collection,
                                file_item.name,
                                len(file_ids),
                            )
                            return self._done(
                                self._with_command(
                                    file_item,
                                    output_command_from_message(source_message, file_item.collection),
                                    source_message,
                                ),
                                "file_id",
                                started,
                                1.0,
                            )

                    # 3) Catch-only compatibility recovery.
                    # Older Catch records can exist outside the canonical unified
                    # UID layout (legacy physical collection or legacy file/id fields).
                    # Never use this path for another source.
                    catch_lookup = bool(
                        scope.command == "/catch"
                        or scope.source_collection in {
                            "items_character_catcher",
                            "items_character_catcher_fw",
                        }
                        or is_character_catcher_spawn(source_message)
                    )
                    if catch_lookup:
                        catch_character_id = _catch_character_id(source_message)
                        catch_file_ids = _telegram_file_ids(source_message, media)
                        # UID was already checked globally just above. Re-querying
                        # UID fields inside Catch compatibility only adds Mongo round trips.
                        # Keep this legacy path for file_id / numeric Catch-ID recovery.
                        compat = await lookup_backend.catch_exact_compat(
                            uids=(),
                            file_ids=catch_file_ids,
                            character_id=catch_character_id,
                        )
                        if compat:
                            hit = True
                            for candidate_uid in file_uids:
                                self.uid_name_cache.set(
                                    f"uid:{filter_tag}:{candidate_uid}",
                                    compat.name,
                                )
                            log.info(
                                "Catch compatibility recovery message=%s source=%s name=%s "
                                "character_id=%s file_ids=%s",
                                getattr(message, "message_id", None),
                                compat.collection,
                                compat.name,
                                catch_character_id,
                                len(catch_file_ids),
                            )
                            return self._done(
                                self._with_command(
                                    compat,
                                    output_command_from_message(source_message, compat.collection),
                                    source_message,
                                ),
                                "catch_compat",
                                started,
                                1.0,
                            )

                # Exact matching is always the fast path, but an exact miss must not
                # make previously working hash/similarity lookup return unknown.
                # STRICT_EXACT_LOOKUP_ONLY remains accepted for config compatibility;
                # ENABLE_HASH_FALLBACK is the actual safety switch for hash matching.
                if not settings.enable_hash_fallback:
                    log.warning(
                        "HASH DEBUG disabled message=%s enable_hash_fallback=%s",
                        getattr(message, "message_id", None),
                        settings.enable_hash_fallback,
                    )
                    return self._done(None, "hash_fallback_disabled", started)

                # Limit the entire media-download + fingerprint phase to two
                # concurrent requests. This is the main RAM guard for PIL/OpenCV.
                async with self.hash_sem:
                    download_file_id = str(getattr(media.obj, "file_id", "") or "").strip()
                    log.info(
                        "HASH DEBUG start message=%s media_type=%s source=%s file_id_present=%s",
                        getattr(message, "message_id", None),
                        media.media_type,
                        collections,
                        bool(download_file_id),
                    )
                    data = await self._download(bot, download_file_id)
                    if not data:
                        log.warning(
                            "HASH DEBUG download_failed message=%s media_type=%s",
                            getattr(message, "message_id", None),
                            media.media_type,
                        )
                        return self._done(None, "download_failed", started)

                    # Raw SHA-256 is much cheaper than decoding an image or
                    # sampling a video. Check it before any heavyweight fingerprinting.
                    raw_sha = await asyncio.to_thread(sha256_bytes, data)
                    log.info(
                        "HASH DEBUG raw_sha_stage message=%s sha_present=%s",
                        getattr(message, "message_id", None),
                        bool(raw_sha),
                    )
                    item = None
                    reason = "sha"
                    if raw_sha:
                        if collections:
                            item = await lookup_backend.exact_sha(raw_sha, collections)
                        if not item:
                            item = await lookup_backend.exact_sha(raw_sha, None)
                            reason = "sha_global"
                        if item:
                            hit = True
                            self._cache_exact(item, file_uid, filter_tag)
                            return self._done(
                                self._with_command(
                                    item,
                                    output_command_from_message(source_message, item.collection),
                                    source_message,
                                ),
                                reason,
                                started,
                                1.0,
                            )

                    # Only a SHA miss reaches the heavyweight image/video fingerprinting path.
                    media_hash = await asyncio.to_thread(
                        hash_photo if media.media_type == "photo" else hash_video,
                        data,
                    )
                    log.info(
                        "HASH DEBUG computed message=%s sha=%s pixel_sha=%s phash=%s dhash=%s bytes=%s",
                        getattr(message, "message_id", None),
                        bool(media_hash.sha256),
                        bool(media_hash.pixel_sha256),
                        bool(media_hash.phash),
                        bool(media_hash.dhash),
                        len(data),
                    )
                    # The original media bytes are no longer needed after the
                    # fingerprint has been computed. Release this potentially large
                    # buffer before candidate lists/Mongo fallbacks are materialized.
                    del data
                # 4) Decoded canonical pixel hash exact match for photos.
                log.info(
                    "HASH DEBUG pixel_sha_stage message=%s enabled=%s",
                    getattr(message, "message_id", None),
                    bool(media.media_type == "photo" and media_hash.pixel_sha256),
                )
                if media.media_type == "photo" and media_hash.pixel_sha256:
                    item = None
                    reason = "pixel_sha"
                    if collections:
                        item = await lookup_backend.exact_pixel_sha(
                            media_hash.pixel_sha256,
                            collections,
                        )
                    if not item:
                        item = await lookup_backend.exact_pixel_sha(
                            media_hash.pixel_sha256,
                            None,
                        )
                        reason = "pixel_sha_global"
                    if item:
                        hit = True
                        self._cache_exact(item, file_uid, filter_tag)
                        return self._done(
                            self._with_command(
                                item,
                                output_command_from_message(source_message, item.collection),
                                source_message,
                            ),
                            reason,
                            started,
                            1.0,
                        )

                # 5) Exact sampled video signature.
                if media.media_type == "video" and media_hash.video_signature:
                    item = None
                    reason = "video_signature"
                    if collections:
                        item = await lookup_backend.exact_video_signature(
                            media_hash.video_signature,
                            collections,
                        )
                    if not item:
                        item = await lookup_backend.exact_video_signature(
                            media_hash.video_signature,
                            None,
                        )
                        reason = "video_signature_global"
                    if item:
                        hit = True
                        self._cache_exact(item, file_uid, filter_tag)
                        return self._done(
                            self._with_command(
                                item,
                                output_command_from_message(source_message, item.collection),
                                source_message,
                            ),
                            reason,
                            started,
                            1.0,
                        )

                # 6) Source-scoped similarity hash.
                log.info(
                    "HASH DEBUG similarity_stage message=%s media_type=%s scope=%s phash=%s dhash=%s",
                    getattr(message, "message_id", None),
                    media.media_type,
                    collections,
                    bool(media_hash.phash),
                    bool(media_hash.dhash),
                )
                item = None
                confidence = 0.0
                if collections:
                    item, confidence = await self._match_similarity(
                        media_hash,
                        media.media_type,
                        collections,
                        global_mode=False,
                    )
                reason = "photo_multihash" if media.media_type == "photo" else "video_multiframe"
                if item:
                    hit = True
                    self._cache_exact(item, file_uid, filter_tag)
                    return self._done(self._with_command(item, output_command, source_message), reason, started, confidence)

                # 7) Global similarity hash fallback. This stage is mandatory
                # after source-scoped hash/similarity misses for BOTH Auto and Manual.
                item, confidence = await self._match_similarity(
                    media_hash,
                    media.media_type,
                    None,
                    global_mode=True,
                )
                if item:
                    hit = True
                    self._cache_exact(item, file_uid, filter_tag)
                    return self._done(
                        self._with_command(
                            item,
                            output_command_from_message(source_message, item.collection),
                            source_message,
                        ),
                        f"{reason}_global",
                        started,
                        confidence,
                    )

                # Origin is retained as a final exact fallback for forwarded/archive
                # records, so UID/global-UID and hash matching always keep priority.
                origin = source_origin_key(source_message)
                if origin:
                    item = None
                    if collections:
                        item = await lookup_backend.exact_origin(origin, collections)
                    if not item:
                        item = await lookup_backend.exact_origin(origin, None)
                    if item:
                        hit = True
                        return self._done(
                            self._with_command(item, output_command, source_message),
                            "origin",
                            started,
                            1.0,
                        )

                return self._done(None, "not_found", started)
        except Exception:
            error = True
            log.exception("V3 lookup failed")
            return self._done(None, "error", started)
        finally:
            perf.lookup.record((time.perf_counter() - started) * 1000, hit=hit, error=error)

    async def _download(self, bot: Bot, file_id: str) -> bytes | None:
        if not file_id:
            return None
        async with self.download_sem:
            try:
                result = await asyncio.wait_for(bot.download(file_id), timeout=settings.download_timeout_seconds)
                if isinstance(result, io.BytesIO):
                    return result.getvalue()
                if hasattr(result, "read"):
                    value = result.read()
                    return value if isinstance(value, bytes) else bytes(value)
                return None
            except asyncio.TimeoutError:
                log.info("download timeout after %ss", settings.download_timeout_seconds)
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("download failed: %s", exc)
                return None

    def invalidate_lookup_cache(self) -> None:
        self.uid_name_cache.clear()

    @staticmethod
    def _filter_tag(collections: list[str] | None) -> str:
        return "+".join(collections) if collections else "all"

    def _cache_exact(self, item: ItemSnapshot, file_uid: str, cache_scope: str) -> None:
        # Positive L1 only: UID -> name. Never retain ItemSnapshot, SHA, miss,
        # or global/unscoped entries in process memory.
        if file_uid and cache_scope and cache_scope != "all" and item.name:
            self.uid_name_cache.set(
                f"uid:{cache_scope}:{file_uid}",
                item.name,
            )

    @staticmethod
    def _with_command(item: ItemSnapshot | None, output_command: str | None, source_message: Message) -> ItemSnapshot | None:
        if not item:
            return None
        command = output_command_from_message(source_message, item.collection) or output_command or item.command
        return item if not command or command == item.command else replace(item, command=command)

    @staticmethod
    def _done(item: ItemSnapshot | None, reason: str, started: float, confidence: float = 0.0) -> LookupResult:
        return LookupResult(item, reason, (time.perf_counter() - started) * 1000, confidence)

    async def _match_similarity(self, media_hash: MediaHash, media_type: str, collections: list[str] | None, *, global_mode: bool) -> tuple[ItemSnapshot | None, float]:
        if media_type == "photo":
            return await self._match_photo(media_hash, collections, global_mode=global_mode)
        return await self._match_video(media_hash, collections, global_mode=global_mode)

    async def _match_photo(self, media_hash: MediaHash, collections: list[str] | None, *, global_mode: bool) -> tuple[ItemSnapshot | None, float]:
        phash_threshold = settings.photo_phash_threshold
        if collections == ["items_waifux_grab"] or collections is None:
            phash_threshold = max(settings.photo_phash_threshold, settings.waifux_photo_phash_threshold)

        async def evaluate(items: list[ItemSnapshot]) -> tuple[ItemSnapshot | None, float]:
            best_item: ItemSnapshot | None = None
            best_score = 0.0
            for item in items:
                p_distance = hamming_hex(media_hash.phash, item.phash)
                d_distance = hamming_hex(media_hash.dhash, item.dhash)
                effective_p_threshold = (
                    settings.waifux_photo_phash_threshold
                    if item.is_waifux
                    else settings.photo_phash_threshold
                )

                # Old V2 records only have pHash: retain compatibility.
                if not item.dhash and p_distance is not None and p_distance <= effective_p_threshold:
                    score = max(0.0, 1.0 - p_distance / 64.0)
                    minimum = settings.global_photo_multi_score_min if global_mode else 0.0
                    if score >= minimum and score > best_score:
                        best_item, best_score = item, score
                    continue

                metrics: list[tuple[float, float]] = []
                for query_value, item_value, weight in (
                    (media_hash.phash, item.phash, 0.35),
                    (media_hash.dhash, item.dhash, 0.25),
                    (media_hash.whash, item.whash, 0.15),
                    (media_hash.phash_large, item.phash_large, 0.15),
                    (media_hash.colorhash, item.colorhash, 0.10),
                ):
                    distance = normalized_hamming(query_value, item_value)
                    if distance is not None:
                        metrics.append((max(0.0, 1.0 - distance), weight))
                if not metrics:
                    continue
                weighted = sum(similarity * weight for similarity, weight in metrics)
                total_weight = sum(weight for _, weight in metrics)
                score = weighted / total_weight if total_weight else 0.0

                structural_ok = (
                    (p_distance is not None and p_distance <= effective_p_threshold)
                    or (d_distance is not None and d_distance <= settings.photo_dhash_threshold)
                )
                minimum = (
                    settings.global_photo_multi_score_min
                    if global_mode
                    else settings.photo_multi_score_min
                )
                if structural_ok and score >= minimum and score > best_score:
                    best_item, best_score = item, score
            return best_item, best_score

        candidates = await lookup_backend.photo_candidates(
            collections,
            media_hash.phash,
            media_hash.dhash,
            phash_threshold,
            settings.photo_dhash_threshold,
            settings.photo_max_candidates,
        )
        best_item, best_score = await evaluate(candidates)
        log.info(
            "HASH DEBUG sqlite_photo_candidates scope=%s count=%s verified=%s score=%.3f",
            collections,
            len(candidates),
            bool(best_item),
            best_score,
        )

        # SQLite is a fast secondary index, never an authority. If it has no
        # verified match, query MongoDB using the same lookup-only projection.
        if best_item is None and settings.lookup_engine_mode == "sqlite":
            mongo_candidates = await lookup_backend.mongo_photo_candidates_fallback(
                collections,
                min(max(settings.photo_max_candidates, 2500), 10000),
                phash=media_hash.phash,
                dhash=media_hash.dhash,
                phash_threshold=phash_threshold,
                dhash_threshold=settings.photo_dhash_threshold,
            )
            seen = {(item.collection, item.mongo_id) for item in candidates}
            mongo_candidates = [
                item for item in mongo_candidates
                if (item.collection, item.mongo_id) not in seen
            ]
            best_item, best_score = await evaluate(mongo_candidates)
            log.info(
                "HASH DEBUG mongo_photo_candidates scope=%s count=%s verified=%s score=%.3f",
                collections,
                len(mongo_candidates),
                bool(best_item),
                best_score,
            )
        return best_item, best_score

    async def _match_video(self, media_hash: MediaHash, collections: list[str] | None, *, global_mode: bool) -> tuple[ItemSnapshot | None, float]:
        async def evaluate(items: list[ItemSnapshot]) -> tuple[ItemSnapshot | None, float]:
            best_item: ItemSnapshot | None = None
            best_score = 0.0
            for item in items:
                frame_threshold = (
                    settings.waifux_video_frame_threshold
                    if item.is_waifux
                    else settings.video_frame_threshold
                )
                avg_threshold = (
                    settings.waifux_video_avg_threshold
                    if item.is_waifux
                    else settings.video_avg_threshold
                )

                distances: list[int] = []
                if media_hash.video_samples and item.video_samples:
                    item_by_pos = {round(sample.position, 3): sample for sample in item.video_samples}
                    for sample in media_hash.video_samples:
                        other = item_by_pos.get(round(sample.position, 3))
                        if not other:
                            continue
                        p = hamming_hex(sample.phash, other.phash)
                        d = hamming_hex(sample.dhash, other.dhash)
                        if p is not None:
                            distances.append(p)
                        if d is not None:
                            distances.append(d)
                elif media_hash.frame_hashes and item.frame_hashes:
                    for left, right in zip(media_hash.frame_hashes, item.frame_hashes):
                        distance = hamming_hex(left, right)
                        if distance is not None:
                            distances.append(distance)
                if not distances:
                    continue
                average = sum(distances) / len(distances)
                minimum = min(distances)
                if global_mode:
                    if minimum > max(1, frame_threshold - 2) or average > max(1.0, avg_threshold - 2.0):
                        continue
                else:
                    if minimum > frame_threshold or average > avg_threshold:
                        continue
                score = max(0.0, 1.0 - average / 64.0)
                if score > best_score:
                    best_item, best_score = item, score
            return best_item, best_score

        candidates = await lookup_backend.video_candidates(
            collections,
            media_hash.duration_ms,
            settings.video_duration_tolerance_seconds,
        )
        best_item, best_score = await evaluate(candidates)

        # Same correctness guard as photos: a stale/partial SQLite index cannot
        # hide a valid MongoDB record when the local candidates do not verify.
        if best_item is None and settings.lookup_engine_mode == "sqlite":
            mongo_candidates = await lookup_backend.mongo_video_candidates_fallback(
                collections,
                media_hash.duration_ms,
                settings.video_duration_tolerance_seconds,
                min(max(settings.video_max_candidates, 5000), 10000),
            )
            seen = {(item.collection, item.mongo_id) for item in candidates}
            mongo_candidates = [
                item for item in mongo_candidates
                if (item.collection, item.mongo_id) not in seen
            ]
            best_item, best_score = await evaluate(mongo_candidates)
        return best_item, best_score



lookup_service = LookupService()

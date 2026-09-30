from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, replace

from aiogram import Bot
from aiogram.types import Message

from config import settings
from services.hash_service import MediaHash, hamming_hex, hash_photo, hash_video, normalized_hamming
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
        self.result_cache: TTLCache[str, ItemSnapshot] = TTLCache(
            settings.result_cache_max_items, settings.result_cache_ttl_seconds
        )
        self.miss_cache: TTLCache[str, bool] = TTLCache(
            settings.result_cache_max_items, settings.miss_cache_ttl_seconds
        )
        self.download_sem = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.lookup_sem = asyncio.Semaphore(settings.max_concurrent_lookups)

    async def lookup_message(self, bot: Bot, message: Message, *, manual: bool = False) -> LookupResult:
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
                # Character Catcher spawn posts are scoped to its own collection first,
                # then explicitly fall back to the complete item database if the
                # Catch collection has no match. The leading emoji is intentionally
                # ignored by the caption detector because Catch Bot changes it.
                catch_spawn_global_fallback = is_character_catcher_spawn(source_message)
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

                # 1) UID exact match, including every Telegram photo-size UID
                # and V3 file_unique_ids aliases. Source scope always wins.
                if file_uids:
                    for candidate_uid in file_uids:
                        cache_key = f"uid:{filter_tag}:{candidate_uid}"
                        cached = self.result_cache.get(cache_key)
                        if cached:
                            hit = True
                            return self._done(
                                self._with_command(cached, output_command, source_message),
                                "uid_cache",
                                started,
                                1.0,
                            )
                        item = await lookup_backend.exact_uid(candidate_uid, collections)
                        if item:
                            hit = True
                            self.result_cache.set(cache_key, item)
                            log.info(
                                "UID DEBUG source_match message=%s source=%s name=%s uid=%s",
                                getattr(message, "message_id", None),
                                item.collection,
                                item.name,
                                candidate_uid,
                            )
                            return self._done(
                                self._with_command(item, output_command, source_message),
                                "uid",
                                started,
                                1.0,
                            )

                    # Global UID recovery is intentionally restricted:
                    # - manual lookup may recover globally;
                    # - Catch spawn is a controlled exception;
                    # - auto lookup with a known source stays source-scoped.
                    allow_global_exact = bool((manual and not collections) or catch_spawn_global_fallback)
                    if allow_global_exact:
                        # Catch source gets priority for the legacy spawn path.
                        for candidate_uid in file_uids:
                            item = await lookup_backend.exact_uid(
                                candidate_uid,
                                ["items_character_catcher"],
                            )
                            if item:
                                hit = True
                                log.info(
                                    "UID DEBUG global_exact_priority message=%s preferred_source=%s uid=%s name=%s",
                                    getattr(message, "message_id", None),
                                    item.collection,
                                    candidate_uid,
                                    item.name,
                                )
                                return self._done(
                                    self._with_command(item, output_command, source_message),
                                    "uid_global",
                                    started,
                                    1.0,
                                )

                        for candidate_uid in file_uids:
                            item = await lookup_backend.exact_uid(candidate_uid, None)
                            if item:
                                hit = True
                                log.info(
                                    "UID DEBUG global_exact_recovery message=%s source=%s uid=%s name=%s",
                                    getattr(message, "message_id", None),
                                    item.collection,
                                    candidate_uid,
                                    item.name,
                                )
                                return self._done(
                                    self._with_command(item, output_command, source_message),
                                    "uid_global",
                                    started,
                                    1.0,
                                )

                    log.warning(
                        "UID DEBUG database_uid_miss message=%s requested_sources=%s",
                        getattr(message, "message_id", None),
                        collections,
                    )

                # Exact matching is always the fast path, but an exact miss must not
                # make previously working hash/similarity lookup return unknown.
                # STRICT_EXACT_LOOKUP_ONLY remains accepted for config compatibility;
                # ENABLE_HASH_FALLBACK is the actual safety switch for hash matching.
                if not settings.enable_hash_fallback:
                    return self._done(None, "hash_fallback_disabled", started)

                data = await self._download(bot, str(getattr(media.obj, "file_id", "") or ""))
                if not data:
                    return self._done(None, "download_failed", started)

                media_hash = await asyncio.to_thread(hash_photo if media.media_type == "photo" else hash_video, data)
                sha_cache_key = f"sha:{filter_tag}:{media_hash.sha256 or ''}"
                if media_hash.sha256:
                    cached = self.result_cache.get(sha_cache_key)
                    if cached:
                        hit = True
                        return self._done(self._with_command(cached, output_command, source_message), "sha_cache", started, 1.0)

                # 2) byte exact SHA aliases.
                item = await lookup_backend.exact_sha(media_hash.sha256 or "", collections)
                reason = "sha"
                if not item and ((manual and not collections) or catch_spawn_global_fallback):
                    item = await lookup_backend.exact_sha(media_hash.sha256 or "", None)
                    reason = "sha_global"
                if item:
                    hit = True
                    self._cache_exact(item, file_uid, sha_cache_key)
                    return self._done(self._with_command(item, output_command, source_message), reason, started, 1.0)

                # 3) decoded canonical pixel hash exact match for photos.
                if media.media_type == "photo" and media_hash.pixel_sha256:
                    item = await lookup_backend.exact_pixel_sha(media_hash.pixel_sha256, collections)
                    reason = "pixel_sha"
                    if not item and ((manual and not collections) or catch_spawn_global_fallback):
                        item = await lookup_backend.exact_pixel_sha(media_hash.pixel_sha256, None)
                        reason = "pixel_sha_global"
                    if item:
                        hit = True
                        self._cache_exact(item, file_uid, sha_cache_key)
                        return self._done(self._with_command(item, output_command, source_message), reason, started, 1.0)

                # 4) exact sampled video signature.
                if media.media_type == "video" and media_hash.video_signature:
                    item = await lookup_backend.exact_video_signature(media_hash.video_signature, collections)
                    reason = "video_signature"
                    if not item and ((manual and not collections) or catch_spawn_global_fallback):
                        item = await lookup_backend.exact_video_signature(media_hash.video_signature, None)
                        reason = "video_signature_global"
                    if item:
                        hit = True
                        self._cache_exact(item, file_uid, sha_cache_key)
                        return self._done(self._with_command(item, output_command, source_message), reason, started, 1.0)

                # 5) source-scoped similarity.
                item, confidence = await self._match_similarity(media_hash, media.media_type, collections, global_mode=False)
                reason = "photo_multihash" if media.media_type == "photo" else "video_multiframe"
                if item:
                    hit = True
                    self._cache_exact(item, file_uid, sha_cache_key)
                    return self._done(self._with_command(item, output_command, source_message), reason, started, confidence)

                # 6) controlled global similarity fallback. Exact fallbacks above are always preferred.
                if catch_spawn_global_fallback or (manual and not collections):
                    item, confidence = await self._match_similarity(media_hash, media.media_type, None, global_mode=True)
                    if item:
                        hit = True
                        self._cache_exact(item, file_uid, sha_cache_key)
                        return self._done(
                            self._with_command(item, output_command_from_message(source_message, item.collection), source_message),
                            f"{reason}_global",
                            started,
                            confidence,
                        )

                # Origin is retained as a late exact fallback for forwarded/archive
                # records, so UID/global-UID and hash matching always keep priority.
                origin = source_origin_key(source_message)
                if origin:
                    item = await lookup_backend.exact_origin(origin, collections)
                    if not item and ((manual and not collections) or catch_spawn_global_fallback):
                        item = await lookup_backend.exact_origin(origin, None)
                    if item:
                        hit = True
                        return self._done(
                            self._with_command(item, output_command, source_message),
                            "origin",
                            started,
                            1.0,
                        )

                miss_key = f"miss:{filter_tag}:{media.media_type}:{media_hash.sha256 or file_uid}"
                self.miss_cache.set(miss_key, True)
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
        self.result_cache.clear()
        self.miss_cache.clear()

    @staticmethod
    def _filter_tag(collections: list[str] | None) -> str:
        return "+".join(collections) if collections else "all"

    def _cache_exact(self, item: ItemSnapshot, file_uid: str, sha_cache_key: str) -> None:
        if file_uid:
            self.result_cache.set(f"uid:all:{file_uid}", item)
        if sha_cache_key:
            self.result_cache.set(sha_cache_key, item)

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

        # SQLite is a fast secondary index, never an authority. If it has no
        # verified match, query MongoDB using the same lookup-only projection.
        if best_item is None and settings.lookup_engine_mode == "sqlite":
            mongo_candidates = await lookup_backend.mongo_photo_candidates_fallback(
                collections,
                min(max(settings.photo_max_candidates, 2500), 10000),
            )
            seen = {(item.collection, item.mongo_id) for item in candidates}
            mongo_candidates = [
                item for item in mongo_candidates
                if (item.collection, item.mongo_id) not in seen
            ]
            best_item, best_score = await evaluate(mongo_candidates)
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

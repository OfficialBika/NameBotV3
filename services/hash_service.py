from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from typing import Tuple

import cv2
import imagehash
from PIL import Image, ImageOps

from config import settings


@dataclass(frozen=True)
class VideoSampleHash:
    position: float
    frame_index: int
    phash: str
    dhash: str


@dataclass(frozen=True)
class MediaHash:
    sha256: str | None = None
    pixel_sha256: str | None = None
    phash: str | None = None
    phash_large: str | None = None
    dhash: str | None = None
    whash: str | None = None
    colorhash: str | None = None
    crop_hash: str | None = None
    frame_hashes: Tuple[str, ...] = ()
    video_samples: Tuple[VideoSampleHash, ...] = ()
    video_signature: str | None = None
    duration_ms: int = 0
    fps: float = 0.0
    frame_count: int = 0
    width: int = 0
    height: int = 0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    """Hash a downloaded media file without loading the whole file into RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hamming_hex(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return (int(str(a), 16) ^ int(str(b), 16)).bit_count()
    except Exception:
        return None


def normalized_hamming(a: str | None, b: str | None) -> float | None:
    distance = hamming_hex(a, b)
    if distance is None:
        return None
    try:
        bits = max(len(str(a)), len(str(b))) * 4
        return distance / max(1, bits)
    except Exception:
        return None


def crop_hash_distance(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        left = imagehash.hex_to_multihash(str(a))
        right = imagehash.hex_to_multihash(str(b))
        value = left - right
        return float(value)
    except Exception:
        return None


def hash_photo(data: bytes) -> MediaHash:
    digest = sha256_bytes(data)
    try:
        with Image.open(io.BytesIO(data)) as opened:
            orientation = 1
            try:
                orientation = int((opened.getexif() or {}).get(274, 1) or 1)
            except Exception:
                orientation = 1
            image = (
                opened.convert("RGB")
                if orientation == 1
                else ImageOps.exif_transpose(opened).convert("RGB")
            )
            width, height = image.size
            # Preserve the exact pixel-SHA input format without constructing a
            # second full-size concatenated bytes object.
            pixel_hasher = hashlib.sha256()
            pixel_hasher.update(width.to_bytes(4, "big"))
            pixel_hasher.update(height.to_bytes(4, "big"))
            pixel_hasher.update(image.tobytes())
            pixel_sha = pixel_hasher.hexdigest()
            return MediaHash(
                sha256=digest,
                pixel_sha256=pixel_sha,
                # Keep the live lookup fingerprint CPU/RAM bounded. pHash, large-pHash
                # and dHash are the indexed structural signals used for candidate search
                # and verification. Full-resolution wHash/colorHash can allocate/scan
                # very large arrays for high-resolution Telegram media, so they remain
                # stored/compatible in Mongo but are intentionally omitted from live
                # request computation.
                phash=str(imagehash.phash(image)),
                phash_large=str(imagehash.phash(image, hash_size=16)),
                dhash=str(imagehash.dhash(image)),
                whash=None,
                colorhash=None,
                crop_hash=None,
                width=width,
                height=height,
            )
    except Exception:
        return MediaHash(sha256=digest)


def hash_photo_file(path: str, digest: str | None = None) -> MediaHash:
    """Compute the live photo fingerprint directly from a disk file."""
    digest = digest or sha256_file(path)
    try:
        with Image.open(path) as opened:
            # Most Telegram images have no rotation requirement. Avoid an
            # unnecessary full-resolution EXIF-transpose copy in that common case.
            orientation = 1
            try:
                orientation = int((opened.getexif() or {}).get(274, 1) or 1)
            except Exception:
                orientation = 1
            image = (
                opened.convert("RGB")
                if orientation == 1
                else ImageOps.exif_transpose(opened).convert("RGB")
            )
        try:
            width, height = image.size
            pixel_hasher = hashlib.sha256()
            pixel_hasher.update(width.to_bytes(4, "big"))
            pixel_hasher.update(height.to_bytes(4, "big"))
            pixel_hasher.update(image.tobytes())
            pixel_sha = pixel_hasher.hexdigest()
            return MediaHash(
                sha256=digest,
                pixel_sha256=pixel_sha,
                phash=str(imagehash.phash(image)),
                phash_large=str(imagehash.phash(image, hash_size=16)),
                dhash=str(imagehash.dhash(image)),
                whash=None,
                colorhash=None,
                crop_hash=None,
                width=width,
                height=height,
            )
        finally:
            try:
                image.close()
            except Exception:
                pass
    except Exception:
        return MediaHash(sha256=digest)


def _frame_bundle(frame) -> tuple[str, str]:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    return str(imagehash.phash(image)), str(imagehash.dhash(image))


def _read_sample(cap, frame_count: int, position: float) -> VideoSampleHash | None:
    index = max(0, min(frame_count - 1, int(frame_count * position)))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    phash, dhash = _frame_bundle(frame)
    return VideoSampleHash(round(float(position), 4), index, phash, dhash)


def hash_video_file(path: str, digest: str | None = None) -> MediaHash:
    """Compute video fingerprints from a disk file without retaining full media bytes."""
    digest = digest or sha256_file(path)
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return MediaHash(sha256=digest)

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if frame_count <= 0:
            return MediaHash(sha256=digest, fps=fps, width=width, height=height)

        legacy: list[str] = []
        for position in settings.video_sample_points:
            sample = _read_sample(cap, frame_count, position)
            if sample:
                legacy.append(sample.phash)

        samples: list[VideoSampleHash] = []
        for position in settings.video_v3_sample_points:
            sample = _read_sample(cap, frame_count, position)
            if sample:
                samples.append(sample)

        material = "|".join(
            f"{sample.position}:{sample.phash}:{sample.dhash}" for sample in samples
        ).encode("utf-8")
        signature = hashlib.sha256(material).hexdigest() if material else None
        return MediaHash(
            sha256=digest,
            frame_hashes=tuple(legacy),
            video_samples=tuple(samples),
            video_signature=signature,
            duration_ms=int(round((frame_count / fps) * 1000)) if fps > 0 else 0,
            fps=round(fps, 6),
            frame_count=frame_count,
            width=width,
            height=height,
        )
    except Exception:
        return MediaHash(sha256=digest)
    finally:
        if cap is not None:
            cap.release()


def hash_video(data: bytes) -> MediaHash:
    digest = sha256_bytes(data)
    tmp_path: str | None = None
    cap = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return MediaHash(sha256=digest)

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if frame_count <= 0:
            return MediaHash(sha256=digest, fps=fps, width=width, height=height)

        legacy: list[str] = []
        for position in settings.video_sample_points:
            sample = _read_sample(cap, frame_count, position)
            if sample:
                legacy.append(sample.phash)

        samples: list[VideoSampleHash] = []
        for position in settings.video_v3_sample_points:
            sample = _read_sample(cap, frame_count, position)
            if sample:
                samples.append(sample)

        material = "|".join(
            f"{sample.position}:{sample.phash}:{sample.dhash}" for sample in samples
        ).encode("utf-8")
        signature = hashlib.sha256(material).hexdigest() if material else None
        duration_ms = int(round((frame_count / fps) * 1000)) if fps > 0 else 0
        return MediaHash(
            sha256=digest,
            frame_hashes=tuple(legacy),
            video_samples=tuple(samples),
            video_signature=signature,
            duration_ms=duration_ms,
            fps=round(fps, 6),
            frame_count=frame_count,
            width=width,
            height=height,
        )
    except Exception:
        return MediaHash(sha256=digest)
    finally:
        if cap is not None:
            cap.release()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

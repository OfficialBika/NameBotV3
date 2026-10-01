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


def hash_photo(
    data: bytes,
    *,
    include_advanced: bool = True,
    include_crop: bool = True,
    digest: str | None = None,
) -> MediaHash:
    """Build photo fingerprints.

    The lookup path uses include_advanced=False first so exact pixel/hash
    matches can return without calculating expensive perceptual hashes.
    """
    digest = digest or sha256_bytes(data)
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            pixel_material = width.to_bytes(4, "big") + height.to_bytes(4, "big") + image.tobytes()
            pixel_sha = hashlib.sha256(pixel_material).hexdigest()
            phash = str(imagehash.phash(image))
            dhash = str(imagehash.dhash(image))
            if not include_advanced:
                return MediaHash(
                    sha256=digest,
                    pixel_sha256=pixel_sha,
                    phash=phash,
                    dhash=dhash,
                    width=width,
                    height=height,
                )
            return MediaHash(
                sha256=digest,
                pixel_sha256=pixel_sha,
                phash=phash,
                phash_large=str(imagehash.phash(image, hash_size=16)),
                dhash=dhash,
                whash=str(imagehash.whash(image)),
                colorhash=str(imagehash.colorhash(image)),
                crop_hash=str(imagehash.crop_resistant_hash(image)) if include_crop else None,
                width=width,
                height=height,
            )
    except Exception:
        return MediaHash(sha256=digest)


def hash_photo_basic(data: bytes, *, digest: str | None = None) -> MediaHash:
    """Fast photo fingerprint stage: pixel SHA + pHash + dHash only."""
    return hash_photo(data, include_advanced=False, digest=digest)

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


def hash_video(data: bytes, *, digest: str | None = None) -> MediaHash:
    """Build full video fingerprints after raw SHA exact matching has missed."""
    digest = digest or sha256_bytes(data)
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

        # The legacy and V3 position sets overlap at 0.5. Decode each
        # physical frame only once, then project the same sample into both
        # compatibility outputs.
        unique_positions = list(
            dict.fromkeys(
                [float(value) for value in settings.video_sample_points]
                + [float(value) for value in settings.video_v3_sample_points]
            )
        )
        sampled: dict[float, VideoSampleHash] = {}
        for position in unique_positions:
            sample = _read_sample(cap, frame_count, position)
            if sample:
                sampled[round(float(position), 4)] = sample

        legacy: list[str] = []
        for position in settings.video_sample_points:
            sample = sampled.get(round(float(position), 4))
            if sample:
                legacy.append(sample.phash)

        samples: list[VideoSampleHash] = []
        for position in settings.video_v3_sample_points:
            sample = sampled.get(round(float(position), 4))
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

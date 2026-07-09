from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.types import Message


@dataclass(frozen=True)
class ExtractedMedia:
    obj: Any
    media_type: str
    source_message: Message


def _document_media_type(document) -> str | None:
    if not document:
        return None
    mime = str(getattr(document, "mime_type", "") or "").lower()
    name = str(getattr(document, "file_name", "") or "").lower()
    if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "photo"
    if mime.startswith("video/") or name.endswith((".mp4", ".mkv", ".mov", ".webm")):
        return "video"
    return None


def extract_media(message: Message) -> ExtractedMedia | None:
    target = message.reply_to_message or message
    if getattr(target, "photo", None):
        return ExtractedMedia(target.photo[-1], "photo", target)
    if getattr(target, "video", None):
        return ExtractedMedia(target.video, "video", target)
    if getattr(target, "animation", None):
        return ExtractedMedia(target.animation, "video", target)
    document = getattr(target, "document", None)
    media_type = _document_media_type(document)
    if document and media_type:
        return ExtractedMedia(document, media_type, target)
    return None


def has_media(message: Message) -> bool:
    return extract_media(message) is not None

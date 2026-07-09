from __future__ import annotations

import html
import re
import unicodedata

_SPACE_RE = re.compile(r"\s+")
_LEADING_NOISE_RE = re.compile(r"^[\s:：|┃•·\-–—_]+")


def h(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=False)


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    value = unicodedata.normalize("NFKC", str(name)).strip()
    value = _LEADING_NOISE_RE.sub("", value)
    return _SPACE_RE.sub(" ", value).strip()


def first_token(name: str) -> str:
    clean = normalize_name(name)
    return (clean.split() or [clean])[0]


def compact_command_text(text: str | None) -> str:
    return _SPACE_RE.sub(" ", (text or "").strip())

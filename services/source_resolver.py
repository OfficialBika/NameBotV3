from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from aiogram.types import Message

from config import (
    BOT_SOURCE_CHAT_ID,
    BOT_SOURCE_COLLECTION,
    BOT_SOURCE_OUTPUT_COMMAND,
    BOT_SOURCE_OUTPUT_USER_ID,
    BOT_SOURCE_USER_ID,
    COLLECTION_TO_OUTPUT_COMMAND,
    COMMAND_TO_COLLECTION,
    settings,
)

try:
    from services.source_blocker import is_blocked_source
except Exception:  # pragma: no cover
    def is_blocked_source(message: Message | None) -> bool:
        return False


@dataclass(frozen=True)
class LookupScope:
    collections: list[str] | None
    mode: str
    command: str | None = None
    source_collection: str | None = None
    strict: bool = False
    confident: bool = False
    source_label: str | None = None


LOOKUP_COLLECTION_ORDER = [c for c in COLLECTION_TO_OUTPUT_COMMAND if c != "items_unknown"]
COMMAND_TO_COLLECTIONS: dict[str, list[str]] = {
    "/catch": ["items_character_catcher"],
    "/hallow": ["items_characters_hallow"],
    "/capture": ["items_capture_character"],
    "/seize": ["items_character_seizer"],
    "/loot": ["items_capture_character"],
    "/take": ["items_takers_character"],
    "/smash": ["items_smash_character"],
    "/challenge": ["items_roronoa_zoro"],
    "/bika": ["items_bika_character"],
    "/ziceko": ["items_super_zeko"],
    "/orin": ["items_orinx_waifu"],
    "/dao": ["items_immortal_donghua"],
    "/pick": ["items_character_picker", "items_senpai_catcher"],
    "/grab": [
        "items_husbando_grabber",
        "items_grab_your_waifu",
        "items_grab_your_husbando",
        "items_waifux_grab",
        "items_waifu_grabber",
    ],
    "/guess": ["items_catch_your_husbando", "items_catch_your_waifu"],
}

STYLIZED_LATIN_TRANSLATION = str.maketrans({
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f",
    "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l",
    "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})


def _norm_text(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").translate(STYLIZED_LATIN_TRANSLATION)


def _clean_title(value: str | None) -> str:
    value = _norm_text(value).lower().strip().replace("_", " ")
    value = re.sub(r"[^0-9a-z\u1000-\u109f\s]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


TITLE_SOURCE_COLLECTION: dict[str, str] = {
    "character catcher": "items_character_catcher",
    "character catcher bot": "items_character_catcher",
    "characters hallow": "items_characters_hallow",
    "hallow upload": "items_characters_hallow",
    "hallow uploads": "items_characters_hallow",
    "capture character": "items_capture_character",
    "capture database": "items_capture_character",
    "character loot": "items_capture_character",
    "character loot bot": "items_capture_character",
    "character seizer": "items_character_seizer",
    "seizer database": "items_character_seizer",
    "husbando grabber": "items_husbando_grabber",
    "grab your waifu": "items_grab_your_waifu",
    "grab your husbando": "items_grab_your_husbando",
    "waifuxgrab": "items_waifux_grab",
    "waifuxgrab database": "items_waifux_grab",
    "grab garden": "items_waifux_grab",
    "waifu grabber": "items_waifu_grabber",
    "takers character": "items_takers_character",
    "catch your husbando": "items_catch_your_husbando",
    "catch your waifu": "items_catch_your_waifu",
    "smash character": "items_smash_character",
    "roronoa zoro": "items_roronoa_zoro",
    "character picker": "items_character_picker",
    "bika waifu database": "items_bika_character",
    "bika character bot": "items_bika_character",
    "senpai catcher": "items_senpai_catcher",
    "senpaicatcher": "items_senpai_catcher",
    "senpai database": "items_senpai_catcher",
    "myanmar character": "items_super_zeko",
    "myanmar character logs": "items_super_zeko",
    "super zeko": "items_super_zeko",
    "ziceko data": "items_super_zeko",
    "orinx waifu": "items_orinx_waifu",
    "orinx waifu bot": "items_orinx_waifu",
    "timunagalaya": "items_orinx_waifu",
    "immortal donghua": "items_immortal_donghua",
    "donghua database": "items_immortal_donghua",
}

TITLE_OUTPUT_COMMAND: dict[str, str] = {
    "character loot": "/loot",
    "character loot bot": "/loot",
    "roronoa zoro": "/challenge",
    "character picker": "/pick",
    "bika waifu database": "/bika",
    "bika character bot": "/bika",
    "senpai catcher": "/pick",
    "senpaicatcher": "/pick",
    "senpai database": "/pick",
    "myanmar character": "/ziceko",
    "super zeko": "/ziceko",
    "orinx waifu": "/orin",
    "timunagalaya": "/orin",
    "immortal donghua": "/dao",
    "donghua database": "/dao",
}

CONTENT_SOURCE_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    (re.compile(r"new\s+waifu\s+added|item\s*id\s*[:：].*\bname\b\s*[:：].*\brarity\b\s*[:：]|waifuxgrab", re.I | re.S), "items_waifux_grab", "/grab"),
    (re.compile(r"new\s+character\s+added\s+to\s+the\s+bot|char\s*id\s*[:：].*\bname\b\s*[:：].*\banime\b\s*[:：].*\brarity\b\s*[:：]", re.I | re.S), "items_senpai_catcher", "/pick"),
    (re.compile(r"character\s+database.*\bid\b\s*[:：].*\bname\b\s*[:：].*\bseries\b\s*[:：].*\brarity\b", re.I | re.S), "items_orinx_waifu", "/orin"),
    (re.compile(r"card\s+drop|myanmar\s+character|/ziceko|တင်ပြီးပြီ|uploaded\s*\(/?li\)|📛.*name|⭐.*rarity", re.I | re.S), "items_super_zeko", "/ziceko"),
    (re.compile(r"(?:saved|updated).*\bname\b\s*[:：].*\bid\b\s*[:：].*\brarity\b\s*[:：].*\banime\b\s*[:：]", re.I | re.S), "items_immortal_donghua", "/dao"),
]

USING_RE = re.compile(r"(?:using|use|hint|full|cmd|command)\s*[:：\-=]?\s*(/[a-zA-Z0-9_]+)(?:@[A-Za-z0-9_]+)?", re.I)
CMD_RE = re.compile(r"(^|\s)(/[a-zA-Z0-9_]+)(?:@[A-Za-z0-9_]+)?(?=\s|$|[^A-Za-z0-9_@])", re.I)


def _message_text(message: Message) -> str:
    parts: list[str] = []
    for obj in (message, getattr(message, "external_reply", None), getattr(message, "reply_to_message", None)):
        if obj is None:
            continue
        for attr in ("caption", "text", "html_text", "md_text"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value.strip():
                parts.append(_norm_text(value))
    return "\n".join(parts)


def command_from_text(text: str | None) -> str | None:
    if not text:
        return None
    value = _norm_text(text).strip()
    first = value.split(maxsplit=1)[0].lower().split("@", 1)[0] if value else ""
    if first in COMMAND_TO_COLLECTIONS or first in COMMAND_TO_COLLECTION:
        return first
    match = USING_RE.search(value) or CMD_RE.search(value)
    if not match:
        return None
    cmd = match.group(match.lastindex or 1).lower().split("@", 1)[0]
    return cmd if cmd in COMMAND_TO_COLLECTIONS or cmd in COMMAND_TO_COLLECTION else None


def collections_from_command(cmd: str | None) -> list[str]:
    if not cmd:
        return []
    cmd = cmd.lower().split("@", 1)[0]
    if cmd in COMMAND_TO_COLLECTIONS:
        return list(COMMAND_TO_COLLECTIONS[cmd])
    collection = COMMAND_TO_COLLECTION.get(cmd)
    return [collection] if collection else []


def collection_from_command(cmd: str | None) -> str | None:
    cols = collections_from_command(cmd)
    return cols[0] if cols else None


def _normalize_username(username: str | None) -> str | None:
    username = (username or "").strip().lower().lstrip("@")
    return "@" + username if username else None


def _source_origin_chat(message: Message):
    origin = getattr(message, "forward_origin", None)
    if origin:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat:
            return chat
    return getattr(message, "forward_from_chat", None) or getattr(message, "sender_chat", None)


def source_chat_id(message: Message) -> int | None:
    chat = _source_origin_chat(message)
    try:
        return int(chat.id) if chat and getattr(chat, "id", None) is not None else None
    except Exception:
        return None


def source_origin_message_id(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    raw = getattr(origin, "message_id", None) if origin else None
    if raw is None:
        raw = getattr(message, "forward_from_message_id", None)
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def source_origin_key(message: Message) -> tuple[int, int] | None:
    chat_id = source_chat_id(message)
    message_id = source_origin_message_id(message)
    return (chat_id, message_id) if chat_id is not None and message_id is not None else None


def source_user_id(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    sender_user = getattr(origin, "sender_user", None) if origin else None
    for user in (
        sender_user,
        getattr(message, "via_bot", None),
        getattr(message, "forward_from", None),
        getattr(message, "from_user", None) if getattr(getattr(message, "from_user", None), "is_bot", False) else None,
    ):
        if user and getattr(user, "id", None) is not None:
            try:
                return int(user.id)
            except Exception:
                pass
    return None


def source_username(message: Message) -> str | None:
    chat = _source_origin_chat(message)
    if chat and getattr(chat, "username", None):
        return _normalize_username(chat.username)
    origin = getattr(message, "forward_origin", None)
    sender_user = getattr(origin, "sender_user", None) if origin else None
    if sender_user and getattr(sender_user, "username", None):
        return _normalize_username(sender_user.username)
    via_bot = getattr(message, "via_bot", None)
    if via_bot and getattr(via_bot, "username", None):
        return _normalize_username(via_bot.username)
    fuser = getattr(message, "forward_from", None)
    if fuser and getattr(fuser, "username", None):
        return _normalize_username(fuser.username)
    from_user = getattr(message, "from_user", None)
    if from_user and getattr(from_user, "is_bot", False) and getattr(from_user, "username", None):
        return _normalize_username(from_user.username)
    return None


def source_title(message: Message) -> str | None:
    chat = _source_origin_chat(message)
    if chat and getattr(chat, "title", None):
        return str(chat.title)
    origin = getattr(message, "forward_origin", None)
    sender_user = getattr(origin, "sender_user", None) if origin else None
    if sender_user:
        name = getattr(sender_user, "full_name", None) or " ".join(x for x in [getattr(sender_user, "first_name", None), getattr(sender_user, "last_name", None)] if x)
        if name:
            return str(name)
    hidden = getattr(origin, "sender_user_name", None) if origin else None
    if hidden:
        return str(hidden)
    return None


def _title_to_collection(title: str | None) -> str | None:
    cleaned = _clean_title(title)
    if not cleaned:
        return None
    if cleaned in TITLE_SOURCE_COLLECTION:
        return TITLE_SOURCE_COLLECTION[cleaned]
    for key, collection in TITLE_SOURCE_COLLECTION.items():
        if key in cleaned or cleaned in key:
            return collection
    return None


def _title_to_output_command(title: str | None) -> str | None:
    cleaned = _clean_title(title)
    if not cleaned:
        return None
    if cleaned in TITLE_OUTPUT_COMMAND:
        return TITLE_OUTPUT_COMMAND[cleaned]
    for key, cmd in TITLE_OUTPUT_COMMAND.items():
        if key in cleaned or cleaned in key:
            return cmd
    return None


def _content_source(message: Message) -> tuple[str | None, str | None]:
    text = _message_text(message)
    for pattern, collection, command in CONTENT_SOURCE_RULES:
        if pattern.search(text):
            return collection, command
    return None, None


def _custom_source_command(message: Message) -> str | None:
    uname = source_username(message)
    title = source_title(message) or ""
    text = f"{title}\n{_message_text(message)}".lower()
    if uname and uname in settings.forward_source_commands:
        return settings.forward_source_commands[uname]
    for key, command in settings.forward_source_commands.items():
        key = key.lower().strip()
        if key.startswith("@"):
            continue
        if "|" in key:
            parts = [part.strip() for part in key.split("|") if part.strip()]
            if parts and all(part in text for part in parts):
                return command
        elif key and key in text:
            return command
    return None


def resolve_source_collection(message: Message) -> str | None:
    if is_blocked_source(message):
        return None
    username = source_username(message)
    if username and username in BOT_SOURCE_COLLECTION:
        return BOT_SOURCE_COLLECTION[username]
    user_id = source_user_id(message)
    if user_id is not None and user_id in BOT_SOURCE_USER_ID:
        return BOT_SOURCE_USER_ID[user_id]
    chat_id = source_chat_id(message)
    if chat_id is not None and chat_id in BOT_SOURCE_CHAT_ID:
        return BOT_SOURCE_CHAT_ID[chat_id]
    title_collection = _title_to_collection(source_title(message))
    if title_collection:
        return title_collection
    content_collection, _ = _content_source(message)
    if content_collection:
        return content_collection
    custom_cmd = _custom_source_command(message)
    cols = collections_from_command(custom_cmd)
    return cols[0] if len(cols) == 1 else None


def resolve_lookup_scope(message: Message) -> LookupScope:
    if is_blocked_source(message):
        return LookupScope([], "blocked", strict=True, confident=True, source_label=source_username(message) or source_title(message))
    source_collection = resolve_source_collection(message)
    command = command_from_text(_message_text(message))
    _, content_command = _content_source(message)
    command = command or content_command
    label = source_username(message) or source_title(message)
    if source_collection:
        return LookupScope(
            [source_collection], "source", command or COLLECTION_TO_OUTPUT_COMMAND.get(source_collection),
            source_collection, settings.strict_forward_source_lookup, True, label,
        )
    command_collections = collections_from_command(command)
    if command_collections:
        return LookupScope(command_collections, "command", command, None, settings.strict_command_lookup, True, label)
    return LookupScope(None, "all", command=command, strict=False, confident=False, source_label=label)


def output_command_from_message(message: Message, collection: str | None = None) -> str | None:
    username = source_username(message)
    if username and username in BOT_SOURCE_OUTPUT_COMMAND:
        return BOT_SOURCE_OUTPUT_COMMAND[username]
    user_id = source_user_id(message)
    if user_id is not None and user_id in BOT_SOURCE_OUTPUT_USER_ID:
        return BOT_SOURCE_OUTPUT_USER_ID[user_id]
    title_command = _title_to_output_command(source_title(message))
    if title_command:
        return title_command
    _, content_command = _content_source(message)
    if content_command and (not collection or collection in collections_from_command(content_command)):
        return content_command
    custom_command = _custom_source_command(message)
    if custom_command and (not collection or collection in collections_from_command(custom_command)):
        return custom_command
    text_command = command_from_text(_message_text(message))
    if text_command and (not collection or collection in collections_from_command(text_command)):
        return text_command
    return COLLECTION_TO_OUTPUT_COMMAND.get(collection) if collection else None


def resolve_lookup_collections(message: Message) -> list[str] | None:
    return resolve_lookup_scope(message).collections


def resolve_collection(message: Message) -> str | None:
    cols = resolve_lookup_collections(message)
    return cols[0] if cols and len(cols) == 1 else None


def default_collection() -> str:
    return collection_from_command(settings.default_command) or "items_characters_hallow"


def all_lookup_collections() -> list[str]:
    return list(LOOKUP_COLLECTION_ORDER)

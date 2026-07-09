from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from dotenv import load_dotenv

load_dotenv(override=True)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _csv(name: str) -> List[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _ids(name: str) -> Set[int]:
    out: Set[int] = set()
    for raw in _csv(name):
        try:
            out.add(int(raw))
        except ValueError:
            pass
    return out


def _normalized_usernames(name: str, defaults: List[str] | None = None) -> Set[str]:
    values = _csv(name) or list(defaults or [])
    out: Set[str] = set()
    for raw in values:
        value = raw.strip().lower().lstrip("@")
        if value:
            out.add("@" + value)
    return out


def _sample_points(name: str, defaults: Tuple[float, ...]) -> Tuple[float, ...]:
    raw = _csv(name)
    if not raw:
        return defaults
    out: list[float] = []
    for item in raw:
        try:
            value = float(item)
            if value > 1:
                value /= 100.0
            out.append(max(0.0, min(1.0, value)))
        except ValueError:
            pass
    return tuple(out or defaults)


def _custom_forward_source_commands() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in _csv("FORWARD_SOURCE_COMMANDS"):
        if ":" not in pair:
            continue
        source, command = pair.split(":", 1)
        source = source.strip().lower()
        command = command.strip().lower()
        if source and command:
            out[source] = command
    return out


def _supported_bots() -> list[tuple[str, str]]:
    defaults = [
        ("@Character_Catcher_Bot", "/catch"),
        ("@Characters_Hallow_bot", "/hallow"),
        ("@CaptureCharacterBot", "/capture"),
        ("@Character_Seizer_Bot", "/seize"),
        ("@Husbando_Grabber_Bot", "/grab"),
        ("@Grab_Your_Waifu_Bot", "/grab"),
        ("@Grab_Your_Husbando_Bot", "/grab"),
        ("@Takers_character_bot", "/take"),
        ("@Catch_Your_Husbando_Bot", "/guess"),
        ("@Smash_Character_Bot", "/smash"),
        ("@WaifuxGrabBot", "/grab"),
        ("@Catch_Your_Waifu_Bot", "/guess"),
        ("@Waifu_Grabber_Bot", "/grab"),
        ("@CharacterLootBot", "/loot"),
        ("@roronoa_zoro_robot", "/challenge"),
        ("@character_picker_bot", "/pick"),
        ("@BikaCharacterBot", "/bika"),
        ("@SenpaiCatcherBot", "/pick"),
        ("@Super_zeko_bot", "/ziceko"),
        ("@orinx_catcher_waifu_bot", "/orin"),
        ("@ImmortalDonghuaBot", "/dao"),
    ]
    raw = _csv("STATUS_SUPPORTED_BOTS")
    if not raw:
        return defaults
    result: list[tuple[str, str]] = []
    for item in raw:
        if "|" in item:
            name, command = item.split("|", 1)
        elif ":" in item:
            name, command = item.rsplit(":", 1)
        else:
            continue
        name, command = name.strip(), command.strip()
        if name and command:
            result.append((name, command if command.startswith("/") else "/" + command))
    return result or defaults


COLLECTION_TO_OUTPUT_COMMAND: Dict[str, str] = {
    "items_character_catcher": "/catch",
    "items_characters_hallow": "/hallow",
    "items_capture_character": "/capture",
    "items_character_seizer": "/seize",
    "items_husbando_grabber": "/grab",
    "items_grab_your_waifu": "/grab",
    "items_grab_your_husbando": "/grab",
    "items_takers_character": "/take",
    "items_catch_your_husbando": "/guess",
    "items_smash_character": "/smash",
    "items_waifux_grab": "/grab",
    "items_catch_your_waifu": "/guess",
    "items_waifu_grabber": "/grab",
    "items_roronoa_zoro": "/challenge",
    "items_character_picker": "/pick",
    "items_bika_character": "/bika",
    "items_senpai_catcher": "/pick",
    "items_super_zeko": "/ziceko",
    "items_orinx_waifu": "/orin",
    "items_immortal_donghua": "/dao",
    "items_unknown": "/name",
}

COMMAND_TO_COLLECTION: Dict[str, str] = {
    "/catch": "items_character_catcher",
    "/hallow": "items_characters_hallow",
    "/capture": "items_capture_character",
    "/seize": "items_character_seizer",
    "/loot": "items_capture_character",
    "/take": "items_takers_character",
    "/smash": "items_smash_character",
    "/challenge": "items_roronoa_zoro",
    "/bika": "items_bika_character",
    "/ziceko": "items_super_zeko",
    "/orin": "items_orinx_waifu",
    "/dao": "items_immortal_donghua",
}

BOT_SOURCE_COLLECTION: Dict[str, str] = {
    "@character_catcher_bot": "items_character_catcher",
    "@characters_hallow_bot": "items_characters_hallow",
    "@hallowuploads": "items_characters_hallow",
    "@capturecharacterbot": "items_capture_character",
    "@capturedatabase": "items_capture_character",
    "@character_seizer_bot": "items_character_seizer",
    "@seizer_database": "items_character_seizer",
    "@characterlootbot": "items_capture_character",
    "@husbando_grabber_bot": "items_husbando_grabber",
    "@grab_your_waifu_bot": "items_grab_your_waifu",
    "@grab_your_husbando_bot": "items_grab_your_husbando",
    "@takers_character_bot": "items_takers_character",
    "@catch_your_husbando_bot": "items_catch_your_husbando",
    "@smash_character_bot": "items_smash_character",
    "@waifuxgrabbot": "items_waifux_grab",
    "@waifuxgrab_database": "items_waifux_grab",
    "@waifuxgrabdb": "items_waifux_grab",
    "@catch_your_waifu_bot": "items_catch_your_waifu",
    "@waifu_grabber_bot": "items_waifu_grabber",
    "@roronoa_zoro_robot": "items_roronoa_zoro",
    "@character_picker_bot": "items_character_picker",
    "@bikacharacterbot": "items_bika_character",
    "@senpaicatcherbot": "items_senpai_catcher",
    "@fafafawfawfa": "items_senpai_catcher",
    "@super_zeko_bot": "items_super_zeko",
    "@zicekodata_1": "items_super_zeko",
    "@orinx_catcher_waifu_bot": "items_orinx_waifu",
    "@timunagalaya": "items_orinx_waifu",
    "@immortaldonghuabot": "items_immortal_donghua",
}

BOT_SOURCE_CHAT_ID: Dict[int, str] = {
    -1003923540741: "items_bika_character",
    -1003860021274: "items_super_zeko",
    -1003598338404: "items_orinx_waifu",
}

BOT_SOURCE_USER_ID: Dict[int, str] = {
    6157455819: "items_character_catcher",
    8688011915: "items_characters_hallow",
    7686672468: "items_capture_character",
    7595626187: "items_character_seizer",
    6546492683: "items_husbando_grabber",
    5934263177: "items_grab_your_waifu",
    6212414747: "items_grab_your_husbando",
    7691496587: "items_takers_character",
    6763528462: "items_catch_your_husbando",
    8336201607: "items_smash_character",
    8649913814: "items_waifux_grab",
    6883098627: "items_catch_your_waifu",
    6195436879: "items_waifu_grabber",
    8359842815: "items_capture_character",
    5284997893: "items_roronoa_zoro",
    8307651649: "items_character_picker",
    8768750156: "items_bika_character",
    8532697507: "items_senpai_catcher",
    8534437620: "items_super_zeko",
    8685992652: "items_orinx_waifu",
    8928030201: "items_immortal_donghua",
}

BOT_SOURCE_OUTPUT_COMMAND: Dict[str, str] = {
    "@characterlootbot": "/loot",
    "@super_zeko_bot": "/ziceko",
    "@zicekodata_1": "/ziceko",
    "@orinx_catcher_waifu_bot": "/orin",
    "@timunagalaya": "/orin",
    "@immortaldonghuabot": "/dao",
}

BOT_SOURCE_OUTPUT_USER_ID: Dict[int, str] = {
    8359842815: "/loot",
    8534437620: "/ziceko",
    8685992652: "/orin",
    8928030201: "/dao",
}

HIDE_ID_RARITY_COMMANDS: Set[str] = {"/capture", "/seize", "/loot", "/challenge"}
SYSTEM_COLLECTIONS = {"sudo_users", "known_users", "known_groups", "user_modes", "settings", "items"}


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    bot_username: str = os.getenv("BOT_USERNAME", "").lstrip("@")
    owner_ids: Set[int] = field(default_factory=lambda: _ids("OWNER_IDS") | _ids("OWNER_ID"))
    sudo_ids: Set[int] = field(default_factory=lambda: _ids("SUDO_IDS"))

    # User-visible naming/branding. Every value falls back to Bika when env is omitted.
    brand_name: str = os.getenv("BRAND_NAME", "Bika") or "Bika"
    bot_display_name: str = os.getenv("BOT_DISPLAY_NAME", os.getenv("BRAND_NAME", "Bika")) or "Bika"
    powered_by_name: str = os.getenv("POWERED_BY_NAME", os.getenv("BRAND_NAME", "Bika")) or "Bika"
    powered_by_username: str = os.getenv("POWERED_BY_USERNAME", os.getenv("OWNER_USERNAME", "@Official_Bika"))
    status_title: str = os.getenv("STATUS_TITLE", f"{os.getenv('BRAND_NAME', 'Bika')} DATABASE STATUS")
    stats_title: str = os.getenv("STATS_TITLE", f"{os.getenv('BRAND_NAME', 'Bika')} BOT STATS")
    service_name: str = os.getenv("SERVICE_NAME", os.getenv("BRAND_NAME", "Bika")) or "Bika"

    mongo_uri: str = os.getenv("MONGO_URI", "")
    db_name: str = os.getenv("DB_NAME", "waifu_adding_v2")
    mongo_server_selection_timeout_ms: int = _int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 8000)
    mongo_connect_timeout_ms: int = _int("MONGO_CONNECT_TIMEOUT_MS", 8000)
    mongo_socket_timeout_ms: int = _int("MONGO_SOCKET_TIMEOUT_MS", 60000)

    mode: str = os.getenv("MODE", "polling").lower()
    use_webhook: bool = _bool("USE_WEBHOOK", False)
    public_url: str = os.getenv("PUBLIC_URL", "").rstrip("/")
    webhook_path: str = os.getenv("WEBHOOK_PATH", "/webhook")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "change-me")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)

    support_group_username: str = os.getenv("SUPPORT_GROUP_USERNAME", "")
    support_group_id: int = _int("SUPPORT_GROUP_ID", 0)
    force_join_channels: List[str] = field(default_factory=lambda: _csv("FORCE_JOIN_CHANNELS"))
    enable_force_join: bool = _bool("ENABLE_FORCE_JOIN", True)
    group_force_join_dm_only: bool = _bool("GROUP_FORCE_JOIN_DM_ONLY", True)
    force_join_dm_start_param: str = os.getenv("FORCE_JOIN_DM_START_PARAM", "forcejoin")
    force_join_positive_cache_seconds: int = _int("FORCE_JOIN_POSITIVE_CACHE_SECONDS", 86400)
    force_join_prompt_throttle_seconds: int = _int("FORCE_JOIN_PROMPT_THROTTLE_SECONDS", 60)

    enable_gapprove: bool = _bool("ENABLE_GAPPROVE", True)
    gapprove_cache_seconds: int = _int("GAPPROVE_CACHE_SECONDS", 300)
    auto_lookup_enabled: bool = _bool("AUTO_LOOKUP_ENABLED", True)
    auto_lookup_only_approved_groups: bool = _bool("AUTO_LOOKUP_ONLY_APPROVED_GROUPS", True)
    auto_lookup_in_support_group: bool = _bool("AUTO_LOOKUP_IN_SUPPORT_GROUP", True)
    auto_lookup_in_dm: bool = _bool("AUTO_LOOKUP_IN_DM", True)
    auto_lookup_reply_not_found: bool = _bool("AUTO_LOOKUP_REPLY_NOT_FOUND", True)
    auto_lookup_dedupe_album: bool = _bool("AUTO_LOOKUP_DEDUPE_ALBUM", False)
    auto_lookup_dedupe_ttl_seconds: int = _int("AUTO_LOOKUP_DEDUPE_TTL_SECONDS", 20)

    default_command: str = os.getenv("DEFAULT_COMMAND", "/hallow")
    show_source_in_result: bool = _bool("SHOW_SOURCE_IN_RESULT", False)
    enable_copy_buttons: bool = _bool("ENABLE_COPY_BUTTONS", True)
    fast_reply_mode: bool = _bool("FAST_REPLY_MODE", False)

    snapshot_startup_load: bool = _bool("SNAPSHOT_STARTUP_LOAD", True)
    snapshot_background_refresh: bool = _bool("SNAPSHOT_BACKGROUND_REFRESH", True)
    snapshot_incremental_sync_seconds: int = _int("SNAPSHOT_INCREMENTAL_SYNC_SECONDS", 10)
    snapshot_full_rebuild_seconds: int = _int("SNAPSHOT_FULL_REBUILD_SECONDS", 3600)
    snapshot_refresh_seconds: int = _int("SNAPSHOT_REFRESH_SECONDS", 300)  # legacy env compatibility

    result_cache_max_items: int = _int("RESULT_CACHE_MAX_ITEMS", 150000)
    result_cache_ttl_seconds: int = _int("RESULT_CACHE_TTL_SECONDS", 7200)
    miss_cache_ttl_seconds: int = _int("MISS_CACHE_TTL_SECONDS", 120)

    photo_phash_threshold: int = _int("PHOTO_PHASH_THRESHOLD", 8)
    photo_dhash_threshold: int = _int("PHOTO_DHASH_THRESHOLD", 12)
    waifux_photo_phash_threshold: int = _int("WAIFUX_PHASH_THRESHOLD", 16)
    photo_multi_score_min: float = _float("PHOTO_MULTI_SCORE_MIN", 0.80)
    global_photo_multi_score_min: float = _float("GLOBAL_PHOTO_MULTI_SCORE_MIN", 0.88)
    photo_max_candidates: int = _int("PHOTO_MAX_CANDIDATES", 2500)

    video_frame_threshold: int = _int("VIDEO_FRAME_THRESHOLD", 10)
    video_avg_threshold: int = _int("VIDEO_AVG_THRESHOLD", 12)
    waifux_video_frame_threshold: int = _int("WAIFUX_VIDEO_FRAME_THRESHOLD", 14)
    waifux_video_avg_threshold: int = _int("WAIFUX_VIDEO_AVG_THRESHOLD", 16)
    video_duration_tolerance_seconds: int = _int("VIDEO_DURATION_TOLERANCE_SECONDS", 4)
    video_sample_points: Tuple[float, ...] = field(default_factory=lambda: _sample_points("VIDEO_SAMPLE_POINTS", (0.2, 0.5, 0.8)))
    video_v3_sample_points: Tuple[float, ...] = field(default_factory=lambda: _sample_points("VIDEO_V3_SAMPLE_POINTS", (0.05, 0.15, 0.30, 0.50, 0.70, 0.85, 0.95)))

    max_concurrent_downloads: int = _int("MAX_CONCURRENT_DOWNLOADS", 10)
    max_concurrent_lookups: int = _int("MAX_CONCURRENT_LOOKUPS", 50)
    download_timeout_seconds: int = _int("DOWNLOAD_TIMEOUT_SECONDS", 20)

    strict_forward_source_lookup: bool = _bool("STRICT_FORWARD_SOURCE_LOOKUP", True)
    strict_command_lookup: bool = _bool("STRICT_COMMAND_LOOKUP", True)
    require_lookup_scope: bool = _bool("REQUIRE_LOOKUP_SCOPE", False)
    strict_exact_lookup_only: bool = _bool("STRICT_EXACT_LOOKUP_ONLY", False)
    enable_hash_fallback: bool = _bool("ENABLE_HASH_FALLBACK", True)
    v3_global_exact_fallback: bool = _bool("V3_GLOBAL_EXACT_FALLBACK", True)
    v3_global_similarity_fallback: bool = _bool("V3_GLOBAL_SIMILARITY_FALLBACK", True)

    blocked_source_user_ids: Set[int] = field(default_factory=lambda: _ids("BLOCKED_SOURCE_USER_IDS") or {8303168571})
    blocked_source_usernames: Set[str] = field(default_factory=lambda: _normalized_usernames("BLOCKED_SOURCE_USERNAMES", ["@CharactersCatcher_Bot"]))
    blocked_source_titles: List[str] = field(default_factory=lambda: _csv("BLOCKED_SOURCE_TITLES") or ["Character Catcher Bot [Beta]"])

    forward_source_commands: Dict[str, str] = field(default_factory=_custom_forward_source_commands)
    supported_bots: list[tuple[str, str]] = field(default_factory=_supported_bots)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    tz: str = os.getenv("TZ", "Asia/Yangon")


settings = Settings()

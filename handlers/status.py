from __future__ import annotations

import re
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from database.mongo import get_db
from services.group_access import is_owner_or_sudo
from services.lookup_service import lookup_service
from services.snapshot_cache import snapshot
from utils.perf import perf
from utils.telegram_safe import safe_reply

router = Router(name="status")
START_TIME = time.time()


def _fmt_int(value: int | None) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def _fmt_ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f} ms"


def _uptime() -> str:
    sec = int(time.time() - START_TIME)
    days, sec = divmod(sec, 86400)
    hours, sec = divmod(sec, 3600)
    minutes, seconds = divmod(sec, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _snapshot_age() -> str:
    age = snapshot.age_seconds()
    if age < 0:
        return "N/A"
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m {age % 60}s"
    return f"{age // 3600}h {(age % 3600) // 60}m"


async def _count(name: str) -> int:
    try:
        return await get_db()[name].count_documents({})
    except Exception:
        return 0


async def _count_gapproved() -> int:
    try:
        return await get_db()["settings"].count_documents({"key": {"$regex": r"^gapprove:"}, "enabled": True})
    except Exception:
        return 0


async def _count_groups() -> int:
    try:
        db = get_db()
        ids: set[int] = set()
        async for doc in db["known_groups"].find({}, {"chat_id": 1, "group_id": 1}):
            raw = doc.get("chat_id") or doc.get("group_id")
            if raw is not None:
                try:
                    ids.add(int(raw))
                except Exception:
                    pass
        async for doc in db["settings"].find({"key": {"$regex": r"^gapprove:"}, "enabled": True}, {"key": 1}):
            match = re.match(r"^gapprove:(-?\d+)$", str(doc.get("key") or ""))
            if match:
                ids.add(int(match.group(1)))
        return len(ids)
    except Exception:
        return 0


async def _ping_db() -> float | None:
    try:
        started = time.perf_counter()
        await get_db().command("ping")
        return (time.perf_counter() - started) * 1000
    except Exception:
        return None


async def _ping_bot(message: Message) -> float | None:
    try:
        started = time.perf_counter()
        await message.bot.get_me()
        return (time.perf_counter() - started) * 1000
    except Exception:
        return None


def _ram_info() -> tuple[str, str, str]:
    try:
        data: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2:
                    data[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", 0)
        used = max(total - available, 0)
        fmt = lambda value: f"{value / (1024 ** 3):.2f} GB"
        return fmt(used), fmt(available), fmt(total)
    except Exception:
        return "N/A", "N/A", "N/A"


async def build_status_text(message: Message) -> str:
    users = await _count("known_users")
    groups = await _count_groups()
    approved = await _count_gapproved()
    blacklisted = await _count("blacklisted_users")
    bot_ping = await _ping_bot(message)
    supported = [f"{idx}. {name} : {cmd}" for idx, (name, cmd) in enumerate(settings.supported_bots, start=1)]
    return (
        f"♻ {settings.status_title}\n"
        f"‣ Total Media : {_fmt_int(snapshot.count)}\n"
        f"‣ Total Users : {_fmt_int(users)}\n"
        f"‣ Total Groups : {_fmt_int(groups)}\n"
        f"‣ GApproved Groups : {_fmt_int(approved)}\n"
        f"‣ Blacklisted Users : {_fmt_int(blacklisted)}\n\n"
        "⚡ LOOKUP ENGINE V3\n"
        f"‣ Snapshot Age : {_snapshot_age()}\n"
        f"‣ Result Cache : {len(lookup_service.result_cache)} / {settings.result_cache_max_items}\n"
        f"‣ Bot Latency : {_fmt_ms(bot_ping)}\n"
        f"‣ Incremental Sync : {settings.snapshot_incremental_sync_seconds}s\n\n"
        "🤖 Supported Bot List\n" + "\n".join(supported)
    )


async def build_stats_text(message: Message) -> str:
    db_ping = await _ping_db()
    bot_ping = await _ping_bot(message)
    used, left, total = _ram_info()
    p = perf.snapshot()
    return (
        f"📊 {settings.stats_title}\n\n"
        f"‣ Uptime : {_uptime()}\n"
        f"‣ DB Ping : {_fmt_ms(db_ping)}\n"
        f"‣ Bot Ping : {_fmt_ms(bot_ping)}\n"
        f"‣ RAM Used : {used}\n"
        f"‣ RAM Left : {left}\n"
        f"‣ RAM Total : {total}\n\n"
        "⚡ LOOKUP ENGINE V3\n"
        f"‣ Snapshot Age : {_snapshot_age()}\n"
        f"‣ Total Media : {_fmt_int(snapshot.count)}\n"
        f"‣ Result Cache : {len(lookup_service.result_cache)}\n"
        f"‣ Lookup Hits : {_fmt_int(int(p.get('lookup_hits', 0)))}\n"
        f"‣ Lookup Misses : {_fmt_int(int(p.get('lookup_misses', 0)))}\n"
        f"‣ EMA latency : {float(p.get('lookup_ema_ms', 0)):.0f} ms"
    )


@router.message(Command("status"))
async def status_cmd(message: Message) -> None:
    await safe_reply(message, await build_status_text(message))


@router.message(Command("stats"))
async def stats_cmd(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_owner_or_sudo(user_id):
        await safe_reply(message, "❌ Owner only command.")
        return
    await safe_reply(message, await build_stats_text(message))

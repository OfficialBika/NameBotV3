from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from services.group_access import is_owner_or_sudo, remember_group_from_message, set_group_approved
from services.snapshot_cache import snapshot
from services.sqlite_fingerprint_index import sqlite_index
from utils.telegram_safe import safe_reply

router = Router(name="admin")


@router.message(Command("gapprove"))
async def gapprove(message: Message) -> None:
    if not is_owner_or_sudo(message.from_user.id if message.from_user else None):
        return
    if message.chat.type == "private":
        await safe_reply(message, "Use /gapprove inside the target group.")
        return
    await remember_group_from_message(message)
    await set_group_approved(message.chat.id, True, message=message)
    await safe_reply(message, "✅ This group is approved for auto lookup.")


@router.message(Command("gunapprove"))
async def gunapprove(message: Message) -> None:
    if not is_owner_or_sudo(message.from_user.id if message.from_user else None):
        return
    if message.chat.type == "private":
        await safe_reply(message, "Use /gunapprove inside the target group.")
        return
    await set_group_approved(message.chat.id, False, message=message)
    await safe_reply(message, "✅ This group is removed from auto lookup.")


@router.message(Command("refresh"))
async def refresh_lookup_index(message: Message) -> None:
    if not is_owner_or_sudo(message.from_user.id if message.from_user else None):
        return
    if settings.lookup_engine_mode == "sqlite":
        await safe_reply(message, "Syncing SQLite fingerprint index...")
        changed = await sqlite_index.incremental_sync()
        stats = await sqlite_index.stats()
        await safe_reply(message, f"✅ SQLite sync complete. Changed: {changed} | Items: {stats['items']}")
        return
    await safe_reply(message, "Refreshing V3 snapshot...")
    await snapshot.refresh()
    await safe_reply(message, f"✅ V3 snapshot refreshed. Items: {snapshot.count}")


@router.message(Command("rebuildindex"))
async def rebuild_sqlite_index(message: Message) -> None:
    if not is_owner_or_sudo(message.from_user.id if message.from_user else None):
        return
    if settings.lookup_engine_mode != "sqlite":
        await safe_reply(message, "ℹ️ LOOKUP_ENGINE_MODE is not sqlite.")
        return
    await safe_reply(message, "♻️ SQLite fingerprint index rebuild started. Exact Mongo lookup remains available.")
    total = await sqlite_index.build_full(clear_existing=True)
    await safe_reply(message, f"✅ SQLite fingerprint index rebuilt. Items: {total}")

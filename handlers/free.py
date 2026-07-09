from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import settings
from services.free_users import add_free_user, is_free_user, remove_free_user
from utils.telegram_safe import safe_reply

router = Router(name="free")


def _target(message: Message, args: str | None) -> tuple[int | None, str]:
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        return user.id, user.full_name or user.username or str(user.id)
    raw = (args or "").strip()
    return (int(raw), raw) if raw.isdigit() else (None, "")


def _owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in settings.owner_ids)


@router.message(Command("free"))
async def free_cmd(message: Message, command: CommandObject) -> None:
    if not _owner(message):
        return
    user_id, name = _target(message, command.args)
    if not user_id:
        await safe_reply(message, "Usage:\nReply user + /free\nor\n/free 123456789")
        return
    await add_free_user(user_id, message.from_user.id, "force_join_bypass")
    await safe_reply(message, f"✅ Force-join free added\nUser: {name}\nID: <code>{user_id}</code>", parse_mode="HTML")


@router.message(Command("unfree"))
async def unfree_cmd(message: Message, command: CommandObject) -> None:
    if not _owner(message):
        return
    user_id, name = _target(message, command.args)
    if not user_id:
        await safe_reply(message, "Usage:\nReply user + /unfree\nor\n/unfree 123456789")
        return
    removed = await remove_free_user(user_id)
    await safe_reply(message, f"{'✅ Removed' if removed else '⚠️ Not found'}\nUser: {name}\nID: <code>{user_id}</code>", parse_mode="HTML")


@router.message(Command("freecheck"))
async def freecheck_cmd(message: Message, command: CommandObject) -> None:
    if not _owner(message):
        return
    user_id, name = _target(message, command.args)
    if not user_id:
        await safe_reply(message, "Usage:\nReply user + /freecheck\nor\n/freecheck 123456789")
        return
    ok = await is_free_user(user_id)
    await safe_reply(message, f"User: {name}\nID: <code>{user_id}</code>\nFree: <b>{'YES' if ok else 'NO'}</b>", parse_mode="HTML")

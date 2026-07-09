from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

try:
    from aiogram.types import CopyTextButton
except Exception:  # pragma: no cover
    CopyTextButton = None  # type: ignore

from config import HIDE_ID_RARITY_COMMANDS, settings
from services.snapshot_cache import ItemSnapshot
from utils.text import first_token, h


def _copy_button(text: str, value: str) -> InlineKeyboardButton:
    if CopyTextButton is not None:
        return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value))
    return InlineKeyboardButton(text=text, callback_data="noop")


def format_result(item: ItemSnapshot) -> str:
    command = item.command or "/name"
    hide_id_rarity = command in HIDE_ID_RARITY_COMMANDS
    lines = [f"<b>NAME :</b> <code>{h(item.name)}</code>"]
    if item.card_id is not None and not hide_id_rarity:
        lines.append(f"<b>ID :</b> {h(item.card_id)}")
    if item.rarity and not hide_id_rarity:
        lines.append(f"<b>RARITY :</b> {h(item.rarity)}")
    if settings.show_source_in_result:
        lines.append(f"<b>SOURCE :</b> {h(command)}")
    hint = f"{command} {first_token(item.name)}"
    full = f"{command} {item.name}"
    lines += [
        "────────────────",
        f"🔹 <b>Hint :</b> <code>{h(hint)}</code>",
        f"🔸 <b>Full :</b> <code>{h(full)}</code>",
    ]
    username = (settings.powered_by_username or "").strip()
    name = settings.powered_by_name or "Bika"
    if username:
        username = username if username.startswith("@") else "@" + username
        lines.append(f'\nPowered by <a href="https://t.me/{h(username.lstrip("@"))}">{h(name)}</a>')
    else:
        lines.append(f"\nPowered by {h(name)}")
    return "\n".join(lines)


def result_buttons(item: ItemSnapshot) -> InlineKeyboardMarkup | None:
    if settings.fast_reply_mode or not settings.enable_copy_buttons:
        return None
    command = item.command or "/name"
    hint = f"{command} {first_token(item.name)}"
    full = f"{command} {item.name}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        _copy_button("📋 Copy Hint", hint),
        _copy_button("📋 Copy Full", full),
    ]])

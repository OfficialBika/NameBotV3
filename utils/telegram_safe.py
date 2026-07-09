from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message

log = logging.getLogger(__name__)


async def safe_answer(message: Message, text: str, **kwargs: Any) -> Message | None:
    try:
        return await message.answer(text, **kwargs)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            return await message.answer(text, **kwargs)
        except Exception:
            log.exception("safe_answer retry failed")
            return None
    except (TelegramBadRequest, TelegramForbiddenError):
        log.warning("safe_answer failed", exc_info=True)
        return None
    except Exception:
        log.exception("safe_answer unexpected error")
        return None


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message | None:
    try:
        return await message.reply(text, **kwargs)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            return await message.reply(text, **kwargs)
        except Exception:
            log.exception("safe_reply retry failed")
            return None
    except TelegramBadRequest:
        return await safe_answer(message, text, **kwargs)
    except TelegramForbiddenError:
        return None
    except Exception:
        log.exception("safe_reply unexpected error")
        return None

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from database.indexes import ensure_indexes
from database.mongo import close_mongo, init_mongo
from handlers import admin, auto_lookup, free, manual_lookup, start, status
from services.snapshot_cache import snapshot

try:
    import uvloop
except Exception:  # pragma: no cover
    uvloop = None

PROCESS_STARTED_AT = int(time.time())
log = logging.getLogger(__name__)


class DropOldUpdatesMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = None
        if isinstance(event, Message):
            message = event
        elif isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            message = event.message
        if message and message.date and int(message.date.timestamp()) < PROCESS_STARTED_AT:
            return None
        return await handler(event, data)


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    old_updates = DropOldUpdatesMiddleware()
    dp.message.outer_middleware(old_updates)
    dp.callback_query.outer_middleware(old_updates)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(status.router)
    dp.include_router(free.router)
    dp.include_router(manual_lookup.router)
    dp.include_router(auto_lookup.router)
    return dp


async def bootstrap_core(bot: Bot, *, webhook: bool) -> list[asyncio.Task]:
    """Heavy bootstrap. In webhook mode this runs only after the HTTP port is bound."""
    await init_mongo()
    await ensure_indexes()
    tasks: list[asyncio.Task] = []
    if settings.snapshot_startup_load:
        await snapshot.refresh()
    if settings.snapshot_background_refresh:
        tasks.append(asyncio.create_task(snapshot.refresh_loop(), name="snapshot-refresh-loop"))
    if webhook:
        if not settings.public_url:
            raise RuntimeError("PUBLIC_URL is required for webhook mode")
        webhook_path = settings.webhook_path if settings.webhook_path.startswith("/") else "/" + settings.webhook_path
        await bot.set_webhook(
            settings.public_url + webhook_path,
            secret_token=settings.webhook_secret,
            drop_pending_updates=False,
        )
        log.info("Webhook configured: %s%s", settings.public_url, webhook_path)
    return tasks


async def run_polling() -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    background: list[asyncio.Task] = []
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        background = await bootstrap_core(bot, webhook=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await close_mongo()
        await bot.session.close()


def _webhook_path() -> str:
    return settings.webhook_path if settings.webhook_path.startswith("/") else "/" + settings.webhook_path


async def run_webhook() -> None:
    """Render-friendly webhook runner.

    The old V2 app awaited Mongo ping + full snapshot load inside aiohttp startup,
    so Render could wait a long time before seeing an open PORT. V3 binds PORT first,
    serves /healthz immediately, then performs Mongo/snapshot bootstrap concurrently.
    Webhook updates receive HTTP 503 until readiness and Telegram retries them.
    """
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    path = _webhook_path()

    @web.middleware
    async def readiness_gate(request: web.Request, handler):
        if request.path == path and not request.app.get("ready", False):
            return web.json_response({"ok": False, "status": "starting"}, status=503)
        return await handler(request)

    app = web.Application(middlewares=[readiness_gate])
    app["ready"] = False
    app["bootstrap_error"] = ""

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": settings.service_name,
                "status": "ready" if app.get("ready") else "starting",
                "snapshot_items": snapshot.count,
                "snapshot_age_seconds": snapshot.age_seconds(),
                "error": app.get("bootstrap_error", ""),
            }
        )

    async def ready(_request: web.Request) -> web.Response:
        if app.get("ready"):
            return web.json_response({"ok": True, "status": "ready", "items": snapshot.count})
        return web.json_response(
            {"ok": False, "status": "starting", "error": app.get("bootstrap_error", "")},
            status=503,
        )

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
    ).register(app, path=path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app, access_log=None)
    background: list[asyncio.Task] = []
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        await site.start()
        log.info("HTTP PORT bound immediately on %s:%s; bootstrap starting", settings.host, settings.port)

        try:
            background = await bootstrap_core(bot, webhook=True)
            app["ready"] = True
            log.info("Webhook service READY | snapshot_items=%s", snapshot.count)
        except Exception as exc:
            app["bootstrap_error"] = str(exc)
            log.exception("Webhook bootstrap failed after port bind")
            raise

        await stop_event.wait()
    finally:
        app["ready"] = False
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        try:
            await close_mongo()
        finally:
            try:
                await bot.session.close()
            finally:
                await runner.cleanup()


def main() -> None:
    setup_logging()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty")
    if uvloop and sys.platform != "win32":
        uvloop.install()
    webhook = settings.use_webhook or settings.mode == "webhook"
    asyncio.run(run_webhook() if webhook else run_polling())


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot stopped")

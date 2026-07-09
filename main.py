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
from services.lookup_backend import lookup_backend
from services.snapshot_cache import snapshot
from services.sqlite_fingerprint_index import sqlite_index

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
    """Initialize the selected lookup backend after HTTP bind in webhook mode."""
    await init_mongo()
    await ensure_indexes()
    tasks: list[asyncio.Task] = []

    if lookup_backend.mode == "sqlite":
        # Open is fast. Initial/full index build runs in the background, so exact Mongo
        # lookup and Telegram handling can start immediately.
        await sqlite_index.open()
        if settings.sqlite_sync_seconds > 0:
            # sync_loop performs the initial background build when required, then delta syncs.
            tasks.append(asyncio.create_task(sqlite_index.sync_loop(), name="sqlite-sync-loop"))
        elif settings.sqlite_build_on_start:
            tasks.append(asyncio.create_task(sqlite_index.ensure_built(), name="sqlite-initial-build"))
        log.info("Lookup engine selected: SQLITE hybrid path=%s", settings.sqlite_index_path)
    else:
        if settings.snapshot_startup_load:
            await snapshot.refresh()
        if settings.snapshot_background_refresh:
            tasks.append(asyncio.create_task(snapshot.refresh_loop(), name="snapshot-refresh-loop"))
        log.info("Lookup engine selected: SNAPSHOT RAM items=%s", snapshot.count)

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


async def shutdown_core(background: list[asyncio.Task]) -> None:
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    if lookup_backend.mode == "sqlite":
        await sqlite_index.close()
    await close_mongo()


async def run_polling() -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    background: list[asyncio.Task] = []
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        background = await bootstrap_core(bot, webhook=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await shutdown_core(background)
        await bot.session.close()


def _webhook_path() -> str:
    return settings.webhook_path if settings.webhook_path.startswith("/") else "/" + settings.webhook_path


async def run_webhook() -> None:
    """Render-friendly runner: bind HTTP first, then bootstrap Mongo/backend/webhook."""
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
        engine = await lookup_backend.stats()
        return web.json_response(
            {
                "ok": True,
                "service": settings.service_name,
                "status": "ready" if app.get("ready") else "starting",
                "lookup_engine": engine,
                "error": app.get("bootstrap_error", ""),
            }
        )

    async def ready(_request: web.Request) -> web.Response:
        engine = await lookup_backend.stats()
        if app.get("ready"):
            return web.json_response({"ok": True, "status": "ready", "lookup_engine": engine})
        return web.json_response(
            {"ok": False, "status": "starting", "lookup_engine": engine, "error": app.get("bootstrap_error", "")},
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
            engine = await lookup_backend.stats()
            log.info("Webhook service READY | engine=%s items=%s", engine.get("mode"), engine.get("items"))
        except Exception as exc:
            app["bootstrap_error"] = str(exc)
            log.exception("Webhook bootstrap failed after port bind")
            raise

        await stop_event.wait()
    finally:
        app["ready"] = False
        try:
            await shutdown_core(background)
        finally:
            try:
                await bot.session.close()
            finally:
                await runner.cleanup()


def main() -> None:
    setup_logging()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty")
    if settings.lookup_engine_mode not in {"snapshot", "sqlite"}:
        log.warning("Invalid LOOKUP_ENGINE_MODE=%s; snapshot fallback will be used", settings.lookup_engine_mode)
    if uvloop and sys.platform != "win32":
        uvloop.install()
    webhook = settings.use_webhook or settings.mode == "webhook"
    asyncio.run(run_webhook() if webhook else run_polling())


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot stopped")

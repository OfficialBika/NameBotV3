from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from config import settings

log = logging.getLogger(__name__)
_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def init_mongo() -> AsyncIOMotorDatabase:
    global _client, _db
    if _client is not None and _db is not None:
        return _db
    if not settings.mongo_uri:
        raise RuntimeError("MONGO_URI is missing")
    _client = AsyncIOMotorClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
        connectTimeoutMS=settings.mongo_connect_timeout_ms,
        socketTimeoutMS=settings.mongo_socket_timeout_ms,
        maxIdleTimeMS=120000,
        retryWrites=True,
    )
    _db = _client[settings.db_name]
    await _db.command("ping")
    log.info("Mongo connected: db=%s", settings.db_name)
    return _db


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("MongoDB is not initialized. Call init_mongo() first.")
    return _db


async def close_mongo() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None

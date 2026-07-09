import logging

logger = logging.getLogger(__name__)


async def ensure_indexes(*args, **kwargs):
    """Lookup bot is read-only; Adding Bot owns the data/index schema."""
    logger.info("Mongo indexes skipped: read-only mode enabled")

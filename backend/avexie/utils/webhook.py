import logging

log = logging.getLogger(__name__)


async def post_webhook(name: str, url: str, message: str, event_data: dict, description: str | None = None) -> bool:
    """Webhook dispatch has been removed. This stub keeps callers from breaking."""
    log.debug('post_webhook stub called (webhooks disabled): %s', url)
    return False

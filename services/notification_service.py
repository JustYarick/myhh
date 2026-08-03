import logging
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

_notify_callback: Optional[Callable[[str], Awaitable[None]]] = None
_notify_photo_callback: Optional[Callable[[bytes, str], Awaitable[None]]] = None


def set_text_callback(callback: Callable[[str], Awaitable[None]]) -> None:
    global _notify_callback
    _notify_callback = callback


def set_photo_callback(callback: Callable[[bytes, str], Awaitable[None]]) -> None:
    global _notify_photo_callback
    _notify_photo_callback = callback


async def notify(text: str) -> None:
    if _notify_callback:
        try:
            await _notify_callback(text)
        except Exception as e:
            logger.error(f"Failed to send text notification: {e}")


async def notify_photo(photo_bytes: bytes, caption: str) -> None:
    if _notify_photo_callback:
        try:
            await _notify_photo_callback(photo_bytes, caption)
        except Exception as e:
            logger.error(f"Failed to send photo notification: {e}")

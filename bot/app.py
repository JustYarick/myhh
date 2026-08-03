import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from ..config import get_settings
from .handlers import router
from .middleware import AccessMiddleware

logger = logging.getLogger(__name__)


def create_bot_and_dispatcher():
    settings = get_settings()

    if not settings.tg_bot_token:
        raise ValueError("TG_BOT_TOKEN not configured!")

    session = None
    if settings.tg_proxy_url:
        session = AiohttpSession(proxy=settings.tg_proxy_url)
        logger.info(f"TG bot using proxy: {settings.tg_proxy_url}")

    bot = Bot(token=settings.tg_bot_token, session=session)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(router)

    logger.info("Telegram bot initialized")
    return bot, dp
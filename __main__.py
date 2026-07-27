import asyncio
import logging
import sys
from typing import Optional, Callable, Awaitable

from .config import get_settings
from .database import init_db
from .services.browser import browser_manager
from .services.flow_entity import init_flow_table


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("autohh").setLevel(logging.DEBUG)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.INFO)
logging.getLogger("playwright").setLevel(logging.WARNING)
logger = logging.getLogger("autohh")

_error_callback: Optional[Callable[[str], Awaitable[None]]] = None
_bot_instance = None


def get_bot():
    return _bot_instance


def get_error_callback() -> Optional[Callable[[str], Awaitable[None]]]:
    return _error_callback


async def send_error_alert(user_id: int, text: str) -> None:
    bot = get_bot()
    if bot:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"Alert: {text}",
            )
        except Exception as e:
            logger.error(f"Failed to send alert to {user_id}: {e}")


async def _on_startup(bot) -> None:
    global _bot_instance
    _bot_instance = bot

    await init_db()
    await init_flow_table()
    settings = get_settings()
    await browser_manager.start()

    from .services.gemini import gemini_service
    from .services.flow_entity import get_setting
    saved_model = await get_setting("gemini_model", "")
    if saved_model:
        gemini_service.set_model(saved_model)
        logger.info(f"Gemini model: {saved_model}")

    from .scheduler import scheduler

    async def notify_callback(message: str) -> None:
        for uid in settings.allowed_user_ids:
            try:
                await bot.send_message(chat_id=uid, text=message, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify {uid}: {e}")

    async def error_callback(message: str) -> None:
        for uid in settings.allowed_user_ids:
            await send_error_alert(uid, message)

    scheduler.set_notify_callback(notify_callback)

    me = await bot.get_me()
    logger.info(f"Bot connected: @{me.username}")

    for uid in settings.allowed_user_ids:
        try:
            await bot.send_message(chat_id=uid, text="🤖 <b>AutoHH bot started!</b>", parse_mode="HTML")
            logger.info(f"Sent startup message to {uid}")
        except Exception as e:
            logger.error(f"Failed to notify {uid}: {e}")


async def _on_shutdown(bot) -> None:
    await browser_manager.stop()
    await bot.session.close()
    logger.info("Browser stopped, bot shut down.")


def run_bot() -> None:
    settings = get_settings()
    settings.ensure_dirs()

    from .bot.app import create_bot_and_dispatcher

    bot, dp = create_bot_and_dispatcher()

    async def _main():
        await _on_startup(bot)
        try:
            logger.info("Starting AutoHH bot polling...")
            await dp.start_polling(bot)
        finally:
            await _on_shutdown(bot)

    asyncio.run(_main())


def main() -> None:
    run_bot()


if __name__ == "__main__":
    main()

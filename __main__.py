import asyncio
import logging
import sys
from typing import Optional, Callable, Awaitable
from loguru import logger

from .config import get_settings
from .database import init_db, get_setting
from .services.browser import browser_manager
from .services.flow_entity import init_flow_table

# Intercept standard library logging and pass to loguru
class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

# Configure standard logging to redirect to loguru InterceptHandler
settings = get_settings()
log_level = settings.log_level.upper()
std_level = getattr(logging, log_level, logging.INFO)
logging.basicConfig(handlers=[InterceptHandler()], level=std_level)

# Configure loguru
logger.remove()
logger.add(
    sys.stderr,
    level=log_level,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

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
    saved_model = await get_setting("gemini_model", "")
    if saved_model:
        gemini_service.set_model(saved_model)
        logger.info(f"Gemini model: {saved_model}")

    from .scheduler import set_notify_callback

    async def notify_callback(message: str) -> None:
        for uid in settings.allowed_user_ids:
            try:
                await bot.send_message(chat_id=uid, text=message, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify {uid}: {e}")

    async def error_callback(message: str) -> None:
        for uid in settings.allowed_user_ids:
            await send_error_alert(uid, message)

    set_notify_callback(notify_callback)

    me = await bot.get_me()
    logger.info(f"Bot connected: @{me.username}")

    monitoring_active = (await get_setting("monitoring_mode", "false")) == "true"
    startup_text = "🤖 <b>AutoHH bot started!</b>"
    if monitoring_active:
        startup_text += "\n🔄 <b>Сервер перезапущен.</b> Восстанавливаю фоновый мониторинг вакансий..."
        logger.warning("System restart detected. Monitoring mode was active, recovering daemon loop.")

    for uid in settings.allowed_user_ids:
        try:
            await bot.send_message(chat_id=uid, text=startup_text, parse_mode="HTML")
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

import logging
from aiogram import Router

from ...config import get_settings
from ...services.hh_auth import hh_auth
from ...services import flow_entity as flow_db

logger = logging.getLogger(__name__)

router = Router()


async def _check_access(user_id: int) -> bool:
    settings = get_settings()
    return settings.is_allowed_user(user_id)


async def _error_reply(message, text: str) -> None:
    try:
        await message.answer(f"Error: {text}")
    except Exception:
        logger.error(f"Failed to send error to {message.from_user.id}: {text}")


async def _main_menu_message(message) -> None:
    from ..keyboards import main_menu_reply_keyboard
    from ...scheduler import scheduler
    run_state = scheduler._run_state.value
    await message.answer(
        "🤖 <b>AutoHH Bot Menu</b>",
        parse_mode="HTML",
        reply_markup=main_menu_reply_keyboard(run_state),
    )


async def _main_menu_callback(callback) -> None:
    from ..keyboards import main_menu_reply_keyboard
    from ...scheduler import scheduler
    run_state = scheduler._run_state.value
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🤖 <b>AutoHH Bot Menu</b>",
        parse_mode="HTML",
        reply_markup=main_menu_reply_keyboard(run_state),
    )


from .common import common_router
from .auth import auth_router
from .flows import flows_router
from .settings import settings_router

router.include_routers(
    common_router,
    auth_router,
    flows_router,
    settings_router,
)

__all__ = ["router", "_check_access", "_error_reply", "_main_menu_message", "_main_menu_callback"]

import logging
from aiogram import Router

from ...config import get_settings
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
    from ...scheduler import manual_scheduler, monitoring_scheduler, RunState
    from ...services.flow_entity import get_active_flow

    manual_state = manual_scheduler.get_status_text()
    if monitoring_scheduler._run_state != RunState.IDLE:
        monitoring_state = "🟢 активен"
    else:
        monitoring_enabled = (await flow_db.get_setting("monitoring_mode", "false")) == "true"
        monitoring_state = "🟢 включен" if monitoring_enabled else "🔴 выключен"

    active_flow = await get_active_flow()
    flow_line = f"📂 Поток: <b>{active_flow.name}</b>" if active_flow else "📂 Поток: <b>не выбран</b>"

    await message.answer(
        f"🤖 <b>AutoHH — главное меню</b>\n\n"
        f"{flow_line}\n"
        f"▶️ Ручной режим: {manual_state}\n"
        f"🔍 Мониторинг: <b>{monitoring_state}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_reply_keyboard(),
    )


async def _main_menu_callback(callback) -> None:
    from ..keyboards import main_menu_reply_keyboard
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _main_menu_message(callback.message)


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

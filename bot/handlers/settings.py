import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery

from ...services.hh_auth import hh_auth

logger = logging.getLogger(__name__)
settings_router = Router()


async def _check_access(user_id: int) -> bool:
    from ...config import get_settings
    return get_settings().is_allowed_user(user_id)


async def _main_menu_callback(callback: CallbackQuery) -> None:
    from . import _main_menu_callback as _impl
    await _impl(callback)


@settings_router.callback_query(F.data == "settings")
async def settings_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    from ...services.flow_entity import get_setting
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    hh_ok = hh_auth.session_exists()
    from ..keyboards import settings_keyboard
    await callback.message.edit_text(
        "<b>Global Settings</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok),
    )


@settings_router.callback_query(F.data == "settings_model")
async def settings_model_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer("Loading models...")
    from ...services.gemini import list_models
    from ...config import get_settings
    from ...services.flow_entity import get_setting
    settings = get_settings()
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    models = list_models(settings.gemini_api_key)
    if not models:
        await callback.answer("Failed to fetch models. Check API key.", show_alert=True)
        return
    from ..keyboards import model_list_keyboard
    await callback.message.edit_text(
        "<b>Select Gemini Model</b>\n* = current",
        parse_mode="HTML",
        reply_markup=model_list_keyboard(models, gemini_model),
    )


@settings_router.callback_query(F.data.startswith("settings_set_model_"))
async def settings_set_model_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    model = callback.data.replace("settings_set_model_", "")
    from ...services.flow_entity import set_setting
    from ...services.gemini import gemini_service
    await set_setting("gemini_model", model)
    gemini_service.set_model(model)
    await callback.answer(f"Model set: {model}", show_alert=True)
    from ..keyboards import settings_keyboard
    hh_ok = hh_auth.session_exists()
    from ...services.flow_entity import get_setting
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    await callback.message.edit_text(
        "<b>Global Settings</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok),
    )


@settings_router.callback_query(F.data == "settings_hh")
async def settings_hh_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    hh_ok = hh_auth.session_exists()
    if hh_ok:
        text = "<b>HH Account</b>\n\nStatus: Linked"
    else:
        text = "<b>HH Account</b>\n\nStatus: Not linked\nUse /start to login"
    from ..keyboards import back_keyboard
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_keyboard("settings"),
    )

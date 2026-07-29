import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

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
    tz_offset = int(await get_setting("monitoring_timezone_offset", "3"))
    hh_ok = hh_auth.session_exists()
    from ..keyboards import settings_keyboard
    await callback.message.edit_text(
        "<b>Global Settings</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok, tz_offset=tz_offset),
    )


@settings_router.callback_query(F.data == "settings_model")
async def settings_model_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    
    await callback.message.edit_text(
        "⚙️ <b>Global Settings</b>\n\n⏳ <i>Fetching available Gemini models...</i>",
        parse_mode="HTML"
    )
    
    from ...services.gemini import list_models
    from ...config import get_settings
    from ...services.flow_entity import get_setting
    settings = get_settings()
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    models = list_models(settings.gemini_api_key)
    if not models:
        await callback.message.edit_text(
            "<b>Global Settings</b>\n\n❌ Failed to fetch models. Check Gemini API key.",
            parse_mode="HTML"
        )
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
    tz_offset = int(await get_setting("monitoring_timezone_offset", "3"))
    await callback.message.edit_text(
        "<b>Global Settings</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok, tz_offset=tz_offset),
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


async def send_global_settings(message: Message) -> None:
    from ...services.flow_entity import get_setting
    from ..keyboards import settings_reply_keyboard
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    tz_offset = int(await get_setting("monitoring_timezone_offset", "3"))
    hh_ok = hh_auth.session_exists()
    hh_status = "Linked" if hh_ok else "Not linked"
    text = (
        f"⚙️ <b>Global Settings</b>\n\n"
        f"🤖 Gemini model: <code>{gemini_model}</code>\n"
        f"🔑 HH Account: <b>{hh_status}</b>\n"
        f"🌐 Часовой пояс: <b>UTC{'+' if tz_offset >= 0 else ''}{tz_offset}</b>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_reply_keyboard(hh_ok, tz_offset=tz_offset),
    )


@settings_router.message(F.text == "⬅️ Back to Settings")
async def back_to_settings_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await send_global_settings(message)


@settings_router.message(F.text == "🤖 Choose Gemini Model")
async def settings_choose_model_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.gemini import list_models
    from ...config import get_settings
    from ...services.flow_entity import get_setting
    from ..keyboards import models_reply_keyboard
    settings = get_settings()
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    models = list_models(settings.gemini_api_key)
    if not models:
        await message.answer("Failed to fetch models. Check API key.")
        return
    await message.answer(
        "<b>Select Gemini Model</b>\n⭐ = current",
        parse_mode="HTML",
        reply_markup=models_reply_keyboard(models, gemini_model),
    )


@settings_router.message(F.text.startswith("Model: "))
async def settings_set_model_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    model = message.text.replace("Model: ", "").replace("⭐ ", "").strip()
    from ...services.flow_entity import set_setting
    from ...services.gemini import gemini_service
    await set_setting("gemini_model", model)
    gemini_service.set_model(model)
    await message.answer(f"✅ Gemini model updated to: <b>{model}</b>", parse_mode="HTML")
    await back_to_settings_message(message)


@settings_router.message(F.text == "🧹 Clear Cache")
async def clear_cache_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    try:
        await db.clear_vacancy_cache()
        await message.answer("🧹 <b>Vacancy cache cleared successfully!</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to clear cache: {e}")
        await message.answer(f"❌ Failed to clear cache: {e}")
    await back_to_settings_message(message)


@settings_router.message(F.text == "🔔 Notifications")
async def notifications_settings_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.flow_entity import get_setting
    from ..keyboards import notifications_keyboard
    
    success = (await get_setting("notify_success", "true")) == "true"
    error = (await get_setting("notify_error", "true")) == "true"
    skip = (await get_setting("notify_skip", "false")) == "true"
    
    await message.answer(
        "🔔 <b>Notification Settings</b>\n\nConfigure which events trigger Telegram notifications:",
        parse_mode="HTML",
        reply_markup=notifications_keyboard(success, error, skip)
    )


@settings_router.callback_query(F.data.startswith("toggle_notify_"))
async def toggle_notification_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    key = callback.data.replace("toggle_", "")
    from ...services.flow_entity import get_setting, set_setting
    from ..keyboards import notifications_keyboard
    
    current = (await get_setting(key, "true" if key != "notify_skip" else "false")) == "true"
    new_val = "false" if current else "true"
    await set_setting(key, new_val)
    
    success = (await get_setting("notify_success", "true")) == "true"
    error = (await get_setting("notify_error", "true")) == "true"
    skip = (await get_setting("notify_skip", "false")) == "true"
    
    await callback.answer(f"{key.replace('notify_', '').capitalize()} toggled {'ON' if new_val == 'true' else 'OFF'}")
    await callback.message.edit_reply_markup(
        reply_markup=notifications_keyboard(success, error, skip)
    )


@settings_router.message(F.text == "🔄 Сбросить лимиты")
async def reset_limits_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        from ...database import reset_today_limits
        await reset_today_limits()
        await message.answer("🔄 <b>Лимиты на сегодня успешно сброшены!</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to reset limits: {e}")
        await message.answer(f"❌ Не удалось сбросить лимиты: {e}")


@settings_router.message(F.text.startswith("🔍 Мониторинг:"))
async def toggle_monitoring_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    current = (await get_setting("monitoring_mode", "false")) == "true"
    new_val = "false" if current else "true"
    await set_setting("monitoring_mode", new_val)
    
    monitoring_enabled = new_val == "true"
    monitoring_status = "ВКЛЮЧЕН" if monitoring_enabled else "ВЫКЛЮЧЕН"
    
    from ...services.hh_auth import hh_auth
    from ..keyboards import settings_reply_keyboard
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    tz_offset = int(await get_setting("monitoring_timezone_offset", "3"))
    hh_ok = hh_auth.session_exists()
    hh_status = "Linked" if hh_ok else "Not linked"
    
    text = (
        f"⚙️ <b>Global Settings</b>\n\n"
        f"🤖 Gemini model: <code>{gemini_model}</code>\n"
        f"🔑 HH Account: <b>{hh_status}</b>\n"
        f"🔍 Фоновый мониторинг: <b>{monitoring_status}</b> (каждые 30 минут)\n"
        f"🌐 Часовой пояс: <b>UTC{'+' if tz_offset >= 0 else ''}{tz_offset}</b>"
    )
    
    await message.answer(
        f"🔍 Режим мониторинга теперь: <b>{monitoring_status}</b>.",
        parse_mode="HTML"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_reply_keyboard(hh_ok, tz_offset=tz_offset),
    )


@settings_router.callback_query(F.data == "settings_tz")
async def settings_tz_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    from ...services.flow_entity import get_setting
    from ..keyboards import timezone_select_keyboard
    current = int(await get_setting("monitoring_timezone_offset", "3"))
    
    await callback.message.edit_text(
        "🌐 <b>Выбор часового пояса:</b>\nВыберите часовой пояс относительно UTC для корректного времени работы мониторинга.",
        parse_mode="HTML",
        reply_markup=timezone_select_keyboard(current),
    )


@settings_router.message(F.text.startswith("🌐 Часовой пояс:"))
async def settings_timezone_message_handler(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.flow_entity import get_setting
    from ..keyboards import timezone_select_keyboard
    current = int(await get_setting("monitoring_timezone_offset", "3"))
    
    await message.answer(
        "🌐 <b>Выбор часового пояса:</b>\nВыберите часовой пояс относительно UTC для корректного времени работы мониторинга.",
        parse_mode="HTML",
        reply_markup=timezone_select_keyboard(current),
    )


@settings_router.callback_query(F.data.startswith("set_tz_"))
async def set_timezone_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    from ...services.flow_entity import get_setting, set_setting
    new_val = int(callback.data.split("_")[-1])
    await set_setting("monitoring_timezone_offset", str(new_val))
    
    await callback.answer(f"Часовой пояс изменен на UTC{'+' if new_val >= 0 else ''}{new_val}")
    
    # Reload settings view
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    hh_ok = hh_auth.session_exists()
    from ..keyboards import settings_keyboard
    
    await callback.message.edit_text(
        "<b>Global Settings</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok, tz_offset=new_val),
    )


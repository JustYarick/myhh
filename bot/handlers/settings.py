import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

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
    from ...services.hh_api_client import hh_api
    hh_ok = hh_api.is_authenticated
    from ..keyboards import settings_keyboard
    await callback.message.edit_text(
        "⚙️ <b>Глобальные настройки</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok, tz_offset=tz_offset),
    )


@settings_router.callback_query(F.data == "settings_model")
async def settings_model_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    
    await callback.message.edit_text(
        "⚙️ <b>Глобальные настройки</b>\n\n⏳ <i>Получаю доступные модели Gemini...</i>",
        parse_mode="HTML"
    )
    
    from ...services.gemini import list_models
    from ...config import get_settings
    from ...services.flow_entity import get_setting
    settings = get_settings()
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    models = await list_models(settings.gemini_api_key)
    if not models:
        await callback.message.edit_text(
            "⚙️ <b>Глобальные настройки</b>\n\n❌ Не удалось получить модели. Проверьте API-ключ Gemini.",
            parse_mode="HTML"
        )
        return
    from ..keyboards import model_list_keyboard
    await callback.message.edit_text(
        "⚙️ <b>Выберите модель Gemini</b>\n⭐ = текущая",
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
    await callback.answer(f"✅ Модель установлена: {model}", show_alert=True)
    from ..keyboards import settings_keyboard
    from ...services.hh_api_client import hh_api
    hh_ok = hh_api.is_authenticated
    from ...services.flow_entity import get_setting
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    tz_offset = int(await get_setting("monitoring_timezone_offset", "3"))
    await callback.message.edit_text(
        "⚙️ <b>Глобальные настройки</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok, tz_offset=tz_offset),
    )


@settings_router.callback_query(F.data == "settings_hh")
async def settings_hh_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    from ...services.hh_api_client import hh_api
    hh_ok = hh_api.is_authenticated
    if hh_ok:
        text = "<b>Аккаунт HH.ru</b>\n\nСтатус: Подключен ✅"
    else:
        text = "<b>Аккаунт HH.ru</b>\n\nСтатус: Не подключен ❌\nПерейдите в <b>⚙️ Настройки → 📱 Авторизация HH (API)</b> для входа."
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
    from ...services.hh_api_client import hh_api
    await hh_api.load_token()
    api_ok = hh_api.is_authenticated
    api_status = "✅ Активен" if api_ok else "❌ Не настроен"
    text = (
        f"⚙️ <b>Глобальные настройки</b>\n\n"
        f"🤖 Модель Gemini: <code>{gemini_model}</code>\n"
        f"📱 HH API токен: <b>{api_status}</b>\n"
        f"🌐 Часовой пояс: <b>UTC{'+' if tz_offset >= 0 else ''}{tz_offset}</b>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_reply_keyboard(api_ok=api_ok, tz_offset=tz_offset),
    )


@settings_router.message(F.text.in_({"⬅️ Back to Settings", "⬅️ Назад в Настройки"}))
async def back_to_settings_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await send_global_settings(message)


@settings_router.message(F.text.in_({"🤖 Choose Gemini Model", "🤖 Выбрать модель Gemini"}))
async def settings_choose_model_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.gemini import list_models
    from ...config import get_settings
    from ...services.flow_entity import get_setting
    from ..keyboards import models_reply_keyboard
    settings = get_settings()
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    models = await list_models(settings.gemini_api_key)
    if not models:
        await message.answer("Ошибка получения моделей. Проверьте API-ключ.")
        return
    await message.answer(
        "<b>Выберите модель Gemini</b>\n⭐ = текущая",
        parse_mode="HTML",
        reply_markup=models_reply_keyboard(models, gemini_model),
    )


@settings_router.message(F.text.startswith("Model: ") | F.text.startswith("Модель: "))
async def settings_set_model_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    model = message.text.replace("Model: ", "").replace("Модель: ", "").replace("⭐ ", "").strip()
    from ...services.flow_entity import set_setting
    from ...services.gemini import gemini_service
    await set_setting("gemini_model", model)
    gemini_service.set_model(model)
    await message.answer(f"✅ Модель Gemini обновлена на: <b>{model}</b>", parse_mode="HTML")
    await back_to_settings_message(message)


@settings_router.message(F.text.in_({"🧹 Clear Cache", "🧹 Очистить кэш"}))
async def clear_cache_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    try:
        await db.clear_vacancy_cache()
        await message.answer("🧹 <b>Кэш вакансий успешно очищен!</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to clear cache: {e}")
        await message.answer(f"❌ Не удалось очистить кэш: {e}")
    await back_to_settings_message(message)


@settings_router.message(F.text.in_({"🔔 Notifications", "🔔 Уведомления"}))
async def notifications_settings_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.flow_entity import get_setting
    from ..keyboards import notifications_keyboard
    
    success = (await get_setting("notify_success", "true")) == "true"
    error = (await get_setting("notify_error", "true")) == "true"
    skip = (await get_setting("notify_skip", "false")) == "true"
    
    await message.answer(
        "🔔 <b>Настройки уведомлений</b>\n\nВыберите, какие события будут отправлять уведомления в Telegram:",
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
    
    await callback.answer(f"{key.replace('notify_', '').capitalize()} переключен {'ВКЛ' if new_val == 'true' else 'ВЫКЛ'}")
    await callback.message.edit_reply_markup(
        reply_markup=notifications_keyboard(success, error, skip)
    )


@settings_router.message(F.text == "🩺 Проверить подключения")
async def health_check_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    from ...services.hh_api_client import hh_api
    from ...config import get_settings

    lines = ["🩺 <b>Проверка подключений</b>\n"]

    await message.answer(text="🩺 Проверяю подключения...")

    # HH API
    await hh_api.load_token()
    if hh_api.is_authenticated:
        try:
            me = await hh_api.get_me()
            name = (me.get("first_name") or "") + " " + (me.get("last_name") or "")
            lines.append(f"📱 HH.ru API: <b>ОК</b> (авторизован как {name.strip()})")
        except Exception as e:
            lines.append(f"📱 HH.ru API: <b>Ошибка запроса</b> — {e}")
    else:
        lines.append("📱 HH.ru API: <b>Нет токена</b> — выполните авторизацию в настройках")

    # Gemini
    settings = get_settings()
    try:
        from ...services.gemini import list_models
        models = await list_models(settings.gemini_api_key)
        if models:
            lines.append(f"🤖 Gemini API: <b>ОК</b> ({len(models)} моделей доступно)")
        else:
            lines.append("🤖 Gemini API: <b>Пустой ответ</b> — проверьте ключ")
    except Exception as e:
        lines.append(f"🤖 Gemini API: <b>Ошибка</b> — {e}")

    # Database
    try:
        today = await db.get_today_stats()
        lines.append(f"🗄 База данных: <b>ОК</b> (статистика за {today['date']})")
    except Exception as e:
        lines.append(f"🗄 База данных: <b>Ошибка</b> — {e}")

    # Active flow
    from ...services.flow_entity import get_active_flow
    flow = await get_active_flow()
    if flow:
        lines.append(f"📂 Активный поток: <b>{flow.name}</b> (search_url: {'настроен' if flow.config.search_url else 'не настроен'}, резюме: {'есть' if flow.config.resume_id else 'нет'})")
    else:
        lines.append("📂 Активный поток: <b>не выбран</b>")

    from ..keyboards import settings_reply_keyboard
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=settings_reply_keyboard(
        api_ok=hh_api.is_authenticated,
        tz_offset=int(await db.get_setting("monitoring_timezone_offset", "3")),
    ))


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


@settings_router.message(F.text.startswith("🌐 Часовой пояс:"))
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
    from ...services.hh_api_client import hh_api
    hh_ok = hh_api.is_authenticated
    from ..keyboards import settings_keyboard
    
    await callback.message.edit_text(
        "⚙️ <b>Глобальные настройки</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard(gemini_model, hh_ok, tz_offset=new_val),
    )


async def _resume_update_menu_message(message: Message) -> None:
    from ... import database as db
    from ..keyboards import resume_update_mode_reply_keyboard
    from ...scheduler.daemons import resume_updater_daemon
    from ..utils.formatters import format_next_run_text
    
    enabled = (await db.get_setting("resume_auto_update", "false")) == "true"
    interval = int(await db.get_setting("resume_update_interval", "240"))
    jitter = int(await db.get_setting("resume_update_jitter", "15"))
    prime_time = await db.get_setting("resume_update_prime_time", "24/7")
    tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))

    next_run_text = await format_next_run_text("resume_update_next_run", enabled)

    text = (
        f"🚀 <b>Автоматическое поднятие резюме</b>\n\n"
        f"Статус: {'<b>АКТИВЕН</b> 🟢' if enabled else '<b>ВЫКЛЮЧЕН</b> 🔴'}\n"
        f"Интервал: <b>Раз в {interval} минут</b> (минимум 240м / 4ч)\n"
        f"Случайный сдвиг (Рандом): <b>{jitter if jitter > 0 else 'Выключен'} мин</b>\n"
        f"Время работы: <b>{prime_time}</b> (по UTC{'+' if tz_offset >= 0 else ''}{tz_offset})\n"
        f"{next_run_text}\n"
        f"Бот будет автоматически обновлять дату публикации резюме, выбранного в активном Потоке (Flow)."
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=resume_update_mode_reply_keyboard(
            enabled, interval=interval, jitter=jitter, prime_time=prime_time
        )
    )


@settings_router.message(F.text == "🚀 Настроить автоподнятие")
async def resume_update_menu_handler(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await _resume_update_menu_message(message)


@settings_router.message(F.text == "🟢 Включить автоподнятие")
async def enable_resume_update_handler(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    await db.set_setting("resume_auto_update", "true")
    await db.set_setting("resume_update_next_run", "")
    # Reset last update timestamp to force immediate run or standard timer run
    from ...services.flow_entity import get_active_flow
    flow = await get_active_flow()
    if flow and flow.config.resume_id:
        await db.set_setting(f"last_resume_update_time_{flow.config.resume_id}", "")
        
    from ...scheduler import start_resume_updater_daemon
    await start_resume_updater_daemon()
    
    await message.answer("🟢 Автоподнятие резюме <b>включено</b>.", parse_mode="HTML")
    await _resume_update_menu_message(message)


@settings_router.message(F.text == "🔴 Выключить автоподнятие")
async def disable_resume_update_handler(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    await db.set_setting("resume_auto_update", "false")
    await db.set_setting("resume_update_next_run", "")
    from ...scheduler import stop_resume_updater_daemon
    await stop_resume_updater_daemon()
    await message.answer("🔴 Автоподнятие резюме <b>выключено</b>.", parse_mode="HTML")
    await _resume_update_menu_message(message)


@settings_router.message(F.text.startswith("⏱ Интервал поднятия:"))
async def toggle_resume_update_interval(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    current = int(await db.get_setting("resume_update_interval", "240"))
    intervals = [240, 360, 480, 720, 1440]
    next_idx = (intervals.index(current) + 1) % len(intervals) if current in intervals else 0
    new_val = intervals[next_idx]
    await db.set_setting("resume_update_interval", str(new_val))
    await message.answer(f"⏱ Интервал поднятия изменен на <b>{new_val} минут</b> ({new_val // 60} ч).", parse_mode="HTML")
    await _resume_update_menu_message(message)


@settings_router.message(F.text.startswith("🎲 Рандом поднятия:"))
async def toggle_resume_update_jitter(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    current = int(await db.get_setting("resume_update_jitter", "15"))
    jitters = [0, 5, 10, 15, 30]
    next_idx = (jitters.index(current) + 1) % len(jitters) if current in jitters else 0
    new_val = jitters[next_idx]
    await db.set_setting("resume_update_jitter", str(new_val))
    label = f"{new_val} минут" if new_val > 0 else "Выключен"
    await message.answer(f"🎲 Рандомный сдвиг изменен на: <b>{label}</b>.", parse_mode="HTML")
    await _resume_update_menu_message(message)


@settings_router.message(F.text.startswith("🕒 Время поднятия:"))
async def toggle_resume_update_prime_time(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ... import database as db
    current = await db.get_setting("resume_update_prime_time", "24/7")
    options = ["24/7", "08:00 - 20:00", "09:00 - 18:00", "10:00 - 22:00"]
    next_idx = (options.index(current) + 1) % len(options) if current in options else 0
    new_val = options[next_idx]
    await db.set_setting("resume_update_prime_time", new_val)
    await message.answer(f"🕒 Время работы автоподнятия изменено на: <b>{new_val}</b>.", parse_mode="HTML")
    await _resume_update_menu_message(message)



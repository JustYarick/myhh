import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from ... import database as db
from ...scheduler import manual_scheduler as scheduler
from ..formatting import bold, italic, code, divider, status_emoji

logger = logging.getLogger(__name__)
common_router = Router()


async def _check_access(user_id: int) -> bool:
    from ...config import get_settings
    return get_settings().is_allowed_user(user_id)


async def _main_menu_message(message: Message) -> None:
    from . import _main_menu_message as _impl
    await _impl(message)


async def _main_menu_callback(callback: CallbackQuery) -> None:
    from . import _main_menu_callback as _impl
    await _impl(callback)


@common_router.message(Command("start", "help"))
async def start_command(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await _main_menu_message(message)
    except Exception as e:
        logger.error(f"/start error: {e}", exc_info=True)
        try:
            await message.answer(f"Error: {e}")
        except Exception:
            pass


@common_router.callback_query(F.data == "noop")
@common_router.callback_query(F.data == "noop_hh")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer("")


@common_router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await _main_menu_callback(callback)


@common_router.callback_query(F.data == "run")
async def run_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        await scheduler.start()
    except Exception as e:
        logger.error(f"Run error: {e}", exc_info=True)
        await callback.answer(f"Error: {e}", show_alert=True)
    await _main_menu_callback(callback)


@common_router.callback_query(F.data == "stop")
async def stop_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        await scheduler.stop()
    except Exception as e:
        logger.error(f"Stop error: {e}", exc_info=True)
    await _main_menu_callback(callback)


@common_router.callback_query(F.data == "pause")
async def pause_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        await scheduler.pause()
    except Exception as e:
        logger.error(f"Pause error: {e}", exc_info=True)
    await _main_menu_callback(callback)


@common_router.callback_query(F.data == "resume")
async def resume_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        await scheduler.resume()
    except Exception as e:
        logger.error(f"Resume error: {e}", exc_info=True)
    await _main_menu_callback(callback)


async def _get_full_stats_text() -> str:
    stats = await db.get_today_stats()
    stats_list = await db.get_stats_range(7)
    lines = [
        "📊 <b>Statistics</b>",
        f"Today ({stats['date']}):",
        f"  📨 Total Applied: <b>{stats['total_applied']}</b>",
        f"  ✅ Success: <b>{stats['successful']}</b>",
        f"  ❌ Errors: <b>{stats['errors']}</b>",
        f"  ⏭ Skipped: <b>{stats['skipped']}</b>",
        f"  🤖 AI Skipped: <b>{stats['analyzed_skip']}</b>",
        "",
        "Last 7 days:"
    ]
    if not stats_list:
        lines.append("  <i>No history data available.</i>")
    else:
        for s in stats_list:
            lines.append(
                f"  <code>{s['date']}</code>: {s['total_applied']} total (✅ {s['successful']} / ❌ {s['errors']})"
            )
    return "\n".join(lines)


@common_router.callback_query(F.data == "stats")
async def stats_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    text = await _get_full_stats_text()
    await callback.message.edit_text(text, parse_mode="HTML")


@common_router.callback_query(F.data == "stats_today")
async def stats_today_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    text = await _get_full_stats_text()
    await callback.message.edit_text(text, parse_mode="HTML")


@common_router.callback_query(F.data == "stats_week")
async def stats_week_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    text = await _get_full_stats_text()
    await callback.message.edit_text(text, parse_mode="HTML")


@common_router.callback_query(F.data == "history")
async def history_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    apps = await db.get_recent_applications(10)
    if not apps:
        text = "📭 <i>No applications yet.</i>"
    else:
        lines = ["📜 <b>Recent applications</b>"]
        for app in apps:
            icon = status_emoji(app["status"])
            lines.append(
                f"  {icon} <b>{app['title'][:40]}</b> @ <i>{app['employer'][:20]}</i>"
            )
        text = "\n".join(lines)
    await callback.message.edit_text(text, parse_mode="HTML")


@common_router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    text = scheduler.get_status_text()
    await callback.message.edit_text(text, parse_mode="HTML")


# === Reply Keyboard Message Handlers ===

async def _manual_menu_message(message: Message) -> None:
    from ..keyboards import manual_mode_reply_keyboard
    run_state = scheduler._run_state.value
    await message.answer(
        f"🔴 <b>Ручной режим управления</b>\n\nТекущий статус планировщика: {scheduler.get_status_text()}",
        parse_mode="HTML",
        reply_markup=manual_mode_reply_keyboard(run_state)
    )


async def _monitoring_menu_message(message: Message) -> None:
    from ..keyboards import monitoring_mode_reply_keyboard
    from ...scheduler import monitoring_scheduler
    monitoring_enabled = (await db.get_setting("monitoring_mode", "false")) == "true"
    interval = int(await db.get_setting("monitoring_interval", "30"))
    jitter = int(await db.get_setting("monitoring_jitter", "0"))
    prime_time = await db.get_setting("monitoring_prime_time", "24/7")
    tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))

    run_status = "РАБОТАЕТ" if monitoring_scheduler._run_state.value == "running" else "ОЖИДАНИЕ"
    
    text = (
        f"🔍 <b>Режим авто-мониторинга</b>\n\n"
        f"Фоновый запуск: {'<b>АКТИВЕН</b> 🟢' if monitoring_enabled else '<b>ВЫКЛЮЧЕН</b> 🔴'}\n"
        f"Интервал проверки: <b>Каждые {interval} минут</b>\n"
        f"Случайный сдвиг (Рандом): <b>{jitter if jitter > 0 else 'Выключен'} мин</b>\n"
        f"Время работы: <b>{prime_time}</b> (по UTC{'+' if tz_offset >= 0 else ''}{tz_offset})\n"
        f"Текущая сессия поиска: <b>{run_status}</b>\n\n"
        f"Бот автоматически отслеживает и откликается только на новые вакансии, опубликованные с момента запуска мониторинга."
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=monitoring_mode_reply_keyboard(
            monitoring_enabled, interval=interval, jitter=jitter, prime_time=prime_time
        )
    )


@common_router.message(F.text == "🔴 Ручной режим")
async def manual_mode_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await _manual_menu_message(message)


@common_router.message(F.text == "🔍 Мониторинг")
async def monitoring_mode_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await _monitoring_menu_message(message)


@common_router.message(F.text == "🟢 Включить мониторинг")
async def enable_monitoring_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await db.set_setting("monitoring_mode", "true")
    from ...services.flow_entity import get_active_flow_id
    flow_id = await get_active_flow_id()
    if flow_id:
        await db.set_setting(f"last_newest_vacancy_{flow_id}", "")
    
    await message.answer("✅ Мониторинг вакансий <b>включен</b>. Устанавливаю базовую точку отсчета свежих вакансий...", parse_mode="HTML")
    await _monitoring_menu_message(message)


@common_router.message(F.text.startswith("⏱ Интервал:"))
async def toggle_monitoring_interval_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    current = int(await db.get_setting("monitoring_interval", "30"))
    intervals = [15, 30, 60, 120]
    next_idx = (intervals.index(current) + 1) % len(intervals) if current in intervals else 1
    new_val = intervals[next_idx]
    await db.set_setting("monitoring_interval", str(new_val))
    await message.answer(f"⏱ Интервал мониторинга изменен на <b>{new_val} минут</b>.", parse_mode="HTML")
    await _monitoring_menu_message(message)


@common_router.message(F.text.startswith("🎲 Рандом:"))
async def toggle_monitoring_jitter_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    current = int(await db.get_setting("monitoring_jitter", "0"))
    jitters = [0, 5, 10, 15]
    next_idx = (jitters.index(current) + 1) % len(jitters) if current in jitters else 1
    new_val = jitters[next_idx]
    await db.set_setting("monitoring_jitter", str(new_val))
    label = f"{new_val} минут" if new_val > 0 else "Выключен"
    await message.answer(f"🎲 Рандомный сдвиг запуска изменен на: <b>{label}</b>.", parse_mode="HTML")
    await _monitoring_menu_message(message)


@common_router.message(F.text.startswith("🕒 Время:"))
async def toggle_monitoring_prime_time_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    current = await db.get_setting("monitoring_prime_time", "24/7")
    options = ["24/7", "08:00 - 20:00", "09:00 - 18:00", "10:00 - 22:00"]
    next_idx = (options.index(current) + 1) % len(options) if current in options else 1
    new_val = options[next_idx]
    await db.set_setting("monitoring_prime_time", new_val)
    await message.answer(f"🕒 Время работы мониторинга изменено на: <b>{new_val}</b>.", parse_mode="HTML")
    await _monitoring_menu_message(message)



@common_router.message(F.text == "🔴 Выключить мониторинг")
async def disable_monitoring_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await db.set_setting("monitoring_mode", "false")
    
    from ...scheduler import monitoring_scheduler, RunState
    if monitoring_scheduler._run_state != RunState.IDLE:
        await monitoring_scheduler.stop()
        
    await message.answer("❌ Мониторинг вакансий <b>выключен</b> (активная сессия остановлена).", parse_mode="HTML")
    await _monitoring_menu_message(message)


@common_router.message(F.text.in_({"▶️ Run Manual", "▶️ Запуск"}))
async def run_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.start()
    except Exception as e:
        logger.error(f"Run error: {e}", exc_info=True)
        await message.answer(f"Error: {e}")
    await _manual_menu_message(message)


@common_router.message(F.text.in_({"⏸ Pause", "⏸ Пауза"}))
async def pause_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.pause()
    except Exception as e:
        logger.error(f"Pause error: {e}", exc_info=True)
    await _manual_menu_message(message)


@common_router.message(F.text.in_({"▶️ Resume", "▶️ Продолжить"}))
async def resume_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.resume()
    except Exception as e:
        logger.error(f"Resume error: {e}", exc_info=True)
    await _manual_menu_message(message)


@common_router.message(F.text.in_({"⏹ Stop", "⏹ Стоп"}))
async def stop_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.stop()
    except Exception as e:
        logger.error(f"Stop error: {e}", exc_info=True)
    await _manual_menu_message(message)


@common_router.message(F.text.in_({"📂 Flows", "📂 Потоки"}))
async def flows_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services import flow_entity as flow_db
    from ..keyboards import flows_reply_keyboard
    flows = await flow_db.list_flows()
    active_id = await flow_db.get_active_flow_id()
    if not flows:
        await message.answer(
            "Пока нет потоков. Создайте поток для начала.",
            reply_markup=flows_reply_keyboard([], active_id),
        )
    else:
        await message.answer(
            "Ваши потоки:",
            reply_markup=flows_reply_keyboard(flows, active_id),
        )


@common_router.message(F.text.in_({"📊 Stats", "📊 Статистика"}))
async def stats_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    text = await _get_full_stats_text()
    await message.answer(text, parse_mode="HTML")


@common_router.message(F.text.in_({"📜 History", "📜 История"}))
async def history_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    apps = await db.get_recent_applications(10)
    if not apps:
        text = "📭 <i>История откликов пуста.</i>"
    else:
        lines = ["📜 <b>Последние отклики:</b>"]
        for app in apps:
            icon = status_emoji(app["status"])
            lines.append(
                f"  {icon} <b>{app['title'][:40]}</b> @ <i>{app['employer'][:20]}</i>"
            )
        text = "\n".join(lines)
    await message.answer(text, parse_mode="HTML")


@common_router.message(F.text == "📝 Вопросы")
async def vacancies_with_questions_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    vacancies = await db.get_vacancies_with_questions(15)
    if not vacancies:
        await message.answer("Нет сохраненных вакансий с вопросами.")
        return
    
    lines = ["📝 <b>Вакансии с вопросами (последние 15):</b>\n"]
    for idx, v in enumerate(vacancies):
        dt = v["created_at"]
        lines.append(f"{idx+1}. <a href=\"{v['vacancy_url']}\">{v['title']}</a> @ <i>{v['employer']}</i> ({dt})")
    
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@common_router.message(F.text.in_({"⚙️ Settings", "⚙️ Настройки"}))
async def settings_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from .settings import send_global_settings
    await send_global_settings(message)


@common_router.message(F.text.in_({"⬅️ Back to Main Menu", "⬅️ Главное меню"}))
@common_router.message(F.text.in_({"📂 Back to Flows", "📂 Назад к Потокам"}))
async def back_to_main_menu_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await _main_menu_message(message)


@common_router.message(F.text.in_({"ℹ️ Status & Info", "ℹ️ Статус и Инфо"}))
async def global_status_info_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    
    from ...services.hh_auth import hh_auth
    from ...scheduler import manual_scheduler, monitoring_scheduler, RunState
    from ...services.flow_entity import get_active_flow
    
    # Global settings
    gemini_model = await db.get_setting("gemini_model", "gemini-2.0-flash")
    tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))
    hh_ok = hh_auth.session_exists()
    hh_status = "🟢 Подключен" if hh_ok else "🔴 Не подключен"
    
    # Local time calculation
    from datetime import datetime, timedelta, timezone
    user_time = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
    time_str = user_time.strftime("%H:%M:%S")
    
    # Schedulers state
    manual_state = "Ожидание ⏹"
    if manual_scheduler._run_state == RunState.RUNNING:
        manual_state = f"Запущен 🟢 (Страница {manual_scheduler.state.current_page}, Обработано {manual_scheduler.state.vacancies_processed})"
    elif manual_scheduler._run_state == RunState.PAUSED:
        manual_state = f"На паузе ⏸ (Страница {manual_scheduler.state.current_page})"
    
    monitoring_enabled = (await db.get_setting("monitoring_mode", "false")) == "true"
    monitoring_state = "Выключен 🔴"
    if monitoring_enabled:
        monitoring_run = "Сканирование 🟢" if monitoring_scheduler._run_state == RunState.RUNNING else "Ожидание ⏱"
        monitoring_state = f"Активен ({monitoring_run})"
        
    interval = int(await db.get_setting("monitoring_interval", "30"))
    jitter = int(await db.get_setting("monitoring_jitter", "0"))
    prime_time = await db.get_setting("monitoring_prime_time", "24/7")
    
    active_flow = await get_active_flow()
    flow_name = active_flow.name if active_flow else "Отсутствует"
    
    # Saved boundary
    boundary_id = "Отсутствует"
    if active_flow:
        boundary_id = await db.get_setting(f"last_newest_vacancy_{active_flow.id}", "Отсутствует")
    
    # Today stats
    stats = await db.get_today_stats()
    
    text = (
        f"🤖 <b>AutoHH: Статус и Системный дашборд</b>\n\n"
        f"⚙️ <b>Системная информация:</b>\n"
        f"• Аккаунт HH.ru: <b>{hh_status}</b>\n"
        f"• Модель ИИ: <code>{gemini_model}</code>\n"
        f"• Активный поток: <b>{flow_name}</b>\n"
        f"• Местное время: <code>{time_str}</code> (UTC{'+' if tz_offset >= 0 else ''}{tz_offset})\n\n"
        f"▶️ <b>Ручной режим:</b>\n"
        f"• Статус: <b>{manual_state}</b>\n\n"
        f"🔍 <b>Режим мониторинга:</b>\n"
        f"• Статус: <b>{monitoring_state}</b>\n"
        f"• Интервал: <b>каждые {interval} мин</b>\n"
        f"• Рандом (Jitter): <b>{jitter if jitter > 0 else 'Выкл'} мин</b>\n"
        f"• Время работы: <b>{prime_time}</b>\n"
        f"• Точка отсчета (ID): <code>{boundary_id}</code>\n\n"
        f"📊 <b>Статистика за сегодня:</b>\n"
        f"• Обработано всего: <b>{stats['total_applied']}</b>\n"
        f"• Успешных откликов: <b>{stats['successful']}</b>\n"
        f"• Пропущено (ИИ/фильтры): <b>{stats['analyzed_skip'] + stats['skipped']}</b>\n"
        f"• Ошибок: <b>{stats['errors']}</b>"
    )
    
    await message.answer(text, parse_mode="HTML")



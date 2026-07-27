import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from ... import database as db
from ...scheduler import scheduler
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


@common_router.callback_query(F.data == "stats")
async def stats_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    from ..keyboards import stats_keyboard
    stats = await db.get_today_stats()
    text = (
        f"📊 <b>Today</b>\n"
        f"  📨 Applied: <b>{stats['total_applied']}</b>\n"
        f"  ✅ Success: <b>{stats['successful']}</b>\n"
        f"  ❌ Errors: <b>{stats['errors']}</b>\n"
        f"  ⏭ Skipped: <b>{stats['skipped']}</b>\n"
        f"  🤖 AI Skipped: <b>{stats['analyzed_skip']}</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=stats_keyboard())


@common_router.callback_query(F.data == "stats_today")
async def stats_today_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    stats = await db.get_today_stats()
    text = (
        f"📊 <b>Today ({stats['date']})</b>\n"
        f"  📨 Total: <b>{stats['total_applied']}</b>\n"
        f"  ✅ Success: <b>{stats['successful']}</b>\n"
        f"  ❌ Errors: <b>{stats['errors']}</b>\n"
        f"  ⏭ Skipped: <b>{stats['skipped']}</b>\n"
        f"  🤖 AI Skipped: <b>{stats['analyzed_skip']}</b>"
    )
    from ..keyboards import back_keyboard
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_keyboard("stats"))


@common_router.callback_query(F.data == "stats_week")
async def stats_week_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    stats_list = await db.get_stats_range(7)
    if not stats_list:
        text = "📭 <i>No data for the last 7 days.</i>"
    else:
        lines = ["📊 <b>Last 7 days</b>"]
        for s in stats_list:
            lines.append(
                f"  <code>{s['date']}</code>: {s['total_applied']} total, "
                f"✅ {s['successful']} ok, ❌ {s['errors']} err"
            )
        text = "\n".join(lines)
    from ..keyboards import back_keyboard
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_keyboard("stats"))


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
    from ..keyboards import back_keyboard
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_keyboard())


@common_router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    text = scheduler.get_status_text()
    from ..keyboards import back_keyboard
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_keyboard())

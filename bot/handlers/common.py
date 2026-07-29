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

@common_router.message(F.text == "▶️ Run")
async def run_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.start()
    except Exception as e:
        logger.error(f"Run error: {e}", exc_info=True)
        await message.answer(f"Error: {e}")
    await _main_menu_message(message)


@common_router.message(F.text == "⏸ Pause")
async def pause_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.pause()
    except Exception as e:
        logger.error(f"Pause error: {e}", exc_info=True)
    await _main_menu_message(message)


@common_router.message(F.text == "▶️ Resume")
async def resume_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.resume()
    except Exception as e:
        logger.error(f"Resume error: {e}", exc_info=True)
    await _main_menu_message(message)


@common_router.message(F.text == "⏹ Stop")
async def stop_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await scheduler.stop()
    except Exception as e:
        logger.error(f"Stop error: {e}", exc_info=True)
    await _main_menu_message(message)


@common_router.message(F.text == "📂 Flows")
async def flows_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services import flow_entity as flow_db
    from ..keyboards import flows_reply_keyboard
    flows = await flow_db.list_flows()
    active_id = await flow_db.get_active_flow_id()
    if not flows:
        await message.answer(
            "No flows yet. Create one to start.",
            reply_markup=flows_reply_keyboard([], active_id),
        )
    else:
        await message.answer(
            "Your flows:",
            reply_markup=flows_reply_keyboard(flows, active_id),
        )


@common_router.message(F.text == "📊 Stats")
async def stats_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    text = await _get_full_stats_text()
    await message.answer(text, parse_mode="HTML")


@common_router.message(F.text == "📜 History")
async def history_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
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
    await message.answer(text, parse_mode="HTML")


@common_router.message(F.text == "⚙️ Settings")
async def settings_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.hh_auth import hh_auth
    from ...services.flow_entity import get_setting
    from ..keyboards import settings_reply_keyboard
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    hh_ok = hh_auth.session_exists()
    hh_status = "Linked" if hh_ok else "Not linked"
    text = (
        f"⚙️ <b>Global Settings</b>\n\n"
        f"🤖 Gemini model: <code>{gemini_model}</code>\n"
        f"🔑 HH Account: <b>{hh_status}</b>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_reply_keyboard(hh_ok),
    )


@common_router.message(F.text == "⬅️ Back to Main Menu")
async def back_to_main_menu_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await _main_menu_message(message)



import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ...services.hh_auth import hh_auth

logger = logging.getLogger(__name__)
auth_router = Router()


class LoginState(StatesGroup):
    waiting_for_credential = State()
    waiting_for_otp = State()


async def _check_access(user_id: int) -> bool:
    from ...config import get_settings
    return get_settings().is_allowed_user(user_id)


async def _main_menu_message(message: Message) -> None:
    from . import _main_menu_message as _impl
    await _impl(message)


async def _main_menu_callback(callback: CallbackQuery) -> None:
    from . import _main_menu_callback as _impl
    await _impl(callback)


async def _show_settings_message(message: Message) -> None:
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


@auth_router.callback_query(F.data == "login_bot")
async def login_bot_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("Connecting to HH.ru...")
    ok, msg = await hh_auth.start_login(callback.from_user.id)
    from ..keyboards import cancel_keyboard
    if ok:
        await callback.message.edit_text(msg, reply_markup=cancel_keyboard())
        await state.set_state(LoginState.waiting_for_credential)
    else:
        await callback.message.edit_text(f"Login failed: {msg}")
        await _main_menu_callback(callback)


@auth_router.message(StateFilter(LoginState.waiting_for_credential))
async def login_credential_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    if message.text == "❌ Cancel Login":
        await cancel_login_message(message, state)
        return
    ok, msg = await hh_auth.submit_credential(message.from_user.id, message.text)
    from ..keyboards import cancel_login_reply_keyboard
    if ok:
        await state.set_state(LoginState.waiting_for_otp)
        await message.answer(msg, reply_markup=cancel_login_reply_keyboard())
    else:
        await state.clear()
        await message.answer(f"Login failed: {msg}")
        await _show_settings_message(message)


@auth_router.message(StateFilter(LoginState.waiting_for_otp))
async def login_otp_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    if message.text == "❌ Cancel Login":
        await cancel_login_message(message, state)
        return
    ok, msg = await hh_auth.submit_otp(message.from_user.id, message.text)
    await state.clear()
    if ok:
        await message.answer(msg)
        await _show_settings_message(message)
    else:
        await message.answer(f"Login failed: {msg}")
        await _show_settings_message(message)


@auth_router.callback_query(F.data == "cancel_action")
async def cancel_action_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_access(callback.from_user.id):
        return
    try:
        await hh_auth.cancel_login(callback.from_user.id)
    except Exception:
        logger.debug("Cancel login cleanup error (ignored)")
    await state.clear()
    try:
        await callback.answer()
    except Exception:
        logger.debug("Cancel callback.answer failed (proxy issue)")
    await _main_menu_callback(callback)


@auth_router.callback_query(F.data == "hh_logout")
async def hh_logout_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    from ..keyboards import confirm_logout_keyboard
    await callback.message.edit_text(
        "Unlink HH.ru session?",
        reply_markup=confirm_logout_keyboard(),
    )


@auth_router.callback_query(F.data == "confirm_hh_logout")
async def confirm_hh_logout_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    ok = await hh_auth.logout()
    if ok:
        await callback.message.edit_text("HH.ru session unlinked.")
    else:
        await callback.message.edit_text("No session to unlink.")
    await _main_menu_callback(callback)


@auth_router.message(F.text == "🔑 Login HH")
async def login_hh_message(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    await message.answer("Connecting to HH.ru...")
    ok, msg = await hh_auth.start_login(message.from_user.id)
    from ..keyboards import cancel_login_reply_keyboard
    if ok:
        await message.answer(msg, reply_markup=cancel_login_reply_keyboard())
        await state.set_state(LoginState.waiting_for_credential)
    else:
        await message.answer(f"Login failed: {msg}")
        await _show_settings_message(message)


@auth_router.message(F.text == "❌ Cancel Login")
async def cancel_login_message(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    try:
        await hh_auth.cancel_login(message.from_user.id)
    except Exception:
        logger.debug("Cancel login cleanup error (ignored)")
    await state.clear()
    await message.answer("Login cancelled.")
    await _show_settings_message(message)


@auth_router.message(F.text == "🔓 Logout HH")
async def logout_hh_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ..keyboards import confirm_logout_reply_keyboard
    await message.answer(
        "⚠️ Unlink HH.ru session?",
        reply_markup=confirm_logout_reply_keyboard(),
    )


@auth_router.message(F.text == "⚠️ Confirm Logout")
async def confirm_logout_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    ok = await hh_auth.logout()
    if ok:
        await message.answer("HH.ru session unlinked.")
    else:
        await message.answer("No session to unlink.")
    await _show_settings_message(message)


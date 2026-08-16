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


class HHApiLoginState(StatesGroup):
    """OAuth code input for mobile API authentication."""
    waiting_for_code = State()


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
    from ...services.hh_api_client import hh_api
    from ..keyboards import settings_reply_keyboard
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")
    hh_ok = hh_auth.session_exists()
    hh_status = "Linked" if hh_ok else "Not linked"

    # Check API token status
    await hh_api.load_token()
    api_ok = hh_api.is_authenticated
    api_status = "✅ Активен" if api_ok else "❌ Не настроен"

    text = (
        f"⚙️ <b>Global Settings</b>\n\n"
        f"🤖 Gemini model: <code>{gemini_model}</code>\n"
        f"🔑 HH Account (браузер): <b>{hh_status}</b>\n"
        f"📱 HH API токен: <b>{api_status}</b>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_reply_keyboard(hh_ok, api_ok=api_ok),
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


# ---------------------------------------------------------------------------
# HH API OAuth login (mobile API token)
# ---------------------------------------------------------------------------

_ANDROID_CLIENT_ID = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
_HH_OAUTH_AUTHORIZE = (
    "https://hh.ru/oauth/authorize"
    f"?response_type=code"
    f"&client_id={_ANDROID_CLIENT_ID}"
)


@auth_router.message(F.text == "📱 Авторизация HH (API)")
async def hh_api_login_message(message: Message, state: FSMContext) -> None:
    """Start the OAuth code-exchange flow for the mobile API token."""
    if not await _check_access(message.from_user.id):
        return
    text = (
        "📱 <b>Авторизация через HH.ru API</b>\n\n"
        "1. Откройте эту ссылку в браузере:\n"
        f"<code>{_HH_OAUTH_AUTHORIZE}</code>\n\n"
        "2. Войдите в аккаунт HH.ru\n\n"
        "3. После авторизации браузер перенаправит на URL вида:\n"
        "<code>hhandroid://...?code=XXXXXXXX...</code>\n\n"
        "4. Скопируйте значение параметра <code>code=</code> из адресной строки "
        "и отправьте его сюда.\n\n"
        "❓ <i>Если URL не открывается автоматически — скопируйте его вручную "
        "из адресной строки браузера.</i>"
    )
    await message.answer(text, parse_mode="HTML")
    await state.set_state(HHApiLoginState.waiting_for_code)


@auth_router.message(StateFilter(HHApiLoginState.waiting_for_code))
async def hh_api_code_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return

    code = (message.text or "").strip()
    if not code:
        await message.answer("Пустой код. Попробуйте ещё раз или отправьте /cancel")
        return

    # Strip full URL if user pasted it instead of just the code
    import re
    m = re.search(r"[?&]code=([^&\s]+)", code)
    if m:
        code = m.group(1)

    await message.answer("⏳ Получаю токен...")

    from ...services.hh_api_client import hh_api
    ok = await hh_api.authenticate_with_code(code)

    await state.clear()
    if ok:
        await message.answer(
            "✅ <b>HH API авторизация успешна!</b>\n"
            "Теперь отклики отправляются через официальный мобильный API HH.ru.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "❌ Не удалось получить токен. Проверьте код и попробуйте ещё раз.\n"
            "Код действителен только несколько минут — если он истёк, пройдите авторизацию заново."
        )
    await _show_settings_message(message)


@auth_router.message(F.text == "🔓 Logout HH API")
async def hh_api_logout_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.hh_api_client import hh_api
    await hh_api.clear_token()
    await message.answer("✅ HH API токен удалён.")
    await _show_settings_message(message)


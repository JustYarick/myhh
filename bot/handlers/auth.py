import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logger = logging.getLogger(__name__)
auth_router = Router()


class HHApiLoginState(StatesGroup):
    """OAuth code input for mobile API authentication."""
    waiting_for_code = State()


async def _check_access(user_id: int) -> bool:
    from ...config import get_settings
    return get_settings().is_allowed_user(user_id)


async def _main_menu_callback(callback: CallbackQuery) -> None:
    from . import _main_menu_callback as _impl
    await _impl(callback)


async def _show_settings_message(message: Message) -> None:
    from ...services.flow_entity import get_setting
    from ...services.hh_api_client import hh_api
    from ..keyboards import settings_reply_keyboard
    gemini_model = await get_setting("gemini_model", "gemini-2.0-flash")

    # Check API token status
    await hh_api.load_token()
    api_ok = hh_api.is_authenticated
    api_status = "✅ Активен" if api_ok else "❌ Не настроен"

    text = (
        f"⚙️ <b>Global Settings</b>\n\n"
        f"🤖 Gemini model: <code>{gemini_model}</code>\n"
        f"📱 HH API токен: <b>{api_status}</b>"
    )
    # The keyboard now only needs to know about api_ok
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=settings_reply_keyboard(hh_ok=False, api_ok=api_ok),
    )


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
        "❓ <i>Если вас перекидывает в приложение (или предлагает открыть) — "
        "скопируйте адрес этой кнопки правой кнопкой мыши. Бот сам достанет код.</i>"
    )
    from ..keyboards import cancel_login_reply_keyboard
    await message.answer(text, parse_mode="HTML", reply_markup=cancel_login_reply_keyboard())
    await state.set_state(HHApiLoginState.waiting_for_code)


@auth_router.message(StateFilter(HHApiLoginState.waiting_for_code))
async def hh_api_code_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return

    code = (message.text or "").strip()
    
    if code == "❌ Cancel Login":
        await state.clear()
        await message.answer("Авторизация отменена.")
        await _show_settings_message(message)
        return

    if not code:
        await message.answer("Пустой код. Попробуйте ещё раз или нажмите 'Cancel Login'.")
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
    from ..keyboards import confirm_logout_reply_keyboard
    await message.answer(
        "⚠️ Вы уверены, что хотите удалить API токен HH.ru?",
        reply_markup=confirm_logout_reply_keyboard(),
    )


@auth_router.message(F.text == "⚠️ Confirm Logout")
async def confirm_logout_message(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    from ...services.hh_api_client import hh_api
    await hh_api.clear_token()
    await message.answer("✅ HH API токен удалён.")
    await _show_settings_message(message)

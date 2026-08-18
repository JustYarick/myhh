from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse, parse_qs

from ..services.flow_entity import FlowEntity


def main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Ручной режим"), KeyboardButton(text="🔍 Мониторинг")],
        [KeyboardButton(text="📂 Потоки"), KeyboardButton(text="📝 Вопросы")],
        [KeyboardButton(text="📜 История"), KeyboardButton(text="ℹ️ Статус и Инфо")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="📚 Помощь")]
    ], resize_keyboard=True)


def manual_mode_reply_keyboard(run_state: str = "idle") -> ReplyKeyboardMarkup:
    controls = []
    if run_state == "running":
        controls = [KeyboardButton(text="⏸ Пауза"), KeyboardButton(text="⏹ Стоп")]
    elif run_state == "paused":
        controls = [KeyboardButton(text="▶️ Продолжить"), KeyboardButton(text="⏹ Стоп")]
    else:
        controls = [KeyboardButton(text="▶️ Запуск")]

    return ReplyKeyboardMarkup(keyboard=[
        controls,
        [KeyboardButton(text="📂 Потоки"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⬅️ Главное меню")]
    ], resize_keyboard=True)


def monitoring_mode_reply_keyboard(enabled: bool, interval: int = 30, jitter: int = 0, prime_time: str = "24/7") -> ReplyKeyboardMarkup:
    toggle_btn = KeyboardButton(text="🔴 Выключить мониторинг") if enabled else KeyboardButton(text="🟢 Включить мониторинг")
    pt_text = f"🕒 Время: {prime_time}"
    jitter_text = f"🎲 Рандом: {jitter}м" if jitter > 0 else "🎲 Рандом: Выкл"
    return ReplyKeyboardMarkup(keyboard=[
        [toggle_btn],
        [KeyboardButton(text=f"⏱ Интервал: {interval}м"), KeyboardButton(text=jitter_text)],
        [KeyboardButton(text=pt_text), KeyboardButton(text="📂 Потоки")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📜 История")],
        [KeyboardButton(text="⬅️ Главное меню")]
    ], resize_keyboard=True)


def resume_pick_keyboard(resumes: list, flow_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in resumes:
        status_mark = "⭐ " if r.status == "active" else ""
        rows.append([InlineKeyboardButton(
            text=f"{status_mark}{r.title} (ID: {r.id})",
            callback_data=f"flow_set_resume_{flow_id}_{r.id}",
        )])
    rows.append([InlineKeyboardButton(text="🔄 Без резюме", callback_data=f"flow_set_resume_{flow_id}_none")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к потоку", callback_data=f"flow_back_to_detail_{flow_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard(callback_data: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]
    ])


def settings_keyboard(gemini_model: str, hh_linked: bool, tz_offset: int = 3) -> InlineKeyboardMarkup:
    hh_status = "Подключен ✅" if hh_linked else "Не подключен ❌"
    tz_label = f"🌐 Часовой пояс: UTC{'+' if tz_offset >= 0 else ''}{tz_offset}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🤖 Модель Gemini: {gemini_model}", callback_data="settings_model")],
        [InlineKeyboardButton(text=f"📱 Аккаунт HH.ru: {hh_status}", callback_data="settings_hh")],
        [InlineKeyboardButton(text=tz_label, callback_data="settings_tz")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")],
    ])


def model_list_keyboard(models: list[dict], current_model: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for m in models:
        marker = "⭐ " if m["name"] == current_model else ""
        rows.append([InlineKeyboardButton(
            text=f"{marker}{m['display']} ({m['name']})",
            callback_data=f"settings_set_model_{m['name']}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_reply_keyboard(api_ok: bool = False, tz_offset: int = 3) -> ReplyKeyboardMarkup:
    api_button = KeyboardButton(text="🔓 Удалить токен HH API") if api_ok else KeyboardButton(text="📱 Авторизация HH (API)")
    tz_button = KeyboardButton(text=f"🌐 Часовой пояс: UTC{'+' if tz_offset >= 0 else ''}{tz_offset}")
    keyboard = [
        [api_button],
        [tz_button, KeyboardButton(text="🚀 Настроить автоподнятие")],
        [KeyboardButton(text="🤖 Выбрать модель Gemini"), KeyboardButton(text="🔔 Уведомления")],
        [KeyboardButton(text="🧹 Очистить кэш"), KeyboardButton(text="🔄 Сбросить лимиты")],
        [KeyboardButton(text="🩺 Проверить подключения")],
        [KeyboardButton(text="⬅️ Главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def resume_update_mode_reply_keyboard(enabled: bool, interval: int = 240, jitter: int = 15, prime_time: str = "24/7") -> ReplyKeyboardMarkup:
    toggle_btn = KeyboardButton(text="🔴 Выключить автоподнятие") if enabled else KeyboardButton(text="🟢 Включить автоподнятие")
    pt_text = f"🕒 Время поднятия: {prime_time}"
    jitter_text = f"🎲 Рандом поднятия: {jitter}м" if jitter > 0 else "🎲 Рандом поднятия: Выкл"
    return ReplyKeyboardMarkup(keyboard=[
        [toggle_btn],
        [KeyboardButton(text=f"⏱ Интервал поднятия: {interval}м"), KeyboardButton(text=jitter_text)],
        [KeyboardButton(text=pt_text)],
        [KeyboardButton(text="⬅️ Назад в Настройки")]
    ], resize_keyboard=True)


def notifications_keyboard(success: bool, error: bool, skip: bool) -> InlineKeyboardMarkup:
    btn_success = InlineKeyboardButton(
        text=f"🟢 Успешные отклики: {'ВКЛ' if success else 'ВЫКЛ'}",
        callback_data="toggle_notify_success"
    )
    btn_error = InlineKeyboardButton(
        text=f"🔴 Ошибки и капчи: {'ВКЛ' if error else 'ВЫКЛ'}",
        callback_data="toggle_notify_error"
    )
    btn_skip = InlineKeyboardButton(
        text=f"🟡 Пропуски вакансий: {'ВКЛ' if skip else 'ВЫКЛ'}",
        callback_data="toggle_notify_skip"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn_success],
        [btn_error],
        [btn_skip],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings")]
    ])


def models_reply_keyboard(models: list[dict], current_model: str) -> ReplyKeyboardMarkup:
    keyboard = []
    row = []
    for m in models:
        marker = "⭐ " if m["name"] == current_model else ""
        row.append(KeyboardButton(text=f"Модель: {marker}{m['name']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([KeyboardButton(text="⬅️ Назад в Настройки")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def cancel_login_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отменить Вход")]
    ], resize_keyboard=True)


def confirm_logout_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⚠️ Подтвердить Выход"), KeyboardButton(text="⬅️ Назад в Настройки")]
    ], resize_keyboard=True)


def flows_reply_keyboard(flows: list, active_flow_id: int | None) -> ReplyKeyboardMarkup:
    keyboard = []
    for f in flows:
        marker = "🟢 " if f.id == active_flow_id else "⚪ "
        keyboard.append([KeyboardButton(text=f"📁 Поток: {marker}{f.name} (ID: {f.id})")])
    keyboard.append([
        KeyboardButton(text="➕ Создать Поток"),
        KeyboardButton(text="⬅️ Главное меню")
    ])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def flow_detail_reply_keyboard(is_active: bool) -> ReplyKeyboardMarkup:
    keyboard = []
    if not is_active:
        keyboard.append([KeyboardButton(text="🟢 Активировать Поток")])
    keyboard.append([
        KeyboardButton(text="⚙️ Редактировать Поток"),
        KeyboardButton(text="🧪 Тестовый запуск"),
    ])
    keyboard.append([
        KeyboardButton(text="🔄 Счётчик вакансий"),
        KeyboardButton(text="🔄 Обновить резюме"),
    ])
    keyboard.append([
        KeyboardButton(text="❌ Удалить Поток"),
        KeyboardButton(text="📂 Назад к Потокам"),
    ])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def flow_edit_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [
            KeyboardButton(text="🔍 Настроить ссылку поиска"),
            KeyboardButton(text="👤 Выбрать резюме"),
        ],
        [
            KeyboardButton(text="✏️ Название потока"),
            KeyboardButton(text="📄 Макс. страниц"),
        ],
        [
            KeyboardButton(text="🎯 Цель откликов за запуск"),
            KeyboardButton(text="⏱ Суточный лимит"),
            KeyboardButton(text="⏱ Часовой лимит"),
        ],
        [
            KeyboardButton(text="⏳ Мин. задержка"),
            KeyboardButton(text="⏳ Макс. задержка"),
            KeyboardButton(text="⏳ Пауза между страницами"),
        ],
        [
            KeyboardButton(text="⏰ Расписание"),
            KeyboardButton(text="📋 Доп. правила"),
        ],
        [
            KeyboardButton(text="📝 Редактировать промпты"),
            KeyboardButton(text="🚫 Черный список"),
        ],
        [KeyboardButton(text="📂 Назад к Потокам")]
    ], resize_keyboard=True)


def flow_prompts_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Промпт сопроводительного")],
        [KeyboardButton(text="📝 Промпт анализа вакансии")],
        [KeyboardButton(text="⚙️ Назад к Потоку")]
    ], resize_keyboard=True)


def timezone_select_keyboard(current: int) -> InlineKeyboardMarkup:
    offsets = [
        -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 0,
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
    ]
    rows = []
    current_row = []
    for off in offsets:
        sign = "+" if off >= 0 else ""
        label = f"UTC{sign}{off}"
        if off == current:
            label = f"✅ {label}"
        current_row.append(InlineKeyboardButton(text=label, callback_data=f"set_tz_{off}"))
        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
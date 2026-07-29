from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse, parse_qs

from ..services.flow_entity import FlowEntity, FlowConfig


def main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Ручной режим"), KeyboardButton(text="🔍 Мониторинг")],
        [KeyboardButton(text="📝 Вопросы"), KeyboardButton(text="📜 История")],
        [KeyboardButton(text="ℹ️ Статус и Инфо"), KeyboardButton(text="⚙️ Настройки")]
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


def main_menu_keyboard(hh_session_ok: bool, flows: list[FlowEntity], active_flow_id: int | None, run_state: str = "idle") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if hh_session_ok:
        rows.append([InlineKeyboardButton(text="HH Linked - Logout", callback_data="hh_logout")])
    else:
        rows.append([InlineKeyboardButton(text="HH Not Linked - Login", callback_data="login_bot")])

    rows.append([
        InlineKeyboardButton(text="Flows", callback_data="flows_list"),
        InlineKeyboardButton(text="Stats", callback_data="stats"),
        InlineKeyboardButton(text="History", callback_data="history"),
    ])

    if run_state == "running":
        rows.append([
            InlineKeyboardButton(text="⏸ Pause", callback_data="pause"),
            InlineKeyboardButton(text="⏹ Stop", callback_data="stop"),
        ])
    elif run_state == "paused":
        rows.append([
            InlineKeyboardButton(text="▶️ Resume", callback_data="resume"),
            InlineKeyboardButton(text="⏹ Stop", callback_data="stop"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="▶️ Run", callback_data="run"),
        ])

    rows.append([
        InlineKeyboardButton(text="Settings", callback_data="settings"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def hh_login_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Login via Bot", callback_data="login_bot")],
    ])


def flows_list_keyboard(flows: list[FlowEntity], active_flow_id: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for f in flows:
        marker = ">> " if f.id == active_flow_id else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{marker}{f.name}",
                callback_data=f"flow_{f.id}",
            )
        ])

    rows.append([InlineKeyboardButton(text="+ New Flow", callback_data="flow_new")])
    rows.append([InlineKeyboardButton(text="Back", callback_data="main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def flow_detail_keyboard(flow_id: int, is_active: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if not is_active:
        rows.append([InlineKeyboardButton(
            text="Activate",
            callback_data=f"flow_activate_{flow_id}",
        )])

    rows.append([InlineKeyboardButton(text="Test Run", callback_data=f"flow_test_{flow_id}")])
    rows.append([InlineKeyboardButton(text="Edit", callback_data=f"flow_edit_{flow_id}")])
    rows.append([InlineKeyboardButton(text="Delete", callback_data=f"flow_delete_{flow_id}")])
    rows.append([InlineKeyboardButton(text="Back", callback_data="flows_list")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def flow_edit_keyboard(flow_id: int, config) -> InlineKeyboardMarkup:
    resume = "set" if config.resume_id else "not set"
    rows: list[list[InlineKeyboardButton]] = []

    if config.search_url:
        parsed = urlparse(config.search_url)
        params = parse_qs(parsed.query)
        search_text = params.get("text", [""])[0] or "—"
        experience = params.get("experience", [])
        work_format = params.get("work_format", [])
        exp_str = ", ".join(experience) if experience else "—"
        fmt_str = ", ".join(work_format) if work_format else "—"
        url_short = config.search_url[:55] + ("..." if len(config.search_url) > 55 else "")
        rows.append([InlineKeyboardButton(text=f"URL: {url_short}", callback_data=f"fe_{flow_id}_search_url")])
        count_str = f"{config.vacancy_count}" if config.vacancy_count else "..."
        rows.append([InlineKeyboardButton(text=f"Found: {count_str} vacancies", callback_data=f"fe_{flow_id}_refresh_count")])
        rows.append([InlineKeyboardButton(text=f"Search: {search_text}", callback_data=f"fe_{flow_id}_noop")])
        rows.append([InlineKeyboardButton(text=f"Exp: {exp_str} | {fmt_str}", callback_data=f"fe_{flow_id}_noop")])
    else:
        rows.append([InlineKeyboardButton(text="Set search URL", callback_data=f"fe_{flow_id}_search_url")])

    if config.resume_id:
        resume_label = f"Resume: loaded ({len(config.resume_text)} chars)" if config.resume_text else f"Resume: {config.resume_id}"
        rows.append([InlineKeyboardButton(text=resume_label, callback_data=f"flow_pick_resume_{flow_id}")])
        if config.resume_text:
            rows.append([InlineKeyboardButton(text="Refresh resume", callback_data=f"fe_{flow_id}_refresh_resume")])
    else:
        rows.append([InlineKeyboardButton(text="Resume: not set", callback_data=f"flow_pick_resume_{flow_id}")])
    rows.append([InlineKeyboardButton(text=f"🎯 Откликов за запуск: {config.target_applies}", callback_data=f"fe_{flow_id}_target_applies")])
    rows.append([
        InlineKeyboardButton(text=f"Day: {config.max_apps_per_day}", callback_data=f"fe_{flow_id}_max_apps_per_day"),
        InlineKeyboardButton(text=f"Hour: {config.max_apps_per_hour}", callback_data=f"fe_{flow_id}_max_apps_per_hour"),
    ])
    rows.append([
        InlineKeyboardButton(text=f"Min delay: {config.delay_min}s", callback_data=f"fe_{flow_id}_delay_min"),
        InlineKeyboardButton(text=f"Max delay: {config.delay_max}s", callback_data=f"fe_{flow_id}_delay_max"),
    ])
    rows.append([
        InlineKeyboardButton(text="Prompts", callback_data=f"fe_{flow_id}_prompts"),
        InlineKeyboardButton(text="Test run", callback_data=f"fe_{flow_id}_test"),
    ])
    rows.append([InlineKeyboardButton(text="Back", callback_data=f"flow_{flow_id}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def resume_pick_keyboard(resumes: list, flow_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in resumes:
        status_mark = " * " if r.status == "active" else ""
        rows.append([InlineKeyboardButton(
            text=f"{r.title}{status_mark} ({r.status})",
            callback_data=f"flow_set_resume_{flow_id}_{r.id}",
        )])
    rows.append([InlineKeyboardButton(text="No resume (use default)", callback_data=f"flow_set_resume_{flow_id}_none")])
    rows.append([InlineKeyboardButton(text="Back", callback_data=f"flow_edit_{flow_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Confirm", callback_data=f"confirm_{action}"),
            InlineKeyboardButton(text="Cancel", callback_data="main_menu"),
        ]
    ])


def confirm_logout_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Yes, Logout", callback_data="confirm_hh_logout"),
            InlineKeyboardButton(text="Cancel", callback_data="main_menu"),
        ]
    ])


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="stats_today")],
        [InlineKeyboardButton(text="За 7 дней", callback_data="stats_week")],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_action")]
    ])


def back_keyboard(callback_data: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data=callback_data)]
    ])


def settings_keyboard(gemini_model: str, hh_linked: bool, tz_offset: int = 3) -> InlineKeyboardMarkup:
    hh_status = "Подключен" if hh_linked else "Не подключен"
    tz_label = f"🌐 Часовой пояс: UTC{'+' if tz_offset >= 0 else ''}{tz_offset}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Модель Gemini: {gemini_model}", callback_data="settings_model")],
        [InlineKeyboardButton(text=f"Аккаунт HH.ru: {hh_status}", callback_data="settings_hh")],
        [InlineKeyboardButton(text=tz_label, callback_data="settings_tz")],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")],
    ])


def model_list_keyboard(models: list[dict], current_model: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for m in models:
        marker = " * " if m["name"] == current_model else ""
        rows.append([InlineKeyboardButton(
            text=f"{marker}{m['display']} ({m['name']})",
            callback_data=f"settings_set_model_{m['name']}",
        )])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_reply_keyboard(hh_linked: bool, tz_offset: int = 3) -> ReplyKeyboardMarkup:
    hh_button = KeyboardButton(text="🔓 Выйти из HH") if hh_linked else KeyboardButton(text="🔑 Войти в HH")
    tz_button = KeyboardButton(text=f"🌐 Часовой пояс: UTC{'+' if tz_offset >= 0 else ''}{tz_offset}")
    keyboard = [
        [KeyboardButton(text="🤖 Выбрать модель Gemini"), hh_button],
        [tz_button, KeyboardButton(text="🔔 Уведомления")],
        [KeyboardButton(text="🧹 Очистить кэш"), KeyboardButton(text="🔄 Сбросить лимиты")],
        [KeyboardButton(text="⬅️ Главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


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
        [InlineKeyboardButton(text="Назад", callback_data="settings")]
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
            KeyboardButton(text="🎯 Цель откликов за запуск"),
            KeyboardButton(text="⏱ Суточный лимит"),
            KeyboardButton(text="⏱ Часовой лимит"),
        ],
        [
            KeyboardButton(text="⏳ Мин. задержка"),
            KeyboardButton(text="⏳ Макс. задержка"),
        ],
        [
            KeyboardButton(text="📝 Редактировать промпты"),
            KeyboardButton(text="📂 Назад к Потокам"),
        ]
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
    rows.append([InlineKeyboardButton(text="Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


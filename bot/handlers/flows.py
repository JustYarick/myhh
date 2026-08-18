import logging
import re
from typing import Any
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ...services import flow_entity as flow_db
from ...services.flow_entity import FlowConfig
from ...services.hh_resume import resume_service
from ...services.hh_api_client import hh_api

logger = logging.getLogger(__name__)
flows_router = Router()


class NewFlowState(StatesGroup):
    waiting_for_name = State()

class EditFlowState(StatesGroup):
    waiting_for_value = State()

class DeleteFlowState(StatesGroup):
    confirming = State()


from ..keyboards import (
    flows_reply_keyboard,
    flow_detail_reply_keyboard,
    flow_edit_reply_keyboard,
    flow_prompts_reply_keyboard,
    resume_pick_keyboard,
)


FIELD_LABELS = {
    "name": "Название потока",
    "search_url": "Ссылка поиска (полный URL HH.ru с фильтрами)",
    "resume_id": "ID резюме",
    "max_pages": "Макс. страниц",
    "target_applies": "Цель откликов за запуск",
    "max_apps_per_day": "Суточный лимит откликов",
    "max_apps_per_hour": "Часовой лимит откликов",
    "delay_min": "Мин. задержка (секунды)",
    "delay_max": "Макс. задержка (секунды)",
    "delay_between_pages": "Пауза между страницами (секунды)",
    "schedule": "Расписание (ЧАС_СТАРТА ЧАС_ОКОНЧАНИЯ или 'off')",
    "cover_letter_prompt": "Промпт сопроводительного",
    "analysis_prompt": "Промпт анализа вакансии",
    "custom_rules": "Доп. правила",
    "exclude_employers": "Черный список компаний (через запятую)",
}


async def _check_access(user_id: int) -> bool:
    from ...config import get_settings
    return get_settings().is_allowed_user(user_id)


async def _main_menu_message(message: Message) -> None:
    from . import _main_menu_message as _impl
    await _impl(message)


async def _send_flow_detail(message: Message, flow_id: int) -> None:
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    active_id = await flow_db.get_active_flow_id()
    is_active = (flow.id == active_id)
    await message.answer(
        f"📂 <b>Детали потока: {flow.name}</b>\n\n{flow.summary()}",
        parse_mode="HTML",
        reply_markup=flow_detail_reply_keyboard(is_active)
    )


async def _start_field_edit(message: Message, flow_id: int, field: str, state: FSMContext) -> None:
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return

    current = getattr(flow.config, field, None)
    label = FIELD_LABELS.get(field, field)

    hint = ""
    if field == "search_url":
        hint = "\n\n<i>Вставьте полный URL поиска HH.ru со всеми фильтрами.</i>\n<code>https://hh.ru/search/vacancy?text=DevOps&experience=between1And3&work_format=REMOTE</code>\nИли введите 'clear' для очистки."
    elif field in ("cover_letter_prompt", "analysis_prompt", "custom_rules"):
        hint = "\n\n<i>Доступные плейсхолдеры:</i>\n<code>{title}</code> <code>{employer}</code> <code>{description}</code> <code>{resume}</code>\n<i>Оставьте пустым или напишите 'clear' для очистки.</i>"
    elif field == "exclude_employers":
        hint = "\n\n<i>Введите названия компаний через запятую (например: Сбер, Яндекс, МТС). Бот будет автоматически пропускать отклики в эти компании. Введите 'clear' для очистки списка.</i>"
    elif field == "schedule":
        hint = "\n\n<i>Формат: два числа через пробел — час начала и час окончания (0-23). Например: <code>9 18</code> — автозапуск в 09:00, автопауза в 18:00. Введите 'off' для отключения расписания.</i>"

    await message.answer(
        f"<b>Поток: {flow.name}</b>\n\nРедактирование: <b>{label}</b>\nТекущее значение: <code>{current}</code>{hint}\n\nОтправьте новое значение ниже:",
        parse_mode="HTML",
    )
    await state.update_data(flow_id=flow_id, field=field)
    await state.set_state(EditFlowState.waiting_for_value)


# === Create / List / Detail (reply path) ===

@flows_router.message(F.text.in_({"➕ Create Flow", "➕ Создать Поток"}))
async def create_flow_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    await message.answer("Введите название нового потока:")
    await state.set_state(NewFlowState.waiting_for_name)


@flows_router.message(StateFilter(NewFlowState.waiting_for_name))
async def new_flow_name_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    name = message.text.strip()
    if not name:
        await message.answer("❌ Имя не может быть пустым. Введите название потока:")
        return

    flow = await flow_db.create_flow(name, FlowConfig())
    await state.clear()
    await message.answer(f"✅ Поток «{name}» создан!")
    active_id = await flow_db.get_active_flow_id()
    if active_id is None:
        await flow_db.set_active_flow(flow.id)
        await message.answer("✅ Поток создан и сразу активирован как основной.")

    await _send_flow_detail(message, flow.id)


@flows_router.message(F.text.in_({"📂 Back to Flows", "📂 Назад к Потокам"}))
async def back_to_flows_message_handler(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    flows = await flow_db.list_flows()
    active_id = await flow_db.get_active_flow_id()
    await message.answer("Ваши потоки:", reply_markup=flows_reply_keyboard(flows, active_id))


@flows_router.message(F.text.startswith("📁 Flow: ") | F.text.startswith("📁 Поток: "))
async def flow_detail_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    match = re.search(r"ID: (\d+)", message.text)
    if not match:
        return
    flow_id = int(match.group(1))
    await state.update_data(flow_id=flow_id)
    await _send_flow_detail(message, flow_id)


@flows_router.message(F.text.in_({"🟢 Activate Flow", "🟢 Активировать Поток"}))
async def flow_activate_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    if not flow_id:
        await message.answer("❌ Сначала выберите поток.")
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    await flow_db.set_active_flow(flow_id)
    await message.answer(f"✅ Поток <b>{flow.name}</b> активирован.", parse_mode="HTML")
    flows = await flow_db.list_flows()
    await message.answer("Ваши потоки:", reply_markup=flows_reply_keyboard(flows, flow_id))


@flows_router.message(F.text.in_({"⚙️ Edit Flow", "⚙️ Редактировать Поток", "⚙️ Назад к Потоку"}))
async def flow_edit_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    if not flow_id:
        await message.answer("❌ Сначала выберите поток.")
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    await message.answer(
        f"⚙️ <b>Редактирование потока: {flow.name}</b>\n\nИспользуйте кнопки у сообщения для настройки:",
        parse_mode="HTML",
        reply_markup=flow_edit_reply_keyboard()
    )


@flows_router.message(F.text.in_({"📝 Edit Prompts", "📝 Редактировать промпты"}))
async def flow_edit_prompts_message_handler(message: Message) -> None:
    if not await _check_access(message.from_user.id):
        return
    await message.answer(
        "📝 <b>Редактирование промптов</b>\n\nВыберите, какой промпт отредактировать:",
        parse_mode="HTML",
        reply_markup=flow_prompts_reply_keyboard()
    )


# === Field edit entries (reply path) ===

@flows_router.message(F.text == "🔍 Настроить ссылку поиска")
async def flow_set_search_url_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "search_url", state)


@flows_router.message(F.text == "✏️ Название потока")
async def flow_name_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "name", state)


@flows_router.message(F.text == "📄 Макс. страниц")
async def flow_max_pages_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "max_pages", state)


@flows_router.message(F.text == "⏳ Пауза между страницами")
async def flow_delay_between_pages_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "delay_between_pages", state)


@flows_router.message(F.text == "⏰ Расписание")
async def flow_schedule_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "schedule", state)


@flows_router.message(F.text == "📋 Доп. правила")
async def flow_custom_rules_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "custom_rules", state)


@flows_router.message(F.text == "🚫 Черный список")
async def flow_exclude_employers_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "exclude_employers", state)


@flows_router.message(F.text == "🎯 Цель откликов за запуск")
async def flow_target_applies_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "target_applies", state)


@flows_router.message(F.text == "⏱ Суточный лимит")
async def flow_daily_limit_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "max_apps_per_day", state)


@flows_router.message(F.text == "⏱ Часовой лимит")
async def flow_hourly_limit_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "max_apps_per_hour", state)


@flows_router.message(F.text == "⏳ Мин. задержка")
async def flow_min_delay_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "delay_min", state)


@flows_router.message(F.text == "⏳ Макс. задержка")
async def flow_max_delay_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "delay_max", state)


@flows_router.message(F.text.in_({"📝 Cover Letter Prompt", "📝 Промпт сопроводительного"}))
async def flow_cover_prompt_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "cover_letter_prompt", state)


@flows_router.message(F.text.in_({"📝 Analysis Prompt", "📝 Промпт анализа вакансии"}))
async def flow_analysis_prompt_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "analysis_prompt", state)


@flows_router.message(StateFilter(EditFlowState.waiting_for_value))
async def flow_field_value_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    field = data.get("field")
    await state.clear()

    if not flow_id or not field:
        await message.answer("❌ Ошибка: контекст поля потерян. Попробуйте ещё раз.")
        await _main_menu_message(message)
        return

    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("❌ Поток не найден.")
        await _main_menu_message(message)
        return

    text = message.text.strip()

    if field == "name":
        if not text:
            await message.answer("❌ Имя не может быть пустым.")
            return
        await flow_db.update_flow(flow_id, name=text)
    elif field == "search_url":
        if text.lower() in ("clear", "off", "remove", ""):
            flow.config.search_url = ""
            flow.config.vacancy_count = 0
        elif "hh.ru" not in text and not text.startswith("http"):
            await message.answer("❌ Введите корректный URL HH.ru или 'clear' для очистки.")
            return
        else:
            flow.config.search_url = text
            flow.config.vacancy_count = 0
            await message.answer("⏳ Получаю количество вакансий...")
            try:
                from ...services.hh_search import _url_to_api_params
                params = _url_to_api_params(text, 0)
                rv = await hh_api.search_vacancies(params)
                flow.config.vacancy_count = rv.get("found", 0)
            except Exception as e:
                logger.warning(f"Failed to fetch vacancy count: {e}")
        await flow_db.update_flow(flow_id, config=flow.config)
    elif field == "schedule":
        if text.lower() == "off":
            flow.config.auto_start_hour = None
            flow.config.auto_stop_hour = None
        else:
            try:
                parts = text.split()
                start, stop = int(parts[0]), int(parts[1])
                if not (0 <= start <= 23 and 0 <= stop <= 23):
                    raise ValueError
                flow.config.auto_start_hour = start
                flow.config.auto_stop_hour = stop
            except (ValueError, IndexError):
                await message.answer("❌ Формат: ЧАС_СТАРТА ЧАС_ОКОНЧАНИЯ (0-23) или 'off'")
                return
        await flow_db.update_flow(flow_id, config=flow.config)
    elif field in ("max_pages", "max_apps_per_day", "max_apps_per_hour", "target_applies"):
        try:
            value = int(text)
            if value < 0:
                raise ValueError
            setattr(flow.config, field, value)
        except ValueError:
            await message.answer("❌ Введите целое число (не меньше 0).")
            return
        await flow_db.update_flow(flow_id, config=flow.config)
    elif field in ("delay_min", "delay_max", "delay_between_pages"):
        try:
            value = float(text)
            if value < 0:
                raise ValueError
            setattr(flow.config, field, value)
        except ValueError:
            await message.answer("❌ Введите число (не меньше 0).")
            return
        await flow_db.update_flow(flow_id, config=flow.config)
    else:
        if text.lower() == "clear":
            text = ""
        setattr(flow.config, field, text)
        await flow_db.update_flow(flow_id, config=flow.config)

    flow = await flow_db.get_flow(flow_id)
    await message.answer(f"✅ Значение для поля <b>{FIELD_LABELS.get(field, field)}</b> успешно обновлено.", parse_mode="HTML")
    await message.answer(
        f"⚙️ <b>Редактирование потока: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_reply_keyboard()
    )


# === Resume selection (inline pick, reply-launched) ===

@flows_router.message(F.text.in_({"👤 Set Resume", "👤 Выбрать резюме"}))
async def flow_set_resume_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    if not hh_api.is_authenticated:
        await message.answer("❌ Сначала авторизуйтесь в HH.ru.")
        return
    try:
        resumes = await resume_service.get_resumes()
        if not resumes:
            await message.answer("❌ На вашем HH аккаунте нет доступных резюме.")
            return
        await message.answer("Выберите резюме из списка:", reply_markup=resume_pick_keyboard(resumes, flow_id))
    except Exception as e:
        await message.answer(f"❌ Не удалось получить резюме: {e}. Пожалуйста, убедитесь, что вы вошли в аккаунт HH.")


@flows_router.callback_query(F.data.startswith("flow_set_resume_"))
async def flow_set_resume_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    parts = callback.data.replace("flow_set_resume_", "").split("_", 1)
    try:
        flow_id = int(parts[0])
        resume_id = parts[1] if len(parts) > 1 else ""
    except (ValueError, IndexError):
        return

    if resume_id == "none":
        resume_id = ""

    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Поток не найден.", show_alert=True)
        return

    flow.config.resume_id = resume_id
    flow.config.resume_text = ""
    await flow_db.update_flow(flow_id, config=flow.config)

    if resume_id:
        await callback.message.edit_text(
            f"<b>Поток: {flow.name}</b>\n\n⏳ <i>Получаю текст резюме с HH.ru...</i>",
            parse_mode="HTML"
        )
        text = await resume_service.fetch_resume_text(resume_id)
        if text:
            flow.config.resume_text = text
            await flow_db.update_flow(flow_id, config=flow.config)
            await callback.answer(f"✅ Резюме загружено: {len(text)} символов", show_alert=True)
        else:
            await callback.answer("⚠️ Резюме установлено, но не удалось получить текст", show_alert=True)
    else:
        await flow_db.update_flow(flow_id, config=flow.config)
        await callback.answer("✅ Резюме очищено", show_alert=True)

    flow = await flow_db.get_flow(flow_id)
    await callback.message.answer(
        f"📂 <b>Детали потока: {flow.name}</b>\n\n{flow.summary()}",
        parse_mode="HTML",
        reply_markup=flow_detail_reply_keyboard((await flow_db.get_active_flow_id()) == flow.id)
    )


@flows_router.callback_query(F.data.startswith("flow_back_to_detail_"))
async def flow_back_to_detail_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("flow_back_to_detail_", ""))
    except ValueError:
        return
    await callback.message.answer("Возвращаюсь к потоку:", reply_markup=None)
    await _send_flow_detail(callback.message, flow_id)


# === Refresh actions (reply path) ===

@flows_router.message(F.text == "🔄 Счётчик вакансий")
async def flow_refresh_count_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    if not flow.config.search_url:
        await message.answer("⚠️ Сначала настройте ссылку поиска.")
        await _send_flow_detail(message, flow_id)
        return

    await message.answer("⏳ Обновляю количество вакансий с HH.ru...")
    count = 0
    success = False
    from ...services.hh_search import _url_to_api_params
    try:
        params = _url_to_api_params(flow.config.search_url, 0)
        rv = await hh_api.search_vacancies(params)
        count = rv.get("found", 0)
        flow.config.vacancy_count = count
        await flow_db.update_flow(flow_id, config=flow.config)
        success = True
    except Exception as e:
        logger.warning(f"Failed to fetch vacancy count: {e}")

    await message.answer(
        f"✅ Количество вакансий обновлено: <b>{count}</b>" if success else "❌ Не удалось обновить количество вакансий",
        parse_mode="HTML"
    )
    await _send_flow_detail(message, flow_id)


@flows_router.message(F.text == "🔄 Обновить резюме")
async def flow_refresh_resume_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    if not flow.config.resume_id:
        await message.answer("⚠️ Сначала выберите резюме.")
        await _send_flow_detail(message, flow_id)
        return

    await message.answer("⏳ Получаю текст резюме с HH.ru...")
    text = await resume_service.fetch_resume_text(flow.config.resume_id)
    if text:
        flow.config.resume_text = text
        await flow_db.update_flow(flow_id, config=flow.config)
        await message.answer(f"✅ Резюме обновлено: <b>{len(text)}</b> символов", parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось обновить резюме. Убедитесь, что HH авторизован.")
    await _send_flow_detail(message, flow_id)


# === Test run ===

async def _run_flow_test(flow_id: int, send: Any) -> None:
    """Run a lightweight one-flow test. `send` is an async callable that
    posts an HTML message to the user."""
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await send("Поток не найден.")
        return

    if not flow.config.search_url or not flow.config.resume_id:
        await send("⚠️ У потока должен быть настроен search_url и выбрано резюме для тестового запуска.")
        return

    await send(f"🧪 Запуск тестирования потока: <b>{flow.name}</b>...\nЭто может занять некоторое время.")
    try:
        from ...services.hh_search import HHSearchService
        from ...services.anti_fraud import AntiFraud
        from ...services.gemini import gemini_service
        search_service = HHSearchService()
        af = AntiFraud(flow.config)

        resume_text = flow.config.resume_text or ""
        if not resume_text and flow.config.resume_id:
            await send("⏳ <i>Получаю текст резюме с HH.ru...</i>")
            resume_text = await resume_service.fetch_resume_text(flow.config.resume_id)
            if resume_text:
                flow.config.resume_text = resume_text
                await flow_db.update_flow(flow_id, config=flow.config)
                logger.info(f"Test run: fetched resume text {len(resume_text)} chars for flow {flow_id}")
            else:
                logger.warning(f"Test run: failed to fetch resume text for flow {flow_id}, resume_id={flow.config.resume_id}")
                await send("⚠️ <i>Не удалось получить текст резюме. Убедитесь что HH авторизован.</i>")

        _, cards, _ = await search_service.search_cards(af, 0, flow.config.search_url)
        if not cards:
            await send("❌ Вакансии на первой странице не найдены.")
            return
        await send(f"📋 Найдено {len(cards)} вакансий на первой странице. Запуск AI оценки...")
        sample = cards[:3]
        for idx, c in enumerate(sample):
            desc = await search_service.get_vacancy_description(None, c["url"])
            res = await gemini_service.analyze_vacancy(
                {"title": c["title"], "url": c["url"], "employer": c["employer"], "description": desc},
                prompt_template=flow.config.analysis_prompt,
                resume_text=resume_text,
                custom_rules=flow.config.custom_rules,
            )
            await send(
                f"📄 <b>{idx+1}/{len(sample)}</b>: <a href=\"{c['url']}\">{c['title']}</a>\n"
                f"🏢 {c['employer']}\n"
                f"📊 Релевантность: {res.relevance}/10 (Отклик: {'Да' if res.apply else 'Нет'})\n"
                f"💬 <i>{res.summary}</i>"
            )
        await send("✅ Тестовый запуск успешно завершен.")
    except Exception as e:
        logger.error(f"Test run error: {e}", exc_info=True)
        await send(f"❌ Ошибка тест-рана: {e}")


@flows_router.message(F.text.in_({"🧪 Test Run Flow", "🧪 Тестовый запуск"}))
async def flow_test_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    if not flow_id:
        await message.answer("❌ Сначала выберите поток.")
        return

    async def send(text: str) -> None:
        await message.answer(text, parse_mode="HTML")

    await _run_flow_test(flow_id, send)


# === Delete (reply path) ===

@flows_router.message(F.text.in_({"❌ Delete Flow", "❌ Удалить Поток"}))
async def flow_delete_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    if not flow_id:
        await message.answer("❌ Сначала выберите поток.")
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    await message.answer(f"⚠️ Вы уверены, что хотите удалить поток <b>{flow.name}</b>? Напишите 'да' для подтверждения или 'нет' для отмены.", parse_mode="HTML")
    await state.set_state(DeleteFlowState.confirming)


@flows_router.message(StateFilter(DeleteFlowState.confirming))
async def flow_delete_confirm_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    text = message.text.strip().lower()
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await state.clear()
    
    if text in ("да", "yes", "y", "подтверждаю"):
        flow = await flow_db.get_flow(flow_id)
        if flow:
            active_id = await flow_db.get_active_flow_id()
            await flow_db.delete_flow(flow_id)
            if active_id == flow_id:
                await flow_db.set_active_flow(None)
            await message.answer(f"✅ Поток «{flow.name}» успешно удален.", parse_mode="HTML")
        else:
            await message.answer("Поток не найден.")
    else:
        await message.answer("❌ Удаление отменено.")
    
    flows = await flow_db.list_flows()
    active_id = await flow_db.get_active_flow_id()
    await message.answer("Ваши потоки:", reply_markup=flows_reply_keyboard(flows, active_id))
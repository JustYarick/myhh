import logging
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

import re
from ..keyboards import (
    flows_reply_keyboard,
    flow_detail_reply_keyboard,
    flow_edit_keyboard,
    flow_prompts_reply_keyboard,
)

async def _start_field_edit(message: Message, flow_id: int, field: str, state: FSMContext) -> None:
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Flow not found")
        return

    current = getattr(flow.config, field, None)
    field_labels = {
        "name": "Flow name",
        "search_url": "Search URL (full hh.ru URL with filters)",
        "resume_id": "Resume ID",
        "target_applies": "Количество откликов за запуск",
        "max_apps_per_day": "Max apps per day",
        "max_apps_per_hour": "Max apps per hour",
        "delay_min": "Min delay (seconds)",
        "delay_max": "Max delay (seconds)",
        "cover_letter_prompt": "Cover letter prompt",
        "analysis_prompt": "Analysis prompt",
        "custom_rules": "Дополнительные пользовательские правила",
        "exclude_employers": "Черный список компаний (через запятую)",
    }
    label = field_labels.get(field, field)

    hint = ""
    if field == "search_url":
        hint = "\n\n<i>Paste a full HH.ru search URL with all filters.</i>\n<code>https://hh.ru/search/vacancy?text=DevOps&experience=between1And3&work_format=REMOTE</code>\nOr enter 'clear' to disable."
    elif field in ("cover_letter_prompt", "analysis_prompt", "custom_rules"):
        hint = "\n\n<i>Available placeholders:</i>\n<code>{title}</code> <code>{employer}</code> <code>{description}</code> <code>{resume}</code>\n<i>Оставьте пустым или напишите 'clear' для очистки.</i>"
    elif field == "exclude_employers":
        hint = "\n\n<i>Введите названия компаний через запятую (например: Сбер, Яндекс, МТС). Бот будет автоматически пропускать отклики в эти компании. Введите 'clear' для очистки списка.</i>"

    await message.answer(
        f"<b>Flow: {flow.name}</b>\n\nEdit: <b>{label}</b>\nCurrent: <code>{current}</code>{hint}\n\nType new value below:",
        parse_mode="HTML",
    )
    await state.update_data(flow_id=flow_id, field=field)
    await state.set_state(EditFlowState.waiting_for_value)


@flows_router.message(F.text.in_({"➕ Create Flow", "➕ Создать Поток"}))
async def create_flow_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    await message.answer("Введите название нового потока:")
    await state.set_state(NewFlowState.waiting_for_name)


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
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Поток не найден.")
        return
    active_id = await flow_db.get_active_flow_id()
    is_active = (flow.id == active_id)
    await state.update_data(flow_id=flow_id)
    await message.answer(
        f"📂 <b>Детали потока: {flow.name}</b>\n\n{flow.summary()}",
        parse_mode="HTML",
        reply_markup=flow_detail_reply_keyboard(is_active)
    )


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


@flows_router.message(F.text.in_({"🧪 Test Run Flow", "🧪 Тестовый запуск"}))
async def flow_test_message_handler(message: Message, state: FSMContext) -> None:
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

    if not flow.config.search_url or not flow.config.resume_id:
        await message.answer("⚠️ У потока должен быть настроен search_url и выбрано резюме для тестового запуска.")
        return

    await message.answer(f"🧪 Запуск тестирования потока: <b>{flow.name}</b>...\nЭто может занять некоторое время.", parse_mode="HTML")
    try:
        from ...services.hh_search import HHSearchService
        from ...services.anti_fraud import AntiFraud
        from ...services.hh_resume import resume_service
        search_service = HHSearchService()
        af = AntiFraud(flow.config)

        resume_text = flow.config.resume_text
        if not resume_text and flow.config.resume_id:
            await message.answer("⏳ <i>Получаю текст резюме с HH.ru...</i>", parse_mode="HTML")
            resume_text = await resume_service.fetch_resume_text(flow.config.resume_id)
            if resume_text:
                flow.config.resume_text = resume_text
                await flow_db.update_flow(flow_id, config=flow.config)

        _, cards, _ = await search_service.search_cards(af, 0, flow.config.search_url)
        if not cards:
            await message.answer("❌ Вакансии на первой странице не найдены.")
            return
        await message.answer(f"📋 Найдено {len(cards)} вакансий на первой странице. Запуск AI оценки...")
        sample = cards[:3]
        for idx, c in enumerate(sample):
            desc = await search_service.get_vacancy_description(None, c["url"])
            from ...services.gemini import gemini_service
            res = await gemini_service.analyze_vacancy(
                {"title": c["title"], "url": c["url"], "employer": c["employer"], "description": desc},
                prompt_template=flow.config.analysis_prompt,
                resume_text=flow.config.resume_text,
                custom_rules=flow.config.custom_rules,
            )
            await message.answer(
                f"📄 <b>{idx+1}/{len(sample)}</b>: <a href=\"{c['url']}\">{c['title']}</a>\n"
                f"🏢 {c['employer']}\n"
                f"📊 Релевантность: {res.relevance}/10 (Отклик: {'Да' if res.apply else 'Нет'})\n"
                f"💬 <i>{res.summary}</i>",
                parse_mode="HTML"
            )
        await message.answer("✅ Тестовый запуск успешно завершен.")
    except Exception as e:
        logger.error(f"Test run error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка тест-рана: {e}")


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
        reply_markup=flow_edit_keyboard(flow_id, flow.config)
    )


@flows_router.message(F.text.in_({"📝 Edit Prompts", "📝 Редактировать промпты"}))
async def flow_edit_prompts_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    if not flow_id:
        await message.answer("❌ Сначала выберите поток.")
        return
    await message.answer(
        f"📝 <b>Редактирование промптов потока {flow_id}</b>",
        parse_mode="HTML",
        reply_markup=flow_prompts_reply_keyboard()
    )


@flows_router.message(F.text.in_({"🔍 Set Search URL", "🔍 Настроить ссылку поиска"}))
async def flow_set_search_url_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "search_url", state)


@flows_router.message(F.text.in_({"👤 Set Resume", "👤 Выбрать резюме"}))
async def flow_set_resume_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("Flow not found")
        return
    try:
        resumes = await resume_service.get_resumes()
        if not resumes:
            await message.answer("❌ На вашем HH аккаунте нет доступных резюме.")
            return
        from ..keyboards import resume_pick_keyboard
        await message.answer("Выберите резюме из списка:", reply_markup=resume_pick_keyboard(resumes, flow_id))
    except Exception as e:
        await message.answer(f"❌ Не удалось получить резюме: {e}. Пожалуйста, убедитесь, что вы вошли в аккаунт HH.")


@flows_router.message(F.text == "🚫 Черный список")
async def flow_exclude_employers_message_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "exclude_employers", state)


@flows_router.message(F.text.in_({"🎯 Target Applies", "🎯 Цель откликов за запуск"}))
async def flow_target_applies_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "target_applies", state)


@flows_router.message(F.text.in_({"⏱ Daily Limit", "⏱ Суточный лимит"}))
async def flow_daily_limit_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "max_apps_per_day", state)


@flows_router.message(F.text.in_({"⏱ Hourly Limit", "⏱ Часовой лимит"}))
async def flow_hourly_limit_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "max_apps_per_hour", state)


@flows_router.message(F.text.in_({"⏳ Min Delay", "⏳ Мин. задержка"}))
async def flow_min_delay_message_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow_id = data.get("flow_id")
    await _start_field_edit(message, flow_id, "delay_min", state)


@flows_router.message(F.text.in_({"⏳ Max Delay", "⏳ Макс. задержка"}))
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
            await message.answer(f"✅ Поток '<b>{flow.name}</b>' успешно удален.", parse_mode="HTML")
        else:
            await message.answer("Поток не найден.")
    else:
        await message.answer("❌ Удаление отменено.")
    
    flows = await flow_db.list_flows()
    active_id = await flow_db.get_active_flow_id()
    await message.answer("Ваши потоки:", reply_markup=flows_reply_keyboard(flows, active_id))





async def _check_access(user_id: int) -> bool:
    from ...config import get_settings
    return get_settings().is_allowed_user(user_id)


async def _main_menu_message(message: Message) -> None:
    from . import _main_menu_message as _impl
    await _impl(message)


async def _main_menu_callback(callback: CallbackQuery) -> None:
    from . import _main_menu_callback as _impl
    await _impl(callback)


# === Flow Test Run ===

@flows_router.callback_query(F.data.startswith("flow_test_"))
async def flow_test_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("flow_test_", ""))
    except ValueError:
        return

    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Flow not found", show_alert=True)
        return

    from ...services.hh_search import search_service
    from ...services.gemini import gemini_service
    from ...services.anti_fraud import AntiFraud
    from ..formatting import bold, italic, code, quote, divider, relevance_bar, relevance_color

    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n⏳ <i>Running test... Fetching vacancies from HH.ru...</i>",
        parse_mode="HTML"
    )

    af = AntiFraud(flow.config)
    vacancies = []
    page_title = "Unknown"
    count = 0

    try:
        if flow.config.search_url:
            count, page_title, vacancies = await search_service.search_with_info(
                flow.config.search_url, af, max_descriptions=3
            )
        else:
            await callback.message.edit_text(
                f"<b>Flow: {flow.name}</b>\n\n❌ Set search URL first in flow settings.",
                parse_mode="HTML"
            )
            return

        resume_text = flow.config.resume_text
        if not resume_text and flow.config.resume_id:
            await callback.message.edit_text(
                f"<b>Flow: {flow.name}</b>\n\n⏳ <i>Fetching resume text from HH.ru...</i>",
                parse_mode="HTML"
            )
            resume_text = await resume_service.fetch_resume_text(flow.config.resume_id)
            if resume_text:
                flow.config.resume_text = resume_text
                await flow_db.update_flow(flow_id, config=flow.config)

        if not vacancies:
            await callback.message.edit_text(
                f"<b>Flow: {flow.name}</b>\n\n🔍 <b>Test Run</b>\nFound: {code(str(count))} vacancies.\n\n⚠️ <i>No vacancies found to analyze.</i>",
                parse_mode="HTML"
            )
        else:
            # Send initial test header
            header_msg = (
                f"🔍 {bold('Test Run')}: {bold(flow.name)}\n"
                f"{divider()}\n"
                f"{bold('Page')}: {italic(page_title)}\n"
                f"{bold('Found')}: {code(str(count))} vacancies.\n\n"
                f"⏳ <i>Analyzing first {len(vacancies)} vacancies...</i>"
            )
            await callback.message.answer(header_msg, parse_mode="HTML")

            for i, v in enumerate(vacancies, 1):
                # Update status in the main loading message
                await callback.message.edit_text(
                    f"<b>Flow: {flow.name}</b>\n\n⏳ <i>Analyzing vacancy {i}/{len(vacancies)}...</i>",
                    parse_mode="HTML"
                )

                vacancy_parts = []
                vacancy_parts.append(f"📋 {bold(f'Vacancy {i}/{len(vacancies)}')}")
                vacancy_parts.append(divider())
                vacancy_parts.append(f"{bold('Title')}: <a href=\"{v.url}\">{v.title}</a>")
                vacancy_parts.append(f"{bold('Employer')}: {italic(v.employer)}")
                if v.description:
                    # Prepend a small snippet of the description
                    desc_short = v.description[:120].replace("\n", " ")
                    vacancy_parts.append(f"   {quote(desc_short + '...')}")
                vacancy_parts.append("")

                analysis = None
                try:
                    from ... import database as db
                    cached_res = await db.get_cached_vacancy_result(v.url)
                    if cached_res and cached_res.get("ai_relevance") is not None and cached_res.get("result") != "parsed":
                        from ...models import VacancyAnalysis
                        analysis = VacancyAnalysis(
                            relevance=cached_res["ai_relevance"],
                            salary_match=False,
                            summary=cached_res["ai_summary"] or "",
                            apply=(cached_res["result"] != "analyzed_skip"),
                        )
                    else:
                        analysis = await gemini_service.analyze_vacancy(
                            v.model_dump(),
                            prompt_template=flow.config.analysis_prompt,
                            resume_text=resume_text,
                            custom_rules=flow.config.custom_rules,
                        )
                    color = relevance_color(analysis.relevance)
                    vacancy_parts.append(f"🤖 {bold('AI Analysis')}: {color} {bold(f'{analysis.relevance}/10')} {relevance_bar(analysis.relevance)}")
                    if analysis.summary:
                        vacancy_parts.append(f"   {italic(analysis.summary)}")
                    should_apply = analysis.relevance >= 4
                    apply_text = f"   {bold('✅ WOULD APPLY')}" if should_apply else f"   {bold('⏭ WOULD SKIP')}"
                    vacancy_parts.append(apply_text)
                except Exception as e:
                    vacancy_parts.append(f"🤖 ❌ AI Error: {code(str(e)[:100])}")

                try:
                    cover = await gemini_service.generate_cover_letter(
                        v.model_dump(),
                        prompt_template=flow.config.cover_letter_prompt,
                        resume_text=resume_text,
                        custom_rules=flow.config.custom_rules,
                    )
                    if cover:
                        vacancy_parts.append(f"\n📝 {bold('Cover Letter')}:")
                        vacancy_parts.append(quote(cover))
                except Exception as e:
                    vacancy_parts.append(f"\n📝 ❌ Cover Error: {code(str(e)[:100])}")

                # Send this vacancy test message separately
                await callback.message.answer("\n".join(vacancy_parts), parse_mode="HTML")

            # Finalize main message
            from ..keyboards import flow_detail_keyboard
            await callback.message.edit_text(
                f"✅ <b>Test Run completed!</b>\nProcessed {len(vacancies)} vacancies.",
                parse_mode="HTML",
                reply_markup=flow_detail_keyboard(flow_id, (await flow_db.get_active_flow_id()) == flow_id),
            )

    except Exception as e:
        logger.error(f"Test run error: {e}", exc_info=True)
        from ..keyboards import flow_detail_keyboard
        await callback.message.edit_text(
            f"❌ <b>Test Run Error</b>: {code(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=flow_detail_keyboard(flow_id, (await flow_db.get_active_flow_id()) == flow_id),
        )


# === Flows ===

@flows_router.callback_query(F.data == "flows_list")
async def flows_list_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    flows = await flow_db.list_flows()
    active_id = await flow_db.get_active_flow_id()
    from ..keyboards import flows_list_keyboard
    if not flows:
        await callback.message.edit_text(
            "No flows yet. Create one to start.",
            reply_markup=flows_list_keyboard([], active_id),
        )
    else:
        await callback.message.edit_text(
            "Your flows:",
            reply_markup=flows_list_keyboard(flows, active_id),
        )


@flows_router.callback_query(F.data == "flow_new")
async def flow_new_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("Enter flow name:")
    await state.set_state(NewFlowState.waiting_for_name)


@flows_router.message(StateFilter(NewFlowState.waiting_for_name))
async def new_flow_name_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    name = message.text.strip()
    if not name:
        await message.answer("Name cannot be empty. Enter flow name:")
        return

    flow = await flow_db.create_flow(name, FlowConfig())
    await state.clear()
    await message.answer(f"✅ Flow '{name}' created!")
    active_id = await flow_db.get_active_flow_id()
    if active_id is None:
        await flow_db.set_active_flow(flow.id)
        await message.answer(f"✅ Flow '{name}' activated as default.")

    from ..keyboards import flow_detail_reply_keyboard
    is_active = (await flow_db.get_active_flow_id()) == flow.id
    await message.answer(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_detail_reply_keyboard(flow.id, is_active),
    )


@flows_router.callback_query(F.data.startswith("flow_activate_"))
async def flow_activate_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("flow_activate_", ""))
    except ValueError:
        return
    await flow_db.set_active_flow(flow_id)
    flow = await flow_db.get_flow(flow_id)
    name = flow.name if flow else str(flow_id)
    from ..keyboards import flow_detail_keyboard
    await callback.message.edit_text(
        f"<b>Flow: {name}</b> ✅ (ACTIVE)\n\n{flow.config.summary() if flow else ''}",
        parse_mode="HTML",
        reply_markup=flow_detail_keyboard(flow_id, True),
    )
    await callback.answer(f"Activated: {name}", show_alert=True)


@flows_router.callback_query(F.data.startswith("flow_edit_"))
async def flow_edit_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("flow_edit_", ""))
    except ValueError:
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Flow not found", show_alert=True)
        return
    from ..keyboards import flow_edit_keyboard
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_keyboard(flow_id, flow.config),
    )


@flows_router.callback_query(F.data.startswith("fe_") & F.data.endswith("_refresh_count"))
async def flow_refresh_count_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        parts = callback.data.split("_")
        flow_id = int(parts[1])
    except (ValueError, IndexError):
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow or not flow.config.search_url:
        return

    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n⏳ <i>Fetching vacancy count from HH.ru...</i>",
        parse_mode="HTML"
    )

    count = 0
    success = False
    from ...services.hh_api_client import hh_api
    from ...services.hh_search import _url_to_api_params
    try:
        params = _url_to_api_params(flow.config.search_url, 0)
        rv = await hh_api.search_vacancies(params)
        count = rv.get("found", 0)
        flow.config.vacancy_count = count
        await flow_db.update_flow(flow)
        success = True
    except Exception as e:
        logger.warning(f"Failed to fetch vacancy count: {e}")

    from ..keyboards import flow_edit_keyboard
    status_msg = f"✅ Vacancy count updated: <b>{count}</b>" if success else "❌ Failed to fetch vacancy count"
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n{status_msg}\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_keyboard(flow_id, flow.config),
    )


@flows_router.callback_query(F.data.startswith("fe_") & F.data.endswith("_refresh_resume"))
async def flow_refresh_resume_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        parts = callback.data.split("_")
        flow_id = int(parts[1])
    except (ValueError, IndexError):
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow or not flow.config.resume_id:
        return

    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n⏳ <i>Fetching resume text from HH.ru...</i>",
        parse_mode="HTML"
    )

    text = await resume_service.fetch_resume_text(flow.config.resume_id)
    success = False
    if text:
        flow.config.resume_text = text
        await flow_db.update_flow(flow_id, config=flow.config)
        success = True

    from ..keyboards import flow_edit_keyboard
    status_msg = f"✅ Resume refreshed: <b>{len(text)}</b> chars" if success else "❌ Failed to refresh resume"
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n{status_msg}\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_keyboard(flow_id, flow.config),
    )


# === Resume Selection ===

@flows_router.callback_query(F.data.startswith("flow_pick_resume_"))
async def flow_pick_resume_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("flow_pick_resume_", ""))
    except ValueError:
        return

    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Flow not found", show_alert=True)
        return

    if not hh_api.is_authenticated:
        await callback.answer("Login to HH.ru first", show_alert=True)
        return

    await callback.message.edit_text("Loading resumes from HH.ru...")

    resumes = await resume_service.get_resumes()
    if not resumes:
        await callback.message.edit_text(
            "No resumes found on your HH.ru account.\nCreate a resume on hh.ru first.",
        )
        from ..keyboards import flow_edit_keyboard
        await callback.message.answer(
            f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
            parse_mode="HTML",
            reply_markup=flow_edit_keyboard(flow_id, flow.config),
        )
        return

    from ..keyboards import resume_pick_keyboard
    await callback.message.edit_text(
        f"Flow: {flow.name}\nCurrent resume: {flow.config.resume_id or 'not set'}\n\nSelect resume:",
        reply_markup=resume_pick_keyboard(resumes, flow_id),
    )


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
        await callback.answer("Flow not found", show_alert=True)
        return

    flow.config.resume_id = resume_id
    await flow_db.update_flow(flow_id, config=flow.config)

    if resume_id:
        await callback.message.edit_text(
            f"<b>Flow: {flow.name}</b>\n\n⏳ <i>Fetching resume details from HH.ru...</i>",
            parse_mode="HTML"
        )
        text = await resume_service.fetch_resume_text(resume_id)
        if text:
            flow.config.resume_text = text
            await flow_db.update_flow(flow_id, config=flow.config)
            await callback.answer(f"Resume loaded: {len(text)} chars", show_alert=True)
        else:
            await callback.answer("Resume set but text fetch failed", show_alert=True)
    else:
        flow.config.resume_text = ""
        await flow_db.update_flow(flow_id, config=flow.config)
        await callback.answer("Resume cleared", show_alert=True)

    from ..keyboards import flow_edit_keyboard
    flow = await flow_db.get_flow(flow_id)
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_keyboard(flow_id, flow.config),
    )


# === Flow Field Edit ===

@flows_router.callback_query(F.data.startswith("fe_"))
async def flow_field_edit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    flow_id = int(parts[1])
    field = "_".join(parts[2:])

    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Flow not found", show_alert=True)
        return

    current = getattr(flow.config, field, None)
    field_labels = {
        "name": "Flow name",
        "search_url": "Search URL (full hh.ru URL with filters)",
        "resume_id": "Resume ID",
        "max_pages": "Max pages",
        "target_applies": "Количество откликов за запуск",
        "max_apps_per_day": "Max apps per day",
        "max_apps_per_hour": "Max apps per hour",
        "delay_min": "Min delay (seconds)",
        "delay_max": "Max delay (seconds)",
        "delay_between_pages": "Page change delay",
        "schedule": "Schedule (START_HOUR STOP_HOUR or 'off')",
        "cover_letter_prompt": "Cover letter prompt",
        "analysis_prompt": "Analysis prompt",
        "custom_rules": "Дополнительные пользовательские правила",
    }
    label = field_labels.get(field, field)

    hint = ""
    if field == "search_url":
        hint = "\n\n<i>Paste a full HH.ru search URL with all filters.</i>\n<code>https://hh.ru/search/vacancy?text=DevOps&experience=between1And3&work_format=REMOTE</code>\nOr enter 'clear' to disable."
    elif field in ("cover_letter_prompt", "analysis_prompt", "custom_rules"):
        hint = "\n\n<i>Available placeholders:</i>\n<code>{title}</code> <code>{employer}</code> <code>{description}</code> <code>{resume}</code>\n<i>Оставьте пустым или напишите 'clear' для очистки.</i>"

    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\nEdit: <b>{label}</b>\nCurrent: <code>{current}</code>{hint}",
        parse_mode="HTML",
    )
    await state.update_data(flow_id=flow_id, field=field)
    await state.set_state(EditFlowState.waiting_for_value)


@flows_router.message(StateFilter(EditFlowState.waiting_for_value))
async def flow_field_value_handler(message: Message, state: FSMContext) -> None:
    if not await _check_access(message.from_user.id):
        return
    data = await state.get_data()
    flow_id = data.get("flow_id")
    field = data.get("field")
    await state.clear()

    if not flow_id or not field:
        await message.answer("❌ Error: no field context. Try again.")
        await _main_menu_message(message)
        return

    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await message.answer("❌ Flow not found.")
        await _main_menu_message(message)
        return

    text = message.text.strip()

    if field == "name":
        if not text:
            await message.answer("❌ Name cannot be empty.")
            return
        await flow_db.update_flow(flow_id, name=text)
    elif field == "search_url":
        if text.lower() in ("clear", "off", "remove", ""):
            flow.config.search_url = ""
            flow.config.vacancy_count = 0
        elif "hh.ru" not in text and not text.startswith("http"):
            await message.answer("❌ Enter a valid HH.ru URL or 'clear' to disable.")
            return
        else:
            flow.config.search_url = text
            flow.config.vacancy_count = 0
            await message.answer("⏳ Fetching vacancy count...")
            try:
                from ...services.hh_api_client import hh_api
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
                flow.config.auto_start_hour = int(parts[0])
                flow.config.auto_stop_hour = int(parts[1])
            except (ValueError, IndexError):
                await message.answer("❌ Format: START_HOUR STOP_HOUR or 'off'")
                return
        await flow_db.update_flow(flow_id, config=flow.config)
    elif field in ("max_pages", "max_apps_per_day", "max_apps_per_hour", "target_applies"):
        try:
            setattr(flow.config, field, int(text))
        except ValueError:
            await message.answer("❌ Enter a valid integer.")
            return
        await flow_db.update_flow(flow_id, config=flow.config)
    elif field in ("delay_min", "delay_max", "delay_between_pages"):
        try:
            setattr(flow.config, field, float(text))
        except ValueError:
            await message.answer("❌ Enter a valid number.")
            return
        await flow_db.update_flow(flow_id, config=flow.config)
    else:
        if text.lower() == "clear":
            text = ""
        setattr(flow.config, field, text)
        await flow_db.update_flow(flow_id, config=flow.config)

    flow = await flow_db.get_flow(flow_id)
    from ..keyboards import flow_edit_reply_keyboard
    await message.answer(f"✅ Значение для поля <b>{field}</b> успешно обновлено.", parse_mode="HTML")
    await message.answer(
        f"⚙️ <b>Редактирование потока: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_reply_keyboard(flow_id),
    )


# === Flow Delete ===

@flows_router.callback_query(F.data.startswith("flow_delete_"))
async def flow_delete_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("flow_delete_", ""))
    except ValueError:
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Flow not found", show_alert=True)
        return
    from ..keyboards import confirm_keyboard
    await callback.message.edit_text(
        f"⚠️ Delete flow '<b>{flow.name}</b>'?",
        parse_mode="HTML",
        reply_markup=confirm_keyboard(f"delete_{flow_id}"),
    )
    await state.set_state(DeleteFlowState.confirming)
    await state.update_data(flow_id=flow_id)


@flows_router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    try:
        flow_id = int(callback.data.replace("confirm_delete_", ""))
    except ValueError:
        return
    active_id = await flow_db.get_active_flow_id()
    await flow_db.delete_flow(flow_id)
    if active_id == flow_id:
        await flow_db.set_active_flow(None)
    await callback.message.edit_text("✅ Flow deleted.")
    await _main_menu_callback(callback)


# === Catch-all flow detail (must be LAST) ===

@flows_router.callback_query(F.data.startswith("flow_"))
async def flow_detail_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer()
    try:
        flow_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow:
        await callback.answer("Flow not found", show_alert=True)
        return
    active_id = await flow_db.get_active_flow_id()
    is_active = flow_id == active_id
    from ..keyboards import flow_detail_keyboard
    status = " ✅ (ACTIVE)" if is_active else ""
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}{status}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_detail_keyboard(flow_id, is_active),
    )

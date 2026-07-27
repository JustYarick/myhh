import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ...services import flow_entity as flow_db
from ...services.flow_entity import FlowConfig
from ...services.hh_auth import hh_auth

logger = logging.getLogger(__name__)
flows_router = Router()


class NewFlowState(StatesGroup):
    waiting_for_name = State()


class EditFlowState(StatesGroup):
    waiting_for_value = State()


class DeleteFlowState(StatesGroup):
    confirming = State()


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

    await callback.message.edit_text("⏳ Running test...")

    af = AntiFraud(flow.config)
    parts = []

    try:
        if flow.config.search_url:
            url = flow.config.search_url
            count, page_title, vacancies = await search_service.search_with_info(
                url, af, max_descriptions=3
            )
            parts.append(f"{bold('🔍 Test Run')}: {bold(flow.name)}")
            parts.append(divider())
            parts.append(f"{bold('Page')}: {italic(page_title)}")
            parts.append(f"{bold('Found')}: {code(str(count))} vacancies")
            parts.append("")
        else:
            await callback.answer("Set search URL first", show_alert=True)
            return

        from ...services.flow_entity import get_setting, set_setting
        resume_text = flow.config.resume_text

        if not resume_text and flow.config.resume_id:
            await callback.answer("Loading resume...", show_alert=False)
            resume_text = await hh_auth.get_resume_text(flow.config.resume_id)
            if resume_text:
                flow.config.resume_text = resume_text
                await flow_db.update_flow(flow_id, config=flow.config)

        if not vacancies:
            parts.append(f"{italic('No vacancies found.')}")
        else:
            parts.append(f"{bold(f'📋 {len(vacancies)} Vacancies')}")
            parts.append("")

            for i, v in enumerate(vacancies, 1):
                parts.append(f"{bold(f'{i}. {v.title}')}")
                parts.append(f"   {italic(v.employer)}")
                if v.description:
                    desc_short = v.description[:120].replace("\n", " ")
                    parts.append(f"   {quote(desc_short + '...')}")
                parts.append("")

                analysis = None
                try:
                    analysis = await gemini_service.analyze_vacancy(
                        v.model_dump(),
                        prompt_template=flow.config.analysis_prompt,
                        resume_text=resume_text,
                    )
                    color = relevance_color(analysis.relevance)
                    parts.append(f"   {bold('AI Analysis')}: {color} {bold(f'{analysis.relevance}/10')} {relevance_bar(analysis.relevance)}")
                    summary_short = analysis.summary[:80]
                    if summary_short:
                        parts.append(f"   {italic(summary_short)}")
                    should_apply = analysis.relevance >= 4
                    apply_text = f"{bold('✅ APPLY')}" if should_apply else f"{bold('⏭ SKIP')}"
                    parts.append(f"   {apply_text}")
                except Exception as e:
                    parts.append(f"   ❌ AI Error: {code(str(e)[:100])}")

                try:
                    cover = await gemini_service.generate_cover_letter(
                        v.model_dump(),
                        prompt_template=flow.config.cover_letter_prompt,
                        resume_text=resume_text,
                    )
                    if cover:
                        parts.append(f"\n   {bold('📝 Cover Letter')}:")
                        parts.append(f"   {quote(cover[:400])}")
                except Exception as e:
                    parts.append(f"   ❌ Cover Error: {code(str(e)[:100])}")

                parts.append("")
                parts.append(divider())
                parts.append("")

    except Exception as e:
        parts.append(f"\n❌ {bold('Error')}: {code(str(e)[:200])}")

    result_text = "\n".join(parts)
    if len(result_text) > 4000:
        result_text = result_text[:3950] + f"\n\n{italic('... (truncated)')}"

    from ..keyboards import flow_detail_keyboard
    try:
        await callback.message.edit_text(
            result_text,
            parse_mode="HTML",
            reply_markup=flow_detail_keyboard(flow_id, (await flow_db.get_active_flow_id()) == flow_id),
        )
    except Exception:
        await callback.message.answer(result_text, parse_mode="HTML")


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

    from ..keyboards import flow_detail_keyboard
    is_active = (await flow_db.get_active_flow_id()) == flow.id
    await message.answer(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_detail_keyboard(flow.id, is_active),
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
    await callback.answer("Fetching count...")
    try:
        parts = callback.data.split("_")
        flow_id = int(parts[1])
    except (ValueError, IndexError):
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow or not flow.config.search_url:
        return
    from ...services.hh_search import search_service
    from ...services.anti_fraud import AntiFraud
    from ...services.browser import browser_manager
    af = AntiFraud(flow.config)
    try:
        async with browser_manager.get_page(use_session=True) as page:
            await page.goto(flow.config.search_url, wait_until="commit", timeout=60000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            count = await search_service.get_vacancy_count(page)
            flow.config.vacancy_count = count
            await flow_db.update_flow(flow)
    except Exception as e:
        logger.warning(f"Failed to fetch vacancy count: {e}")
    from ..keyboards import flow_edit_keyboard
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_keyboard(flow_id, flow.config),
    )


@flows_router.callback_query(F.data.startswith("fe_") & F.data.endswith("_refresh_resume"))
async def flow_refresh_resume_callback(callback: CallbackQuery) -> None:
    if not await _check_access(callback.from_user.id):
        return
    await callback.answer("Refreshing resume...", show_alert=False)
    try:
        parts = callback.data.split("_")
        flow_id = int(parts[1])
    except (ValueError, IndexError):
        return
    flow = await flow_db.get_flow(flow_id)
    if not flow or not flow.config.resume_id:
        return
    text = await hh_auth.get_resume_text(flow.config.resume_id)
    if text:
        flow.config.resume_text = text
        await flow_db.update_flow(flow_id, config=flow.config)
        await callback.answer(f"Resume refreshed: {len(text)} chars", show_alert=True)
    else:
        await callback.answer("Failed to refresh resume", show_alert=True)
    from ..keyboards import flow_edit_keyboard
    await callback.message.edit_text(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
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

    if not hh_auth.session_exists():
        await callback.answer("Login to HH.ru first", show_alert=True)
        return

    await callback.message.edit_text("Loading resumes from HH.ru...")

    resumes = await hh_auth.get_resumes()
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
        await callback.answer("Loading resume text...", show_alert=False)
        text = await hh_auth.get_resume_text(resume_id)
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
        "max_apps_per_day": "Max apps per day",
        "max_apps_per_hour": "Max apps per hour",
        "delay_min": "Min delay (seconds)",
        "delay_max": "Max delay (seconds)",
        "delay_between_pages": "Page change delay",
        "schedule": "Schedule (START_HOUR STOP_HOUR or 'off')",
        "cover_letter_prompt": "Cover letter prompt",
        "analysis_prompt": "Analysis prompt",
    }
    label = field_labels.get(field, field)

    hint = ""
    if field == "search_url":
        hint = "\n\n<i>Paste a full HH.ru search URL with all filters.</i>\n<code>https://hh.ru/search/vacancy?text=DevOps&experience=between1And3&work_format=REMOTE</code>\nOr enter 'clear' to disable."
    elif field in ("cover_letter_prompt", "analysis_prompt"):
        hint = "\n\n<i>Available placeholders:</i>\n<code>{title}</code> <code>{employer}</code> <code>{description}</code> <code>{resume}</code>"

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
                from ...services.hh_search import search_service
                from ...services.browser import browser_manager
                from ...services.anti_fraud import AntiFraud
                af = AntiFraud(flow.config)
                async with browser_manager.get_page(use_session=True) as page:
                    await page.goto(text, wait_until="commit", timeout=60000)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    flow.config.vacancy_count = await search_service.get_vacancy_count(page)
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
    elif field in ("max_pages", "max_apps_per_day", "max_apps_per_hour"):
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
        setattr(flow.config, field, text)
        await flow_db.update_flow(flow_id, config=flow.config)

    flow = await flow_db.get_flow(flow_id)
    from ..keyboards import flow_edit_keyboard
    await message.answer(f"✅ Updated <b>{field}</b>.", parse_mode="HTML")
    await message.answer(
        f"<b>Flow: {flow.name}</b>\n\n{flow.config.summary()}",
        parse_mode="HTML",
        reply_markup=flow_edit_keyboard(flow_id, flow.config),
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

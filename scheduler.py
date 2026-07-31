import asyncio
import logging
from enum import Enum
from typing import Optional, Callable, Awaitable

from .config import get_settings
from .models import BotState, ApplyStatus, Vacancy
from . import database as db
from .database import extract_vacancy_id
from .services.anti_fraud import AntiFraud
from .services.hh_search import search_service, prepare_search_url
from .services.hh_apply import apply_service
from .services.gemini import gemini_service
from .services.flow_entity import FlowConfig, get_active_flow, update_flow, get_active_flow_id
from .services.browser import browser_manager
from .services.hh_auth import hh_auth
from .bot.formatting import format_apply_success, format_session_finished, format_scheduler_status, format_monitoring_finished

logger = logging.getLogger(__name__)

def check_requires_test(description: str) -> bool:
    desc = description.lower()
    
    # Prompt injection check
    if "проигнорируй" in desc or "банан" in desc or "рецепт блинчиков" in desc:
        return True
        
    # Check for test task requirements, excluding general QA experience
    test_keywords = [
        "тестовое задание",
        "тестового задания",
        "выполнить тест",
        "пройти тест",
        "ссылка на тест",
        "пройти тестирование перед",
        "тест-задание",
        "выполнение теста",
        "тестовое тз"
    ]
    for kw in test_keywords:
        if kw in desc:
            return True
            
    return False


class VacancyLockManager:
    def __init__(self) -> None:
        self._processing_urls: set[str] = set()
        self._lock = asyncio.Lock()

    async def acquire(self, url: str) -> bool:
        async with self._lock:
            if url in self._processing_urls:
                return False
            self._processing_urls.add(url)
            return True

    async def release(self, url: str) -> None:
        async with self._lock:
            self._processing_urls.discard(url)


vacancy_lock_manager = VacancyLockManager()


class RunState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class Scheduler:
    def __init__(self, name: str = "Scheduler") -> None:
        self.name = name
        self.state = BotState()
        self._run_state = RunState.IDLE
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._notify_callback: Optional[Callable[[str], Awaitable[None]]] = None

    def set_notify_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self._notify_callback = callback

    async def _notify(self, message: str, notify_type: str = "info") -> None:
        logger.info(f"[NOTIFY] {message}")
        enabled = True
        if notify_type == "success":
            enabled = (await db.get_setting("notify_success", "true")) == "true"
        elif notify_type == "error":
            enabled = (await db.get_setting("notify_error", "true")) == "true"
        elif notify_type == "skip":
            enabled = (await db.get_setting("notify_skip", "false")) == "true"

        if enabled and self._notify_callback:
            try:
                await self._notify_callback(message)
            except Exception as e:
                logger.error(f"Notification failed: {e}")

    def _check_stop(self) -> bool:
        return self._stop_event.is_set()

    async def _check_pause(self) -> None:
        while self._run_state == RunState.PAUSED:
            if self._stop_event.is_set():
                return
            await asyncio.sleep(1)

    async def _run_interruptible(self, coro):
        await self._check_pause()
        if self._check_stop():
            raise asyncio.CancelledError("Scheduler stopped by user request")

        task = asyncio.create_task(coro)
        stop_task = asyncio.create_task(self._stop_event.wait())

        done, pending = await asyncio.wait(
            {task, stop_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        for p in pending:
            p.cancel()
            try:
                await p
            except asyncio.CancelledError:
                pass

        if stop_task in done:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise asyncio.CancelledError("Scheduler stopped by user request")

        return task.result()

    async def start(self) -> None:
        if self._run_state == RunState.RUNNING:
            await self._notify("⚠️ Already running!")
            return

        settings = get_settings()
        if not settings.session_file.exists():
            await self._notify("❌ HH.ru session not found. Login first.")
            return

        flow = await get_active_flow()
        if not flow:
            await self._notify("❌ No active flow. Create and activate one first.")
            return

        # Prepare start notification text
        mod_url = prepare_search_url(flow.config.search_url)
        gemini_model = await db.get_setting("gemini_model", "gemini-2.0-flash")
        
        # Local user time
        from datetime import datetime, timedelta, timezone
        tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))
        user_time = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
        time_str = user_time.strftime("%d/%m/%y %H:%M:%S")

        if self.name == "Monitoring":
            last_newest_url = await db.get_setting(f"last_newest_vacancy_{flow.id}", "")
            if last_newest_url:
                start_msg = "🔍 <b>Начало обхода новых вакансий...</b>"
            else:
                start_msg = None
        else:
            start_msg = (
                f"🚀 <b>Запуск планировщика</b>\n\n"
                f"📅 Время запуска: <code>{time_str}</code> (UTC{'+' if tz_offset >= 0 else ''}{tz_offset})\n"
                f"📂 Поток: <b>{flow.name}</b>\n"
                f"🎯 Цель за запуск: <b>{flow.config.target_applies}</b> откл.\n"
                f"⏱ Суточный лимит: <b>{flow.config.max_apps_per_day}</b> откл.\n"
                f"🤖 Модель ИИ: <code>{gemini_model}</code>\n"
                f"⏱ Паузы: <b>{flow.config.delay_min}-{flow.config.delay_max}</b> сек.\n"
                f"🔗 Ссылка поиска:\n<code>{mod_url}</code>"
            )

        self._stop_event.clear()
        self._resume_event.set()
        self._run_state = RunState.RUNNING
        self.state = BotState(is_running=True)
        self._task = asyncio.create_task(self._run_loop(flow.config))
        if start_msg:
            await self._notify(start_msg)

    async def stop(self) -> None:
        if self._run_state == RunState.IDLE:
            await self._notify("⚠️ Not running!")
            return

        self._stop_event.set()
        self._resume_event.set()
        self._run_state = RunState.IDLE

        if self._task:
            self._task.cancel()
            self._task = None

        self.state = BotState()
        await self._notify("⏹ <b>Работа планировщика остановлена</b>")

    async def pause(self) -> None:
        if self._run_state != RunState.RUNNING:
            await self._notify("⚠️ Nothing to pause.")
            return

        self._run_state = RunState.PAUSED
        self._resume_event.clear()
        self.state.paused = True
        await self._notify("⏸ <b>Paused</b>")

    async def resume(self) -> None:
        if self._run_state != RunState.PAUSED:
            await self._notify("⚠️ Not paused.")
            return

        self._run_state = RunState.RUNNING
        self._resume_event.set()
        self.state.paused = False
        await self._notify("▶️ <b>Resumed</b>")

    async def _process_card(
        self,
        card: dict,
        page,
        anti_fraud: AntiFraud,
        resume_text: str,
        config: FlowConfig,
        index: int,
        total: int,
    ) -> None:
        """
        Handles a single vacancy card end-to-end. Any exception raised here
        is caught by the caller and logged as a per-vacancy failure - it
        must NEVER be allowed to bubble up and kill the whole run.
        """
        if await db.was_vacancy_applied(card["url"]):
            logger.debug(f"Already applied: {card['title']}")
            return

        if await db.is_vacancy_cached(card["url"]):
            logger.debug(f"Already processed (cache): {card['title']}")
            return

        if not await vacancy_lock_manager.acquire(card["url"]):
            logger.debug(f"Vacancy is already being processed by another runner: {card['title']}")
            return

        await self._notify(f"📄 <b>{index + 1}/{total}</b>: {card['title']}...")

        await anti_fraud.pre_action_delay()
        description = await self._run_interruptible(
            search_service.get_vacancy_description(page, card["url"])
        )
        await anti_fraud.post_action_delay()

        vacancy = Vacancy(
            title=card["title"],
            url=card["url"],
            employer=card["employer"],
            description=description,
        )

        if self._check_stop():
            return

        logger.debug(f"Analyzing: {vacancy.title} @ {vacancy.employer}")
        analysis_result = None
        try:
            cached_res = await db.get_cached_vacancy_result(vacancy.url)
            if cached_res and cached_res.get("ai_relevance") is not None and cached_res.get("result") != "parsed":
                from .models import VacancyAnalysis
                analysis_result = VacancyAnalysis(
                    relevance=cached_res["ai_relevance"],
                    salary_match=False,
                    summary=cached_res["ai_summary"] or "",
                    apply=(cached_res["result"] != "analyzed_skip"),
                )
                logger.info(f"Using cached AI analysis for {vacancy.title}: relevance={analysis_result.relevance}")
        except Exception as cache_err:
            logger.debug(f"Failed to read AI cache: {cache_err}")

        if not analysis_result:
            try:
                analysis_result = await self._run_interruptible(
                    gemini_service.analyze_vacancy(
                        vacancy.model_dump(),
                        prompt_template=config.analysis_prompt,
                        resume_text=resume_text,
                    )
                )
            except Exception as e:
                logger.error(f"Analysis failed: {e}")

        # Check if the vacancy requires testing or is a prompt injection trap
        is_test = check_requires_test(vacancy.description) or (analysis_result and analysis_result.requires_test)
        if is_test:
            if analysis_result:
                analysis_result.apply = False
                analysis_result.relevance = 1
                if not analysis_result.summary or analysis_result.summary == "AI parse error":
                    analysis_result.summary = "Обнаружено требование о прохождении тестирования / ловушка"
            else:
                from .models.vacancy import VacancyAnalysis
                analysis_result = VacancyAnalysis(
                    relevance=1,
                    salary_match=False,
                    summary="Обнаружено требование о прохождении тестирования / ловушка",
                    apply=False,
                    requires_test=True
                )
            await self._skip_vacancy(vacancy, analysis_result, is_test_skip=True)
            return

        if analysis_result and analysis_result.relevance < 4:
            await self._skip_vacancy(vacancy, analysis_result)
            return

        if self._check_stop():
            return

        cover_letter = ""
        try:
            cover_letter = await self._run_interruptible(
                gemini_service.generate_cover_letter(
                    vacancy.model_dump(),
                    prompt_template=config.cover_letter_prompt,
                    resume_text=resume_text,
                )
            )
        except Exception as e:
            logger.error(f"Cover letter generation failed: {e}")

        # Validate cover letter to prevent sending API error messages or junk text to employers
        is_letter_valid = True
        letter_error_reason = ""
        if not cover_letter or len(cover_letter.strip()) < 10:
            is_letter_valid = False
            letter_error_reason = "Empty or too short cover letter"
        else:
            stop_words = ["error", "fail", "ошибка", "gemini", "api key", "исключение", "json", "prompt", "failed"]
            lower_letter = cover_letter.lower()
            for stop_word in stop_words:
                if stop_word in lower_letter:
                    is_letter_valid = False
                    letter_error_reason = f"Cover letter contains error pattern: '{stop_word}'"
                    break

        if not is_letter_valid:
            logger.error(f"AI Cover letter validation failed: {letter_error_reason}. Blocked application.")
            await db.save_application(
                vacancy_url=vacancy.url,
                title=vacancy.title,
                employer=vacancy.employer,
                description=vacancy.description[:500],
                cover_letter=cover_letter,
                ai_relevance=(analysis_result.relevance if analysis_result else 0),
                ai_analysis=(analysis_result.summary if analysis_result else ""),
                status="error",
                error_message=f"AI Validation failed: {letter_error_reason}",
            )
            self.state.vacancies_processed += 1
            await self._notify(
                f"❌ <b>Blocked Apply</b> (AI Error): <a href=\"{vacancy.url}\">{vacancy.title}</a>\n"
                f"⚠️ {letter_error_reason}",
                notify_type="error"
            )
            return

        await anti_fraud.random_delay()
        result = await self._run_interruptible(
            apply_service.apply(
                vacancy.url, cover_letter,
                af=anti_fraud, resume_id=config.resume_id,
                existing_page=page,
            )
        )

        await db.save_application(
            vacancy_url=vacancy.url,
            title=vacancy.title,
            employer=vacancy.employer,
            description=vacancy.description[:500],
            cover_letter=cover_letter,
            ai_relevance=(analysis_result.relevance if analysis_result else 0),
            ai_analysis=(analysis_result.summary if analysis_result else ""),
            status=result.status.value,
            error_message=result.message,
        )

        if result.status != ApplyStatus.ERROR:
            await db.cache_vacancy_result(
                vacancy_url=vacancy.url,
                title=vacancy.title,
                employer=vacancy.employer,
                ai_relevance=analysis_result.relevance if analysis_result else 0,
                ai_summary=analysis_result.summary if analysis_result else "",
                result=result.status.value,
            )

        self.state.vacancies_processed += 1
        await self._handle_apply_result(vacancy, result, analysis_result, cover_letter)
        return result.status == ApplyStatus.SUCCESS

    async def _skip_vacancy(self, vacancy: Vacancy, analysis_result, is_test_skip: bool = False) -> None:
        await db.cache_vacancy_result(
            vacancy_url=vacancy.url,
            title=vacancy.title,
            employer=vacancy.employer,
            ai_relevance=analysis_result.relevance,
            ai_summary=analysis_result.summary,
            result="analyzed_skip",
        )
        await db.save_application(
            vacancy_url=vacancy.url,
            title=vacancy.title,
            employer=vacancy.employer,
            description=vacancy.description[:500],
            cover_letter="",
            ai_relevance=analysis_result.relevance,
            ai_analysis=analysis_result.summary,
            status="analyzed_skip",
            error_message=analysis_result.summary,
        )
        logger.info(
            f"Skipped (AI): {vacancy.title} "
            f"(relevance={analysis_result.relevance})"
        )
        if is_test_skip:
            await self._notify(
                f"⏭ <b>Пропущено (Требуется тест / Ловушка):</b> <a href=\"{vacancy.url}\">{vacancy.title}</a> @ {vacancy.employer}\n"
                f"<blockquote>{analysis_result.summary}</blockquote>",
                notify_type="info"
            )
        else:
            await self._notify(
                f"⏭ <b>Пропущено</b> (Релевантность={analysis_result.relevance}/10): <a href=\"{vacancy.url}\">{vacancy.title}</a> @ {vacancy.employer}\n"
                f"<i>{analysis_result.summary}</i>",
                notify_type="skip"
            )

    async def _handle_apply_result(
        self, vacancy: Vacancy, result, analysis_result, cover_letter: str
    ) -> None:
        if result.status == ApplyStatus.SUCCESS:
            self.state.applied_today += 1
            logger.info(f"Applied: {vacancy.title}")
            msg = format_apply_success(
                title=vacancy.title,
                url=vacancy.url,
                employer=vacancy.employer,
                relevance=analysis_result.relevance if analysis_result else None,
                summary=analysis_result.summary if analysis_result else None,
                cover_letter=cover_letter,
            )
            await self._notify(msg, notify_type="success")
        elif result.status == ApplyStatus.ANALYZED_SKIP and result.message == "requires_questions":
            logger.info(f"Skipped due to questionnaire requirement: {vacancy.title}")
            await db.save_application(
                vacancy_url=vacancy.url,
                title=vacancy.title,
                employer=vacancy.employer,
                description=vacancy.description[:500],
                cover_letter="",
                ai_relevance=analysis_result.relevance if analysis_result else 0,
                ai_analysis="Требуется заполнение вопросов на HH.ru",
                status="skipped_questions",
                error_message="Требуется заполнение вопросов/анкеты работодателя перед откликом",
            )
            await db.cache_vacancy_result(
                vacancy_url=vacancy.url,
                title=vacancy.title,
                employer=vacancy.employer,
                ai_relevance=analysis_result.relevance if analysis_result else 0,
                ai_summary="Требуется заполнение вопросов",
                result="skipped_questions",
            )
            await self._notify(
                f"⏭ <b>Пропущено (Отклик требует ответов на вопросы):</b> <a href=\"{vacancy.url}\">{vacancy.title}</a> @ {vacancy.employer}\n"
                f"<i>Работодатель требует ответить на вопросы на сайте HH.ru перед подачей заявки.</i>",
                notify_type="info"
            )
        elif result.status == ApplyStatus.CAPTCHA:
            self.state.captcha_detected = True
            self._run_state = RunState.PAUSED
            self._resume_event.clear()
            self.state.paused = True
            await self._notify(
                "🔒 <b>Captcha on apply!</b> Paused. Resume when ready.",
                notify_type="error"
            )
            raise _CaptchaPause()
        else:
            logger.info(
                f"Apply result: {result.status.value} - {result.message} "
                f"for {vacancy.title}"
            )

    async def _run_loop(self, config: FlowConfig) -> None:
        anti_fraud = AntiFraud(config)
        resume_text = config.resume_text

        flow_id = await get_active_flow_id()
        last_newest_url = ""
        if flow_id:
            last_newest_url = await db.get_setting(f"last_newest_vacancy_{flow_id}", "")
            logger.info(f"Loaded boundary url for flow {flow_id}: {last_newest_url}")

        new_boundary_url = None

        if not resume_text and config.resume_id:
            logger.info(f"Resume text not cached, fetching for resume_id={config.resume_id}")
            resume_text = await self._run_interruptible(hh_auth.get_resume_text(config.resume_id))
            if resume_text:
                config.resume_text = resume_text
                if flow_id:
                    await update_flow(flow_id, config=config)
                logger.info(f"Resume text fetched and cached: {len(resume_text)} chars")
            else:
                logger.warning("Failed to fetch resume text, proceeding without it")

        logger.info(f"Starting run loop: max_pages={config.max_pages}, "
                     f"search_url={config.search_url[:60] if config.search_url else ''}, "
                     f"resume={'loaded' if resume_text else 'none'}")

        context = None
        page = None
        try:
            context, page = await browser_manager.create_run_context()
            logger.info("Created shared browser context for run")
            session_success_count = 0
            session_skipped_count = 0
            session_error_count = 0
            session_total_processed = 0
            consecutive_cached_count = 0
            first_cached_in_sequence = None

            # Search up to 40 pages (hh.ru limit) to satisfy the target apply limit
            max_search_pages = 40
            for page_num in range(max_search_pages):
                if self._check_stop():
                    break

                await self._check_pause()
                if self._check_stop():
                    break

                allowed, reason = await anti_fraud.check_rate_limits()
                if not allowed:
                    await self._notify(f"🛑 Stopping: {reason}", notify_type="error")
                    break

                if last_newest_url or self.name != "Monitoring":
                    await self._notify(f"🔍 Searching page <b>{page_num + 1}/{max_search_pages}</b>...")
                self.state.current_page = page_num + 1

                # A search failure (timeout, layout change, network blip)
                # must skip this page, not kill the whole run.
                try:
                    page, cards, new_ctx = await self._run_interruptible(
                        search_service.search_cards(
                            anti_fraud=anti_fraud,
                            page_num=page_num,
                            url=config.search_url,
                            existing_page=page,
                        )
                    )
                    if new_ctx and not context:
                        context = new_ctx
                except RuntimeError as e:
                    if "captcha" in str(e).lower():
                        anti_fraud.captcha_detected()
                        self._run_state = RunState.PAUSED
                        self._resume_event.clear()
                        self.state.paused = True
                        await self._notify(
                            "🔒 <b>Captcha detected!</b> Pausing. Wait and resume.",
                            notify_type="error"
                        )
                        continue
                    logger.error(f"Search failed on page {page_num + 1}: {e}", exc_info=True)
                    await self._notify(f"⚠️ Search error on page {page_num + 1}, skipping: {e}", notify_type="error")
                    continue
                except Exception as e:
                    logger.error(f"Unexpected search error on page {page_num + 1}: {e}", exc_info=True)
                    await self._notify(f"⚠️ Unexpected search error on page {page_num + 1}, skipping: {e}", notify_type="error")
                    continue

                if not cards:
                    await self._notify(f"📭 No vacancies found on page {page_num + 1}")
                    break

                if page_num == 0:
                    first_url = cards[0]["url"]
                    first_id = extract_vacancy_id(first_url) if first_url else ""
                    new_boundary_url = first_id

                    # If there's no baseline set, establish it now and stop
                    if not last_newest_url:
                        await self._notify(
                            f"🏁 <b>Базовая точка мониторинга установлена</b>:\n"
                            f"Вакансия ID: <code>{first_id}</code>\n"
                            f"С этого момента бот будет отслеживать новые вакансии относительно неё."
                        )
                        break

                    if last_newest_url and first_id == last_newest_url:
                        await self._notify("📭 <b>Новых вакансий нет</b> с момента запуска мониторинга. Завершаем.")
                        break

                await self._notify(f"📋 Found <b>{len(cards)}</b> vacancies on page {page_num + 1}")

                boundary_reached = False
                for i, card in enumerate(cards):
                    if self._check_stop():
                        break

                    await self._check_pause()
                    if self._check_stop():
                        break

                    card_id = extract_vacancy_id(card.get("url", ""))

                    if last_newest_url and card_id == last_newest_url:
                        await self._notify("🏁 <b>Достигнута граница ранее обработанных вакансий.</b> Завершаем.")
                        boundary_reached = True
                        break

                    # Early exit if we encounter 2 consecutive cached vacancies
                    # (remembers the first cached vacancy of the sequence as the new baseline)
                    is_applied = await db.was_vacancy_applied(card.get("url", ""))
                    is_cached = await db.is_vacancy_cached(card.get("url", ""))
                    if is_applied or is_cached:
                        if consecutive_cached_count == 0:
                            first_cached_in_sequence = card_id
                        consecutive_cached_count += 1
                        if consecutive_cached_count >= 2:
                            await self._notify("🏁 <b>Достигнута граница ранее обработанных вакансий (по кэшу).</b> Завершаем.")
                            new_boundary_url = first_cached_in_sequence
                            boundary_reached = True
                            break
                    else:
                        consecutive_cached_count = 0

                    allowed, reason = await anti_fraud.check_rate_limits()
                    if not allowed:
                        await self._notify(f"🛑 Stopping: {reason}", notify_type="error")
                        self._stop_event.set()
                        break

                    # Each vacancy gets its own safety net. Whatever goes
                    # wrong inside (AI call, apply, DB write) is logged and
                    # we move on to the next card - it never takes down the
                    # whole run anymore.
                    try:
                        # Wrap vacancy processing with a hard timeout of 3 minutes (180s)
                        applied = await asyncio.wait_for(
                            self._process_card(
                                card, page, anti_fraud, resume_text, config, i, len(cards)
                            ),
                            timeout=180.0
                        )
                        session_total_processed += 1
                        if applied:
                            session_success_count += 1
                            if session_success_count >= config.target_applies:
                                await self._notify(f"🎯 <b>Целевой лимит достигнут!</b> Отправлено <b>{session_success_count}</b> откликов за этот запуск.")
                                self._stop_event.set()
                                break
                        else:
                            session_skipped_count += 1
                    except _CaptchaPause:
                        break
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        session_total_processed += 1
                        session_error_count += 1
                        logger.error(
                            f"Timeout processing vacancy '{card.get('title', '?')}' "
                            f"({card.get('url', '?')}) - took more than 3 minutes."
                        )
                        try:
                            await db.save_application(
                                vacancy_url=card.get("url", ""),
                                title=card.get("title", ""),
                                employer=card.get("employer", ""),
                                description="",
                                cover_letter="",
                                ai_relevance=0,
                                ai_analysis="",
                                status="error",
                                error_message="Превышен лимит ожидания обработки вакансии (3 минуты)",
                            )
                        except Exception as db_err:
                            logger.error(f"Could not save timed out application record: {db_err}")
                        
                        await self._notify(
                            f"⚠️ <b>Пропуск (Превышен таймаут):</b> <a href=\"{card.get('url', '')}\">{card.get('title', 'Без названия')}</a> @ {card.get('employer', 'Неизвестно')}\n"
                            f"<i>Процесс обработки вакансии завис и был принудительно остановлен через 3 минуты.</i>",
                            notify_type="error"
                        )
                        continue
                    except Exception as e:
                        session_total_processed += 1
                        session_error_count += 1
                        logger.error(
                            f"Failed processing vacancy '{card.get('title', '?')}' "
                            f"({card.get('url', '?')}): {e}",
                            exc_info=True,
                        )
                        try:
                            await db.save_application(
                                vacancy_url=card.get("url", ""),
                                title=card.get("title", ""),
                                employer=card.get("employer", ""),
                                description="",
                                cover_letter="",
                                ai_relevance=0,
                                ai_analysis="",
                                status="error",
                                error_message=str(e),
                            )
                        except Exception as db_err:
                            logger.error(f"Could not save failed application record: {db_err}")
                        
                        await self._notify(
                            f"❌ <b>Ошибка обработки вакансии:</b> <a href=\"{card.get('url', '')}\">{card.get('title', 'Без названия')}</a>\n"
                            f"⚠️ {e}",
                            notify_type="error"
                        )
                        continue
                    finally:
                        await vacancy_lock_manager.release(card.get("url", ""))

                if boundary_reached or self._check_stop():
                    break

                if page_num < config.max_pages - 1:
                    await anti_fraud.random_delay(is_page_change=True)

        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled")
        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)
            await self._notify(f"Error: {e}")
        finally:
            if flow_id and new_boundary_url:
                await db.set_setting(f"last_newest_vacancy_{flow_id}", new_boundary_url)
                logger.info(f"Saved new newest vacancy url boundary for flow {flow_id}: {new_boundary_url}")

            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            # Stop the browser to free memory and prevent leaks
            try:
                await browser_manager.stop()
                logger.info("Browser stopped at the end of scheduler run to free memory")
            except Exception as e:
                logger.error(f"Failed to stop browser: {e}")

            self._run_state = RunState.IDLE
            self.state = BotState()
            if self.name == "Monitoring":
                interval_mins = int(await db.get_setting("monitoring_interval", "30"))
                jitter_mins = int(await db.get_setting("monitoring_jitter", "0"))
                
                import random
                next_jitter_secs = random.randint(0, jitter_mins * 60) if jitter_mins > 0 else 0
                await db.set_setting("monitoring_next_jitter", str(next_jitter_secs))
                
                from datetime import datetime, timedelta, timezone
                import re
                
                prime_time = await db.get_setting("monitoring_prime_time", "24/7")
                tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))
                
                now_utc = datetime.now(timezone.utc)
                standard_next_run_utc = now_utc + timedelta(minutes=interval_mins, seconds=next_jitter_secs)
                
                actual_next_run_utc = standard_next_run_utc
                is_delayed_by_prime_time = False
                
                if prime_time != "24/7":
                    match = re.search(r"(\d{2}):\d{2}\s*-\s*(\d{2}):\d{2}", prime_time)
                    if match:
                        start_h = int(match.group(1))
                        end_h = int(match.group(2))
                        
                        user_next = standard_next_run_utc + timedelta(hours=tz_offset)
                        
                        def is_hour_inside(h: int) -> bool:
                            if start_h <= end_h:
                                return start_h <= h < end_h
                            else:
                                return h >= start_h or h < end_h
                        
                        if not is_hour_inside(user_next.hour):
                            is_delayed_by_prime_time = True
                            user_now = now_utc + timedelta(hours=tz_offset)
                            candidate = user_now.replace(hour=start_h, minute=0, second=0, microsecond=0)
                            if candidate <= user_now:
                                candidate += timedelta(days=1)
                            actual_next_run_utc = candidate - timedelta(hours=tz_offset)
                
                if is_delayed_by_prime_time:
                    diff = actual_next_run_utc - now_utc
                    diff_secs = int(diff.total_seconds())
                    diff_hours = diff_secs // 3600
                    diff_mins = (diff_secs % 3600) // 60
                    
                    next_run_str = ""
                    if diff_hours > 0:
                        next_run_str += f"{diff_hours} ч "
                    next_run_str += f"{diff_mins} мин"
                    
                    target_time_local = actual_next_run_utc + timedelta(hours=tz_offset)
                    next_run_str += f" (начало рабочего времени в {target_time_local.strftime('%H:%M')} по UTC{'+' if tz_offset >= 0 else ''}{tz_offset})"
                else:
                    next_run_str = f"{interval_mins} мин"
                    if next_jitter_secs > 0:
                        next_run_str += f" (+ {next_jitter_secs // 60}м {next_jitter_secs % 60}с рандом задержки)"
                
                if last_newest_url:
                    await self._notify(
                        format_monitoring_finished(
                            total=session_total_processed,
                            successful=session_success_count,
                            errors=session_error_count,
                            skipped=session_skipped_count,
                            next_run=next_run_str,
                        )
                    )
                else:
                    await self._notify(
                        f"⏱ Следующий запуск: <b>{next_run_str}</b>"
                    )
            else:
                stats = await db.get_today_stats()
                await self._notify(
                    format_session_finished(
                        total=stats["total_applied"],
                        successful=stats["successful"],
                        errors=stats["errors"],
                        skipped=stats["analyzed_skip"] + stats["skipped"],
                    )
                )

    def get_status_text(self) -> str:
        if self._run_state == RunState.RUNNING:
            return format_scheduler_status(
                run_state_name="▶️ Running",
                page=self.state.current_page,
                processed=self.state.vacancies_processed,
                applied_today=self.state.applied_today,
                captcha_detected=self.state.captcha_detected,
            )
        elif self._run_state == RunState.PAUSED:
            return format_scheduler_status(
                run_state_name="⏸ Paused",
                page=self.state.current_page,
                processed=self.state.vacancies_processed,
                applied_today=self.state.applied_today,
                captcha_detected=self.state.captcha_detected,
            )
        return "⏹ <b>Stopped</b>"

class _CaptchaPause(Exception):
    """Internal signal used to break out of the card loop on captcha."""
    pass


manual_scheduler = Scheduler(name="Manual")
monitoring_scheduler = Scheduler(name="Monitoring")

monitoring_daemon_task: Optional[asyncio.Task] = None


async def monitoring_daemon_loop() -> None:
    logger.info("Background monitoring daemon loop started")
    while True:
        try:
            await asyncio.sleep(60)  # check conditions every 1 minute
            enabled = (await db.get_setting("monitoring_mode", "false")) == "true"
            if not enabled:
                continue
            
            if monitoring_scheduler._run_state == RunState.IDLE:
                settings = get_settings()
                if not settings.session_file.exists():
                    continue
                
                flow = await get_active_flow()
                if not flow:
                    continue

                last_newest_url = await db.get_setting(f"last_newest_vacancy_{flow.id}", "")

                # 1. Check Prime Time and Timezone
                prime_time = await db.get_setting("monitoring_prime_time", "24/7")
                if prime_time != "24/7" and last_newest_url:
                    tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))
                    # Calculate current hour in user's timezone
                    from datetime import datetime, timedelta, timezone
                    import re
                    user_time = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
                    current_hour = user_time.hour
                    
                    # Parse prime_time format, e.g. "08:00 - 20:00"
                    match = re.search(r"(\d{2}):\d{2}\s*-\s*(\d{2}):\d{2}", prime_time)
                    if match:
                        start_h = int(match.group(1))
                        end_h = int(match.group(2))
                        # Check if inside range (handles overnight intervals like 22:00 - 06:00 too!)
                        is_inside = False
                        if start_h <= end_h:
                            is_inside = start_h <= current_hour < end_h
                        else:
                            is_inside = current_hour >= start_h or current_hour < end_h
                            
                        if not is_inside:
                            logger.info(f"Monitoring skipped: current hour {current_hour:02d}:00 is outside prime time ({prime_time})")
                            continue

                # 2. Handle Jitter (Random delay before starting)
                # Skip Jitter delay entirely if there is no baseline set yet (so baseline is established immediately)
                jitter = int(await db.get_setting("monitoring_jitter", "0"))
                if jitter > 0 and last_newest_url:
                    saved_jitter = await db.get_setting("monitoring_next_jitter", "")
                    if saved_jitter:
                        delay_secs = int(saved_jitter)
                        # Clear it so it is not reused next time
                        await db.set_setting("monitoring_next_jitter", "")
                    else:
                        import random
                        delay_secs = random.randint(0, jitter * 60)
                    logger.info(f"Jitter delay: sleeping for {delay_secs} seconds before starting monitoring run")
                    # Check enabled status while sleeping in jitter
                    for _ in range(delay_secs // 5):
                        await asyncio.sleep(5)
                        enabled = (await db.get_setting("monitoring_mode", "false")) == "true"
                        if not enabled:
                            break
                    if not enabled:
                        continue
                
                logger.info("Monitoring daemon: triggering active flow apply")
                await monitoring_scheduler.start()
                
                # Wait for it to complete
                while monitoring_scheduler._run_state == RunState.RUNNING:
                    await asyncio.sleep(5)
                
                interval_mins = int(await db.get_setting("monitoring_interval", "30"))
                logger.info(f"Monitoring daemon run completed. Sleeping for {interval_mins} minutes.")
                # Loop interval_mins * 6 times with 10s sleep to check enabling status frequently
                for _ in range(interval_mins * 6):
                    await asyncio.sleep(10)
                    enabled = (await db.get_setting("monitoring_mode", "false")) == "true"
                    if not enabled:
                        break
        except Exception as e:
            logger.error(f"Error in monitoring daemon loop: {e}", exc_info=True)
            await asyncio.sleep(60)


def set_notify_callback(callback: Callable[[str], Awaitable[None]]) -> None:
    global monitoring_daemon_task
    manual_scheduler.set_notify_callback(callback)
    monitoring_scheduler.set_notify_callback(callback)
    if monitoring_daemon_task is None:
        monitoring_daemon_task = asyncio.create_task(monitoring_daemon_loop())

import asyncio
import logging
from enum import Enum
from typing import Optional, Callable, Awaitable

from .config import get_settings
from .models import BotState, ApplyStatus, Vacancy
from . import database as db
from .services.anti_fraud import AntiFraud
from .services.hh_search import search_service
from .services.hh_apply import apply_service
from .services.gemini import gemini_service
from .services.flow_entity import FlowConfig, get_active_flow, update_flow, get_active_flow_id
from .services.browser import browser_manager
from .services.hh_auth import hh_auth
from .bot.formatting import format_apply_success, format_session_finished, format_scheduler_status

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


class RunState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class Scheduler:
    def __init__(self) -> None:
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

        self._stop_event.clear()
        self._resume_event.set()
        self._run_state = RunState.RUNNING
        self.state = BotState(is_running=True)
        self._task = asyncio.create_task(self._run_loop(flow.config))
        await self._notify(f"▶️ <b>Auto-apply started</b>: <i>{flow.name}</i>")

    async def stop(self) -> None:
        if self._run_state == RunState.IDLE:
            await self._notify("⚠️ Not running!")
            return

        self._stop_event.set()
        self._resume_event.set()
        self._run_state = RunState.IDLE

        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None

        self.state = BotState()
        await self._notify("⏹ <b>Auto-apply stopped</b>")

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
                f"<i>{analysis_result.summary}</i>",
                notify_type="info"
            )
        else:
            await self._notify(
                f"⏭ <b>Skipped</b> (Relevance={analysis_result.relevance}/10): <a href=\"{vacancy.url}\">{vacancy.title}</a> @ {vacancy.employer}\n"
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

        if not resume_text and config.resume_id:
            logger.info(f"Resume text not cached, fetching for resume_id={config.resume_id}")
            resume_text = await self._run_interruptible(hh_auth.get_resume_text(config.resume_id))
            if resume_text:
                config.resume_text = resume_text
                flow_id = await get_active_flow_id()
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

            for page_num in range(config.max_pages):
                if self._check_stop():
                    break

                await self._check_pause()
                if self._check_stop():
                    break

                allowed, reason = await anti_fraud.check_rate_limits()
                if not allowed:
                    await self._notify(f"🛑 Stopping: {reason}", notify_type="error")
                    break

                await self._notify(f"🔍 Searching page <b>{page_num + 1}/{config.max_pages}</b>...")
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
                    if context:
                        await context.close()
                        context = None
                    continue

                await self._notify(f"📋 Found <b>{len(cards)}</b> vacancies on page {page_num + 1}")

                for i, card in enumerate(cards):
                    if self._check_stop():
                        break

                    await self._check_pause()
                    if self._check_stop():
                        break

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
                        await asyncio.wait_for(
                            self._process_card(
                                card, page, anti_fraud, resume_text, config, i, len(cards)
                            ),
                            timeout=180.0
                        )
                    except _CaptchaPause:
                        break
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
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

                if self._check_stop():
                    break

                if page_num < config.max_pages - 1:
                    await anti_fraud.random_delay(is_page_change=True)

        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled")
        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)
            await self._notify(f"Error: {e}")
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            self._run_state = RunState.IDLE
            self.state = BotState()
            stats = await db.get_today_stats()
            await self._notify(
                format_session_finished(
                    total=stats["total_applied"],
                    successful=stats["successful"],
                    errors=stats["errors"],
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


scheduler = Scheduler()

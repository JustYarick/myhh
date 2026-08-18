import asyncio
import logging
from enum import Enum
from typing import Optional, Callable, Awaitable

from ..config import get_settings
from ..models import BotState, ApplyStatus, Vacancy
from .. import database as db
from ..database import extract_vacancy_id
from ..services.anti_fraud import AntiFraud
from ..services.hh_search import search_service, prepare_search_url
from ..services.hh_apply import apply_service
from ..services.gemini import gemini_service
from ..services.flow_entity import FlowConfig, get_active_flow, update_flow, get_active_flow_id
from ..services.hh_api_client import hh_api
from ..bot.formatting import format_apply_success, format_session_finished, format_scheduler_status, format_monitoring_finished

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
            await self._notify("⚠️ Уже запущен!")
            return

        # Check that HH API token is available
        await hh_api.load_token()
        if not hh_api.is_authenticated:
            await self._notify(
                "❌ HH API токен не найден.\n"
                "Перейдите в <b>Настройки → 📱 Авторизация HH (API)</b> и пройдите авторизацию.",
            )
            return

        flow = await get_active_flow()
        if not flow:
            await self._notify("❌ Нет активного потока. Создайте и активируйте поток в меню «📂 Потоки».")
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
            await self._notify("⚠️ Планировщик не запущен!")
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
            await self._notify("⚠️ Нет запущенной сессии для паузы.")
            return

        self._run_state = RunState.PAUSED
        self._resume_event.clear()
        self.state.paused = True
        await self._notify("⏸ <b>Сессия на паузе</b>")

    async def resume(self) -> None:
        if self._run_state != RunState.PAUSED:
            await self._notify("⚠️ Сессия не на паузе.")
            return

        self._run_state = RunState.RUNNING
        self._resume_event.set()
        self.state.paused = False
        await self._notify("▶️ <b>Сессия продолжена</b>")

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

        # Check blacklist (exclude_employers)
        exclude_list = config.exclude_employers
        if exclude_list:
            employer_name = card.get("employer", "").strip()
            if employer_name:
                blacklist_companies = [c.strip().lower() for c in exclude_list.split(",") if c.strip()]
                lower_employer = employer_name.lower()
                matched_blacklisted = None
                for blacklisted in blacklist_companies:
                    if blacklisted in lower_employer:
                        matched_blacklisted = blacklisted
                        break
                
                if matched_blacklisted:
                    logger.info(f"Skipping vacancy '{card['title']}' - employer '{employer_name}' is blacklisted (matches '{matched_blacklisted}')")
                    await db.cache_vacancy_result(
                        vacancy_url=card["url"],
                        title=card["title"],
                        employer=employer_name,
                        ai_relevance=0,
                        ai_summary=f"Черный список: {matched_blacklisted}",
                        result="analyzed_skip"
                    )
                    await db.save_application(
                        vacancy_url=card["url"],
                        title=card["title"],
                        employer=employer_name,
                        description="",
                        cover_letter="",
                        ai_relevance=0,
                        ai_analysis=f"Черный список: {matched_blacklisted}",
                        status="analyzed_skip",
                        error_message=f"Компания в черном списке ({matched_blacklisted})"
                    )
                    await self._notify(
                        f"⏭ <b>Пропущено (Черный список компаний):</b> <a href=\"{card['url']}\">{card['title']}</a> @ {employer_name}\n"
                        f"<i>Компания совпадает с маской: '{matched_blacklisted}'</i>",
                        notify_type="info"
                    )
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
                from ..models import VacancyAnalysis
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
                        custom_rules=config.custom_rules,
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
                from ..models.vacancy import VacancyAnalysis
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
                    custom_rules=config.custom_rules,
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
            stop_words = ["error", "fail", "ошибка", "gemini", "api key", "исключение", "prompt", "failed"]
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

        is_questions_skip = (
            result.status == ApplyStatus.ANALYZED_SKIP
            and result.message == "requires_questions"
        )

        if not is_questions_skip:
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
                "🔒 <b>Обнаружена капча при отклике!</b> Сессия поставлена на паузу.\n"
                "Решите капчу на сайте HH.ru (в браузере), затем нажмите «▶️ Продолжить» в меню.",
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

        if not resume_text and config.resume_id:
            logger.info(f"Resume text not cached, fetching for resume_id={config.resume_id}")
            try:
                from ..services.hh_resume import resume_service
                resume_text = await self._run_interruptible(resume_service.fetch_resume_text(config.resume_id))
            except Exception as e:
                logger.warning(f"Failed to fetch resume text: {e}")
                resume_text = ""
            if resume_text:
                config.resume_text = resume_text
                if flow_id:
                    await update_flow(flow_id, config=config)
                logger.info(f"Resume text fetched and cached: {len(resume_text)} chars")
            else:
                logger.warning("Failed to fetch resume text, proceeding without it")

        logger.info(
            f"Starting run loop [{self.name}]: search_url={config.search_url[:60] if config.search_url else ''}, "
            f"resume={'loaded' if resume_text else 'none'}"
        )

        session_success_count = 0
        session_skipped_count = 0
        session_error_count = 0
        session_total_processed = 0
        new_boundary_url = None

        try:
            if self.name == "Monitoring":
                from .strategies import MonitoringRunStrategy
                strategy = MonitoringRunStrategy()
            else:
                from .strategies import ManualRunStrategy
                strategy = ManualRunStrategy()

            new_boundary_url = await strategy.execute(
                self, config, anti_fraud, resume_text, flow_id,
                session_success_count, session_skipped_count,
                session_error_count, session_total_processed,
            )

        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled")
        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)
            await self._notify(f"Error: {e}")
        finally:
            if flow_id and new_boundary_url:
                await db.set_setting(f"last_newest_vacancy_{flow_id}", new_boundary_url)
                logger.info(f"Saved boundary for flow {flow_id}: {new_boundary_url}")

            self._run_state = RunState.IDLE
            self.state = BotState()
            if self.name == "Monitoring":
                from datetime import datetime, timezone
                next_run_str = "по расписанию"
                next_run_db = await db.get_setting("monitoring_next_run", "")
                if next_run_db:
                    try:
                        next_run_dt = datetime.fromisoformat(next_run_db)
                        now_utc = datetime.now(timezone.utc)
                        if next_run_dt > now_utc:
                            diff_secs = int((next_run_dt - now_utc).total_seconds())
                            diff_hours = diff_secs // 3600
                            diff_mins = (diff_secs % 3600) // 60
                            next_run_str = ""
                            if diff_hours > 0:
                                next_run_str += f"{diff_hours} ч "
                            next_run_str += f"{diff_mins} мин"
                    except Exception:
                        pass
                last_newest_url = await db.get_setting(f"last_newest_vacancy_{flow_id}", "") if flow_id else ""
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
                    await self._notify(f"⏱ Следующий запуск: <b>{next_run_str}</b>")
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

    async def _save_error_record(self, card: dict, error_message: str) -> None:
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
                error_message=error_message,
            )
        except Exception as db_err:
            logger.error(f"Could not save error record: {db_err}")

    def get_status_text(self) -> str:
        if self._run_state == RunState.RUNNING:
            return format_scheduler_status(
                run_state_name="▶️ Запущен",
                page=self.state.current_page,
                processed=self.state.vacancies_processed,
                applied_today=self.state.applied_today,
                captcha_detected=self.state.captcha_detected,
            )
        elif self._run_state == RunState.PAUSED:
            return format_scheduler_status(
                run_state_name="⏸ На паузе",
                page=self.state.current_page,
                processed=self.state.vacancies_processed,
                applied_today=self.state.applied_today,
                captcha_detected=self.state.captcha_detected,
            )
        return "⏹ <b>Остановлен</b>"

class _CaptchaPause(Exception):
    """Internal signal used to break out of the card loop on captcha."""
    pass


vacancy_lock_manager = VacancyLockManager()
manual_scheduler = Scheduler(name="Manual")
monitoring_scheduler = Scheduler(name="Monitoring")

import asyncio
import logging
from typing import Optional, Callable, Awaitable

from .config import get_settings
from .models import BotState, ApplyStatus
from . import database as db
from .services.anti_fraud import AntiFraud
from .services.hh_search import search_service
from .services.hh_apply import apply_service
from .services.gemini import gemini_service
from .services.flow_entity import FlowConfig, get_active_flow

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self.state = BotState()
        self._task: Optional[asyncio.Task] = None
        self._notify_callback: Optional[Callable[[str], Awaitable[None]]] = None

    def set_notify_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self._notify_callback = callback

    async def _notify(self, message: str) -> None:
        logger.info(f"[NOTIFY] {message}")
        if self._notify_callback:
            try:
                await self._notify_callback(message)
            except Exception as e:
                logger.error(f"Notification failed: {e}")

    async def start(self) -> None:
        if self.state.is_running:
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

        self.state = BotState(is_running=True)
        self._task = asyncio.create_task(self._run_loop(flow.config))
        await self._notify(f"▶️ <b>Auto-apply started</b>: <i>{flow.name}</i>")

    async def stop(self) -> None:
        if not self.state.is_running:
            await self._notify("⚠️ Not running!")
            return

        self.state.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._notify("⏹ <b>Auto-apply stopped</b>")

    async def pause(self) -> None:
        self.state.paused = True
        await self._notify("⏸ <b>Paused</b>")

    async def resume(self) -> None:
        self.state.paused = False
        await self._notify("▶️ <b>Resumed</b>")

    async def _run_loop(self, config: FlowConfig) -> None:
        anti_fraud = AntiFraud(config)
        resume_text = config.resume_text

        if not resume_text and config.resume_id:
            logger.info(f"Resume text not cached, fetching for resume_id={config.resume_id}")
            from .services.hh_auth import hh_auth
            resume_text = await hh_auth.get_resume_text(config.resume_id)
            if resume_text:
                config.resume_text = resume_text
                from .services.flow_entity import update_flow
                from .services.flow_entity import get_active_flow_id
                flow_id = await get_active_flow_id()
                if flow_id:
                    await update_flow(flow_id, config=config)
                logger.info(f"Resume text fetched and cached: {len(resume_text)} chars")
            else:
                logger.warning("Failed to fetch resume text, proceeding without it")

        logger.info(f"Starting run loop: max_pages={config.max_pages}, "
                     f"search_url={config.search_url[:60] if config.search_url else ''}, "
                     f"resume={'loaded' if resume_text else 'none'}")

        try:
            for page_num in range(config.max_pages):
                if not self.state.is_running:
                    break

                while self.state.paused and self.state.is_running:
                    await asyncio.sleep(5)

                if not self.state.is_running:
                    break

                allowed, reason = await anti_fraud.check_rate_limits()
                if not allowed:
                    await self._notify(f"🛑 Stopping: {reason}")
                    break

                await self._notify(f"🔍 Searching page <b>{page_num + 1}/{config.max_pages}</b>...")

                try:
                    vacancies = await search_service.search(
                        query="",
                        area_code="",
                        anti_fraud=anti_fraud,
                        page_num=page_num,
                        url=config.search_url,
                    )
                except RuntimeError as e:
                    if "captcha" in str(e).lower():
                        anti_fraud.captcha_detected()
                        await self._notify(
                            "🔒 <b>Captcha detected!</b> Pausing. Wait and try /resume later."
                        )
                        self.state.captcha_detected = True
                        self.state.paused = True
                        continue
                    raise

                if not vacancies:
                    await self._notify(f"📭 No vacancies found on page {page_num + 1}")
                    continue

                await self._notify(f"📋 Found <b>{len(vacancies)}</b> vacancies on page {page_num + 1}")

                for i, vacancy in enumerate(vacancies):
                    if not self.state.is_running:
                        break

                    while self.state.paused and self.state.is_running:
                        await asyncio.sleep(5)

                    if not self.state.is_running:
                        break

                    allowed, reason = await anti_fraud.check_rate_limits()
                    if not allowed:
                        await self._notify(f"🛑 Stopping: {reason}")
                        self.state.is_running = False
                        break

                    if await db.was_vacancy_applied(vacancy.url):
                        logger.debug(f"Already applied: {vacancy.title}")
                        continue

                    logger.debug(f"Analyzing: {vacancy.title} @ {vacancy.employer}")
                    analysis_result = None
                    try:
                        analysis_result = await gemini_service.analyze_vacancy(
                            vacancy.model_dump(),
                            prompt_template=config.analysis_prompt,
                            resume_text=resume_text,
                        )
                    except Exception as e:
                        logger.error(f"Analysis failed: {e}")

                    if analysis_result and not analysis_result.apply:
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
                        continue

                    cover_letter = ""
                    try:
                        cover_letter = await gemini_service.generate_cover_letter(
                            vacancy.model_dump(),
                            prompt_template=config.cover_letter_prompt,
                            resume_text=resume_text,
                        )
                    except Exception as e:
                        logger.error(f"Cover letter generation failed: {e}")

                    await anti_fraud.random_delay()
                    result = await apply_service.apply(
                        vacancy.url, cover_letter,
                        af=anti_fraud, resume_id=config.resume_id,
                    )

                    await db.save_application(
                        vacancy_url=vacancy.url,
                        title=vacancy.title,
                        employer=vacancy.employer,
                        description=vacancy.description[:500],
                        cover_letter=cover_letter,
                        ai_relevance=(
                            analysis_result.relevance if analysis_result else 0
                        ),
                        ai_analysis=(
                            analysis_result.summary if analysis_result else ""
                        ),
                        status=result.status.value,
                        error_message=result.message,
                    )

                    self.state.vacancies_processed += 1

                    if result.status == ApplyStatus.SUCCESS:
                        self.state.applied_today += 1
                        logger.info(f"Applied: {vacancy.title}")
                    elif result.status == ApplyStatus.CAPTCHA:
                        self.state.captcha_detected = True
                        self.state.paused = True
                        await self._notify(
                            "🔒 <b>Captcha on apply!</b> Paused. Use /resume later."
                        )
                        break
                    else:
                        logger.info(
                            f"Apply result: {result.status.value} - {result.message} "
                            f"for {vacancy.title}"
                        )

                if page_num < config.max_pages - 1 and self.state.is_running:
                    await anti_fraud.random_delay(is_page_change=True)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)
            await self._notify(f"Error: {e}")
        finally:
            self.state.is_running = False
            stats = await db.get_today_stats()
            await self._notify(
                f"📊 <b>Session finished</b>\n"
                f"Today: <b>{stats['total_applied']}</b> total, "
                f"✅ {stats['successful']} success, "
                f"❌ {stats['errors']} errors"
            )

    def get_status_text(self) -> str:
        if self.state.is_running:
            status = "▶️ Running" if not self.state.paused else "⏸ Paused"
            return (
                f"<b>{status}</b>\n"
                f"Page: {self.state.current_page}\n"
                f"Processed: {self.state.vacancies_processed}\n"
                f"Applied today: {self.state.applied_today}\n"
                f"Captcha: {'⚠️ Yes' if self.state.captcha_detected else '✅ No'}"
            )
        return "⏹ <b>Stopped</b>"


scheduler = Scheduler()

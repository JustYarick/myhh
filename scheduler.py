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
from .services.flow_entity import FlowConfig, get_active_flow

logger = logging.getLogger(__name__)


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

    async def _notify(self, message: str) -> None:
        logger.info(f"[NOTIFY] {message}")
        if self._notify_callback:
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
        description = await search_service.get_vacancy_description(page, card["url"])
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
            analysis_result = await gemini_service.analyze_vacancy(
                vacancy.model_dump(),
                prompt_template=config.analysis_prompt,
                resume_text=resume_text,
            )
        except Exception as e:
            logger.error(f"Analysis failed: {e}")

        if analysis_result and analysis_result.relevance < 4:
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
            return

        if self._check_stop():
            return

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
            existing_page=page,
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

        if result.status == ApplyStatus.SUCCESS:
            self.state.applied_today += 1
            logger.info(f"Applied: {vacancy.title}")
            relevance_str = f" | {analysis_result.relevance}/10" if analysis_result else ""
            summary_str = analysis_result.summary[:100] if analysis_result and analysis_result.summary else ""
            cover_preview = cover_letter[:200].replace("\n", " ") if cover_letter else "—"
            await self._notify(
                f"✅ <b>Applied</b>: <a href=\"{vacancy.url}\">{vacancy.title}</a>\n"
                f"🏢 {vacancy.employer}{relevance_str}\n"
                f"💬 {summary_str}\n"
                f"📝 {cover_preview}"
            )
        elif result.status == ApplyStatus.CAPTCHA:
            self.state.captcha_detected = True
            self._run_state = RunState.PAUSED
            self._resume_event.clear()
            self.state.paused = True
            await self._notify(
                "🔒 <b>Captcha on apply!</b> Paused. Resume when ready."
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

        context = None
        page = None
        try:
            from .services.browser import browser_manager
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
                    await self._notify(f"🛑 Stopping: {reason}")
                    break

                await self._notify(f"🔍 Searching page <b>{page_num + 1}/{config.max_pages}</b>...")
                self.state.current_page = page_num + 1

                # A search failure (timeout, layout change, network blip)
                # must skip this page, not kill the whole run.
                try:
                    page, cards, new_ctx = await search_service.search_cards(
                        anti_fraud=anti_fraud,
                        page_num=page_num,
                        url=config.search_url,
                        existing_page=page,
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
                            "🔒 <b>Captcha detected!</b> Pausing. Wait and resume."
                        )
                        continue
                    logger.error(f"Search failed on page {page_num + 1}: {e}", exc_info=True)
                    await self._notify(f"⚠️ Search error on page {page_num + 1}, skipping: {e}")
                    continue
                except Exception as e:
                    logger.error(f"Unexpected search error on page {page_num + 1}: {e}", exc_info=True)
                    await self._notify(f"⚠️ Unexpected search error on page {page_num + 1}, skipping: {e}")
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
                        await self._notify(f"🛑 Stopping: {reason}")
                        self._stop_event.set()
                        break

                    # Each vacancy gets its own safety net. Whatever goes
                    # wrong inside (AI call, apply, DB write) is logged and
                    # we move on to the next card - it never takes down the
                    # whole run anymore.
                    try:
                        await self._process_card(
                            card, page, anti_fraud, resume_text, config, i, len(cards)
                        )
                    except _CaptchaPause:
                        break
                    except asyncio.CancelledError:
                        raise
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
                f"📊 <b>Session finished</b>\n"
                f"Today: <b>{stats['total_applied']}</b> total, "
                f"✅ {stats['successful']} success, "
                f"❌ {stats['errors']} errors"
            )

    def get_status_text(self) -> str:
        if self._run_state == RunState.RUNNING:
            return (
                f"<b>▶️ Running</b>\n"
                f"Page: {self.state.current_page}\n"
                f"Processed: {self.state.vacancies_processed}\n"
                f"Applied today: {self.state.applied_today}\n"
                f"Captcha: {'⚠️ Yes' if self.state.captcha_detected else '✅ No'}"
            )
        elif self._run_state == RunState.PAUSED:
            return (
                f"<b>⏸ Paused</b>\n"
                f"Page: {self.state.current_page}\n"
                f"Processed: {self.state.vacancies_processed}\n"
                f"Applied today: {self.state.applied_today}\n"
                f"Captcha: {'⚠️ Yes' if self.state.captcha_detected else '✅ No'}"
            )
        return "⏹ <b>Stopped</b>"


class _CaptchaPause(Exception):
    """Internal signal used to break out of the card loop on captcha."""
    pass


scheduler = Scheduler()

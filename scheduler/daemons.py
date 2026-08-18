import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from .. import database as db
from ..services.flow_entity import get_active_flow
from ..services.hh_resume import resume_service
from .daemon import BackgroundDaemon
from .runner import monitoring_scheduler, RunState
from .time_helpers import calculate_next_wait_seconds, is_within_prime_time, get_resumed_sleep_time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Concrete daemon: vacancy monitoring
# ---------------------------------------------------------------------------

class MonitoringDaemonService(BackgroundDaemon):
    """Periodically triggers the monitoring Scheduler to scan for new vacancies."""

    async def _run(self) -> None:
        logger.info("Monitoring daemon started")
        try:
            wait_secs = await get_resumed_sleep_time("monitoring_next_run")
            if wait_secs is not None:
                logger.info(f"Monitoring daemon: resuming sleep for {wait_secs}s from previous session")
                await self.sleep(wait_secs)

            while True:
                total_wait = await calculate_next_wait_seconds(
                    "monitoring_interval", "30",
                    "monitoring_jitter", "0",
                    prime_time_key="monitoring_prime_time"
                )
                next_run_dt = datetime.now(timezone.utc) + timedelta(seconds=total_wait)
                await db.set_setting("monitoring_next_run", next_run_dt.isoformat())

                if monitoring_scheduler._run_state == RunState.IDLE:
                    flow = await get_active_flow()
                    if flow:
                        logger.info("Monitoring daemon: triggering active flow apply")
                        await monitoring_scheduler.start()
                        while monitoring_scheduler._run_state == RunState.RUNNING:
                            await asyncio.sleep(5)

                now = datetime.now(timezone.utc)
                if next_run_dt > now:
                    sleep_secs = (next_run_dt - now).total_seconds()
                    logger.info(f"Monitoring daemon: sleeping {sleep_secs}s until next run")
                    triggered = await self.sleep(sleep_secs)
                    if triggered:
                        logger.info("Monitoring daemon: woken up by trigger")
                        await db.set_setting("monitoring_next_run", "")

        except asyncio.CancelledError:
            logger.info("Monitoring daemon cancelled")
            raise
        except Exception as e:
            logger.error(f"Monitoring daemon error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Concrete daemon: resume auto-raise
# ---------------------------------------------------------------------------

class ResumeUpdaterDaemonService(BackgroundDaemon):
    """Periodically raises the active resume on HH.ru."""

    async def _run(self) -> None:
        logger.info("Resume updater daemon started")
        try:
            settings = get_settings()
            
            wait_secs = await get_resumed_sleep_time("resume_update_next_run")
            if wait_secs is not None:
                logger.info(f"Resume updater daemon: resuming sleep for {wait_secs}s from previous session")
                await self.sleep(wait_secs)

            while True:
                total_wait = await calculate_next_wait_seconds(
                    "resume_update_interval", "240",
                    "resume_update_jitter", "15",
                    min_interval_mins=240,
                    prime_time_key="resume_update_prime_time"
                )
                next_run_dt = datetime.now(timezone.utc) + timedelta(seconds=total_wait)
                await db.set_setting("resume_update_next_run", next_run_dt.isoformat())

                # Prime-time guard for immediate execution
                if not await is_within_prime_time("resume_update_prime_time", "24/7"):
                    now = datetime.now(timezone.utc)
                    if next_run_dt > now:
                        sleep_secs = (next_run_dt - now).total_seconds()
                        await self.sleep(sleep_secs)
                    continue

                flow = await get_active_flow()
                from ..services.hh_api_client import hh_api
                if hh_api.is_authenticated and flow and flow.config.resume_id:
                    await self._raise_resume(flow.config.resume_id)

                now = datetime.now(timezone.utc)
                if next_run_dt > now:
                    sleep_secs = (next_run_dt - now).total_seconds()
                    logger.info(f"Resume updater daemon: sleeping {sleep_secs}s until next run")
                    triggered = await self.sleep(sleep_secs)
                    if triggered:
                        logger.info("Resume updater daemon: woken up by trigger")
                        await db.set_setting("resume_update_next_run", "")

        except asyncio.CancelledError:
            logger.info("Resume updater daemon cancelled")
            raise
        except Exception as e:
            logger.error(f"Resume updater daemon error: {e}", exc_info=True)

    async def _raise_resume(self, resume_id: str) -> None:
        """Perform a single publish_resume call and send the appropriate notification."""
        from datetime import datetime

        logger.info(f"Resume updater: raising resume {resume_id}")
        if monitoring_scheduler._notify_callback:
            await monitoring_scheduler._notify(
                f"⏳ Начинаю автоматическое поднятие резюме <code>{resume_id}</code>..."
            )

        success, msg = await resume_service.publish_resume(resume_id)

        if success:
            await db.set_setting(
                f"last_resume_update_time_{resume_id}", datetime.now().isoformat()
            )
            logger.info(f"Resume {resume_id} raised: {msg}")
            await monitoring_scheduler._notify(f"✅ Резюме успешно поднято: {msg}", "success")
        elif "слишком рано" in msg.lower() or "заблокирована" in msg.lower():
            await db.set_setting(
                f"last_resume_update_time_{resume_id}", datetime.now().isoformat()
            )
            logger.info(f"Resume {resume_id} already raised: {msg}")
            await monitoring_scheduler._notify(f"ℹ️ Резюме: {msg}", "info")
        else:
            logger.warning(f"Failed to raise resume {resume_id}: {msg}")
            await monitoring_scheduler._notify(
                f"⚠️ Ошибка автоподнятия резюме: {msg}", "error"
            )


# ---------------------------------------------------------------------------
# Module-level singleton daemon instances
# ---------------------------------------------------------------------------

monitoring_daemon = MonitoringDaemonService("Monitoring")
resume_updater_daemon = ResumeUpdaterDaemonService("ResumeUpdater")

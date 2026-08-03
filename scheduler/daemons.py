import asyncio
import logging

from ..config import get_settings
from .. import database as db
from ..services.flow_entity import get_active_flow
from ..services.hh_auth import hh_auth
from .daemon import BackgroundDaemon
from .runner import monitoring_scheduler, RunState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Concrete daemon: vacancy monitoring
# ---------------------------------------------------------------------------

class MonitoringDaemonService(BackgroundDaemon):
    """Periodically triggers the monitoring Scheduler to scan for new vacancies."""

    async def _run(self) -> None:
        import random

        logger.info("Monitoring daemon started")
        try:
            # First iteration: run IMMEDIATELY on startup / re-enable
            if monitoring_scheduler._run_state == RunState.IDLE:
                flow = await get_active_flow()
                if flow:
                    logger.info("Monitoring daemon: first run — triggering immediately")
                    await monitoring_scheduler.start()
                    while monitoring_scheduler._run_state == RunState.RUNNING:
                        await asyncio.sleep(5)

            while True:
                interval_mins = int(await db.get_setting("monitoring_interval", "30"))
                jitter = int(await db.get_setting("monitoring_jitter", "0"))
                jitter_secs = random.randint(0, jitter * 60) if jitter > 0 else 0
                total_wait = (interval_mins * 60) + jitter_secs

                logger.info(f"Monitoring daemon: sleeping {total_wait}s until next run")
                triggered = await self.sleep(total_wait)
                if triggered:
                    logger.info("Monitoring daemon: woken up by trigger")



                if monitoring_scheduler._run_state == RunState.IDLE:
                    flow = await get_active_flow()
                    if flow:
                        logger.info("Monitoring daemon: triggering active flow apply")
                        await monitoring_scheduler.start()
                        while monitoring_scheduler._run_state == RunState.RUNNING:
                            await asyncio.sleep(5)

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
        import re
        import random
        from datetime import datetime, timedelta, timezone

        logger.info("Resume updater daemon started")
        try:
            # First iteration: run IMMEDIATELY on startup / re-enable
            settings = get_settings()
            flow = await get_active_flow()
            if settings.session_file.exists() and flow and flow.config.resume_id:
                await self._raise_resume(flow.config.resume_id)

            while True:
                interval_mins = int(await db.get_setting("resume_update_interval", "240"))
                interval_mins = max(interval_mins, 240)  # minimum 4 h

                jitter = int(await db.get_setting("resume_update_jitter", "15"))
                jitter_secs = random.randint(0, jitter * 60) if jitter > 0 else 0
                total_wait = (interval_mins * 60) + jitter_secs

                logger.info(f"Resume updater daemon: sleeping {total_wait}s until next run")
                triggered = await self.sleep(total_wait)
                if triggered:
                    logger.info("Resume updater daemon: woken up by trigger")

                # Prime-time guard
                prime_time = await db.get_setting("resume_update_prime_time", "24/7")
                if prime_time != "24/7":
                    tz_offset = int(await db.get_setting("monitoring_timezone_offset", "3"))
                    user_time = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
                    current_hour = user_time.hour

                    match = re.search(r"(\d{2}):\d{2}\s*-\s*(\d{2}):\d{2}", prime_time)
                    if match:
                        start_h, end_h = int(match.group(1)), int(match.group(2))
                        inside = (
                            start_h <= current_hour < end_h
                            if start_h <= end_h
                            else current_hour >= start_h or current_hour < end_h
                        )
                        if not inside:
                            logger.info(
                                f"Resume updater: hour {current_hour:02d}:00 "
                                f"outside prime time ({prime_time}), skipping"
                            )
                            continue

                flow = await get_active_flow()
                if flow and flow.config.resume_id:
                    await self._raise_resume(flow.config.resume_id)

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

        success, msg = await hh_auth.publish_resume(resume_id)

        if success:
            await db.set_setting(
                f"last_resume_update_time_{resume_id}", datetime.now().isoformat()
            )
            logger.info(f"Resume {resume_id} raised: {msg}")
            await monitoring_scheduler._notify(f"✅ Резюме успешно поднято: {msg}", "success")
        elif "уже поднято" in msg.lower() or "заблокирована" in msg.lower():
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

"""
autohh.scheduler — public API package.

All names previously importable from ``autohh.scheduler`` (the old monolith)
are re-exported here so that no external import site needs to change.
"""

import asyncio
import logging
from typing import Callable, Awaitable, Optional

from .. import database as db
from ..services.notification_service import set_text_callback, set_photo_callback  # noqa: F401

from .runner import (  # noqa: F401
    VacancyLockManager,
    RunState,
    Scheduler,
    vacancy_lock_manager,
    manual_scheduler,
    monitoring_scheduler,
    check_requires_test,
)
from .daemon import BackgroundDaemon  # noqa: F401
from .daemons import (  # noqa: F401
    MonitoringDaemonService,
    ResumeUpdaterDaemonService,
    monitoring_daemon,
    resume_updater_daemon,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backward-compatible free-function API (thin delegates)
# These keep all existing handler imports working without changes.
# ---------------------------------------------------------------------------

def trigger_monitoring() -> None:
    """Wake up the monitoring daemon immediately."""
    monitoring_daemon.trigger()


def trigger_resume_update() -> None:
    """Wake up the resume updater daemon immediately."""
    resume_updater_daemon.trigger()


async def start_monitoring_daemon() -> None:
    await monitoring_daemon.start()


async def stop_monitoring_daemon() -> None:
    await monitoring_daemon.stop()


async def start_resume_updater_daemon() -> None:
    await resume_updater_daemon.start()


async def stop_resume_updater_daemon() -> None:
    await resume_updater_daemon.stop()


# ---------------------------------------------------------------------------
# Notification wiring + startup recovery
# ---------------------------------------------------------------------------

def set_notify_callback(
    callback: Callable[[str], Awaitable[None]],
    photo_callback: Optional[Callable[[bytes, str], Awaitable[None]]] = None,
) -> None:
    manual_scheduler.set_notify_callback(callback)
    monitoring_scheduler.set_notify_callback(callback)
    set_text_callback(callback)
    if photo_callback is not None:
        set_photo_callback(photo_callback)

    async def restore_daemons() -> None:
        """Re-start any daemons that were active before the server restarted."""
        if (await db.get_setting("monitoring_mode", "false")) == "true":
            await monitoring_daemon.start()

        if (await db.get_setting("resume_auto_update", "false")) == "true":
            await resume_updater_daemon.start()

    asyncio.create_task(restore_daemons(), name="daemon_restore")


# Compat alias: some code does ``from autohh.scheduler import scheduler``
# (via ``manual_scheduler as scheduler`` in common.py) — the alias below
# ensures a bare ``import autohh.scheduler; autohh.scheduler.manual_scheduler``
# path also resolves correctly (it already does via the re-export above).
scheduler = manual_scheduler

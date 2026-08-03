import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BackgroundDaemon — standard asyncio service lifecycle pattern
# ---------------------------------------------------------------------------

class BackgroundDaemon:
    """
    Manages a single long-running async background task.

    The asyncio primitives (Lock, Event) are created lazily on first access so
    they always bind to the event loop that is actually running — avoiding the
    "attached to a different event loop" error that arises when primitives are
    created at module import time (before asyncio.run() starts).

    Typical usage
    -------------
        daemon = MyDaemon("name")
        await daemon.start()    # idempotent — safe to call multiple times
        daemon.trigger()        # wake up immediately, skip current sleep
        await daemon.stop()     # cancel task and wait for it to finish

    Subclasses override _run() with the actual loop logic and call
    ``await self.sleep(seconds)`` instead of ``asyncio.sleep(seconds)``
    to support interruptible sleeps.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._task: Optional[asyncio.Task] = None
        # Created lazily so they bind to the running event loop
        self._lock: Optional[asyncio.Lock] = None
        self._wakeup: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # Lazy primitive accessors
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_wakeup(self) -> asyncio.Event:
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        return self._wakeup

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True if the daemon task is alive."""
        return self._task is not None and not self._task.done()

    def trigger(self) -> None:
        """Wake up the daemon immediately, skipping the current sleep."""
        if self._wakeup is not None:
            self._wakeup.set()

    async def start(self) -> None:
        """Start the daemon task.  Idempotent — safe to call multiple times."""
        async with self._get_lock():
            if not self.is_running():
                self._task = asyncio.create_task(
                    self._run(), name=f"daemon_{self.name}"
                )
                logger.info(f"{self.name} daemon started")

    async def stop(self) -> None:
        """Cancel the daemon task and wait for clean shutdown."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info(f"{self.name} daemon stopped")

    async def sleep(self, seconds: float) -> bool:
        """
        Sleep for up to *seconds*, but return early when trigger() is called.

        Returns
        -------
        True  — woken up by trigger() before the timeout elapsed
        False — full timeout elapsed normally
        """
        event = self._get_wakeup()
        event.clear()
        try:
            await asyncio.wait_for(asyncio.shield(event.wait()), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        raise NotImplementedError

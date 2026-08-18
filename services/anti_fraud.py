import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Optional

from .. import database as db

logger = logging.getLogger(__name__)


class AntiFraud:
    def __init__(self, config) -> None:
        self._config = config
        self._last_request_time: Optional[float] = None

    async def check_rate_limits(self) -> tuple[bool, str]:
        config = self._config

        today_stats = await db.get_today_stats()
        if today_stats["successful"] >= config.max_apps_per_day:
            msg = f"Daily limit reached: {today_stats['successful']}/{config.max_apps_per_day}"
            logger.warning(msg)
            return False, msg

        hourly_count = await db.get_hourly_applications_count()
        if hourly_count >= config.max_apps_per_hour:
            msg = f"Hourly limit reached: {hourly_count}/{config.max_apps_per_hour}"
            logger.warning(msg)
            return False, msg

        if config.auto_start_hour is not None and config.auto_stop_hour is not None:
            current_hour = datetime.now().hour
            if not (config.auto_start_hour <= current_hour < config.auto_stop_hour):
                msg = f"Outside schedule: {config.auto_start_hour}:00 - {config.auto_stop_hour}:00"
                return False, msg

        return True, ""

    async def random_delay(self, is_page_change: bool = False) -> None:
        config = self._config
        if is_page_change:
            delay = config.delay_between_pages + random.uniform(-2, 3)
        else:
            delay = random.uniform(config.delay_min, config.delay_max)

        delay = max(1.0, delay)

        if self._last_request_time:
            elapsed = time.time() - self._last_request_time
            if elapsed < delay:
                wait = delay - elapsed
                logger.debug(f"Anti-fraud delay: {wait:.1f}s")
                await asyncio.sleep(wait)
        else:
            await asyncio.sleep(delay)

        self._last_request_time = time.time()

    async def pre_action_delay(self) -> None:
        await asyncio.sleep(random.uniform(0.3, 1.2))

    async def post_action_delay(self) -> None:
        await asyncio.sleep(random.uniform(0.5, 2.0))
import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Optional

from .. import database as db

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
    {"width": 1600, "height": 900},
]


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_random_viewport() -> dict:
    return random.choice(VIEWPORTS)


class AntiFraud:
    def __init__(self, config) -> None:
        self._config = config
        self._last_request_time: Optional[float] = None
        self._consecutive_captchas = 0

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

    async def random_scroll(self, page) -> None:
        scroll_amount = random.randint(100, 400)
        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await asyncio.sleep(random.uniform(0.3, 0.8))

    def captcha_detected(self) -> None:
        self._consecutive_captchas += 1
        logger.warning(f"Captcha detected ({self._consecutive_captchas} consecutive)")

    def captcha_cleared(self) -> None:
        self._consecutive_captchas = 0

    @property
    def should_pause_on_captcha(self) -> bool:
        return self._consecutive_captchas >= 2

    async def post_apply_delay(self) -> None:
        await asyncio.sleep(random.uniform(1.5, 3.5))

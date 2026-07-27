import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from ..config import get_settings
from .anti_fraud import get_random_viewport, get_random_user_agent

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._settings = get_settings()

    async def start(self) -> None:
        async with self._lock:
            if self._playwright is None:
                logger.info("Starting Playwright...")
                self._playwright = await async_playwright().start()

                launch_args = {
                    "headless": self._settings.browser_headless,
                    "slow_mo": self._settings.browser_slow_mo,
                }

                if self._settings.proxy_url:
                    launch_args["proxy"] = {"server": self._settings.proxy_url}

                self._browser = await self._playwright.chromium.launch(**launch_args)
                logger.info("Browser launched")

    async def stop(self) -> None:
        async with self._lock:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            logger.info("Browser stopped")

    def _validate_session(self) -> None:
        if not self._settings.session_file.exists():
            raise FileNotFoundError(
                f"Session file not found: {self._settings.session_file}. "
                "Use /login in Telegram bot first."
            )

    @asynccontextmanager
    async def get_page(self, use_session: bool = True) -> AsyncGenerator[Page, None]:
        if not self._browser:
            await self.start()

        context: Optional[BrowserContext] = None
        try:
            viewport = get_random_viewport()
            user_agent = get_random_user_agent()

            ctx_args = {
                "viewport": viewport,
                "user_agent": user_agent,
            }

            if use_session:
                self._validate_session()
                ctx_args["storage_state"] = str(self._settings.session_file)

            if self._settings.proxy_url:
                ctx_args["proxy"] = {"server": self._settings.proxy_url}

            context = await self._browser.new_context(**ctx_args)
            page = await context.new_page()
            page.set_default_timeout(self._settings.page_timeout)

            yield page

        finally:
            if context:
                await context.close()

    @asynccontextmanager
    async def get_interactive_context(
        self, headless: Optional[bool] = None
    ) -> AsyncGenerator[tuple[BrowserContext, Page], None]:
        if headless is None:
            headless = self._settings.browser_headless

        async with async_playwright() as p:
            launch_args = {"headless": headless}
            if self._settings.proxy_url:
                launch_args["proxy"] = {"server": self._settings.proxy_url}

            browser = await p.chromium.launch(**launch_args)

            viewport = get_random_viewport()
            user_agent = get_random_user_agent()

            ctx_args = {"viewport": viewport, "user_agent": user_agent}
            if self._settings.proxy_url:
                ctx_args["proxy"] = {"server": self._settings.proxy_url}

            context = await browser.new_context(**ctx_args)
            page = await context.new_page()

            try:
                yield context, page
            finally:
                await browser.close()


browser_manager = BrowserManager()

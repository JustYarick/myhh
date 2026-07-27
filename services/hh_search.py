import asyncio
import logging
import random
import re
from typing import Optional

from playwright.async_api import Page

from ..models import Vacancy
from .browser import browser_manager
from .anti_fraud import AntiFraud, get_random_viewport, get_random_user_agent

logger = logging.getLogger(__name__)


class HHSearchService:
    async def _check_bot_protection(self, page: Page) -> bool:
        try:
            title = (await page.title()).lower()
            if "captcha" in title or "smartcaptcha" in title or "подтвердите" in title:
                logger.warning(f"Captcha detected by title: {title}")
                return True

            for sel in [
                "[data-qa*='captcha']",
                "iframe[src*='captcha']",
                "iframe[src*='smartcaptcha']",
                ".SmartCaptcha",
                "#captcha",
                "[class*='captcha']",
                "div[data-testid='captcha']",
            ]:
                if await page.locator(sel).count() > 0:
                    logger.warning(f"Captcha detected by selector: {sel}")
                    return True

            body_text = await page.evaluate(
                "() => { const b = document.body; return b ? b.innerText.substring(0, 2000) : ''; }"
            )
            body_lower = body_text.lower()
            if "подтвердите, что вы не робот" in body_lower or "smartcaptcha" in body_lower:
                logger.warning("Captcha detected by body text")
                return True

            return False
        except Exception as e:
            logger.debug(f"Captcha check error: {e}")
            return False

    async def _get_vacancy_description(self, page: Page, url: str) -> str:
        try:
            logger.debug(f"Fetching description: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_selector("[data-qa='vacancy-description']", timeout=10000)

            description_el = page.locator("[data-qa='vacancy-description']")
            if await description_el.count() > 0:
                text = (await description_el.inner_text()).strip()
                logger.debug(f"Description length: {len(text)} chars")
                logger.debug(f"[VACANCY FULL DESC]\n{'='*60}\n{text}\n{'='*60}")
                return text
            return ""
        except Exception as e:
            logger.warning(f"Failed to get description for {url}: {e}")
            return ""

    async def get_vacancy_count(self, page: Page) -> int:
        for sel in [
            "[data-qa='vacancy-serp__found-count']",
            "[data-qa='vacancy-serp__results-search-info']",
            "h1",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    text = await el.inner_text()
                    nums = re.findall(r"[\d\s]+", text.replace("\xa0", " "))
                    for n in nums:
                        n = n.strip().replace(" ", "")
                        if n.isdigit():
                            count = int(n)
                            logger.info(f"Vacancy count from '{sel}': {count}")
                            return count
            except Exception:
                continue

        try:
            text = await page.locator("body").inner_text()
            m = re.search(r"(?:Найден[оа]?\s+)([\d\s]+)\s*(?:ваканси|результа)", text)
            if m:
                count = int(m.group(1).replace(" ", ""))
                logger.info(f"Vacancy count from body text: {count}")
                return count
        except Exception:
            pass

        logger.warning("Could not extract vacancy count")
        return 0

    async def _parse_vacancy_cards(self, page: Page) -> list[dict]:
        vacancy_data: list[dict] = []
        cards = await page.locator("[data-qa='vacancy-serp__vacancy']").all()

        for i, card in enumerate(cards):
            try:
                title_el = card.locator("[data-qa='serp-item__title']")
                await title_el.wait_for(state="visible", timeout=5000)

                href = await title_el.get_attribute("href")
                title = await title_el.inner_text()

                employer_el = card.locator(
                    "[data-qa='vacancy-serp__vacancy-employer']"
                ).first
                employer = (
                    await employer_el.inner_text()
                    if await employer_el.count() > 0
                    else "Unknown"
                )

                salary = ""
                salary_el = card.locator("[data-qa='vacancy-serp__vacancy-compensation']").first
                if await salary_el.count() > 0:
                    salary = await salary_el.inner_text()

                vacancy_data.append({
                    "title": title,
                    "url": href,
                    "employer": employer,
                    "salary": salary,
                })
                logger.debug(f"Card {i}: {title} @ {employer}")
            except Exception as e:
                logger.warning(f"Failed to parse vacancy card {i}: {e}")
                continue

        logger.info(f"Parsed {len(vacancy_data)} vacancy cards")
        return vacancy_data

    async def _fetch_descriptions(
        self, page: Page, vacancy_data: list[dict], af: AntiFraud, max_count: int = 0
    ) -> list[Vacancy]:
        vacancies: list[Vacancy] = []
        items = vacancy_data[:max_count] if max_count > 0 else vacancy_data
        for data in items:
            await af.pre_action_delay()
            description = await self._get_vacancy_description(page, data["url"])
            vacancies.append(Vacancy(
                title=data["title"],
                url=data["url"],
                employer=data["employer"],
                description=description,
            ))
            await af.post_action_delay()
        return vacancies

    async def _go_to_search_page(self, page: Page, url: str, af: AntiFraud) -> None:
        logger.info(f"Navigating to: {url}")
        await af.random_delay()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)

        if await self._check_bot_protection(page):
            af.captcha_detected()
            raise RuntimeError("Bot protection triggered (captcha)")

        af.captcha_cleared()

    async def _wait_for_vacancies(self, page: Page) -> bool:
        try:
            await page.wait_for_selector(
                "[data-qa='vacancy-serp__vacancy']", timeout=10000
            )
            return True
        except Exception:
            logger.info("No vacancy cards found on page")
            return False

    async def get_vacancy_description(self, page: Page, url: str) -> str:
        return await self._get_vacancy_description(page, url)

    async def close_page(self, page: Page) -> None:
        try:
            await page.context.__aexit__(None, None, None)
        except Exception:
            pass

    async def search_cards(
        self,
        anti_fraud: AntiFraud,
        page_num: int = 0,
        url: str = "",
        existing_page: Optional[Page] = None,
    ) -> tuple[Optional[Page], list[dict], Optional[object]]:
        if "page=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={page_num}"
        else:
            url = re.sub(r"page=\d+", f"page={page_num}", url)

        logger.info(f"search_cards: {url}")

        page = existing_page
        context = None

        if page is None:
            ctx_args = {
                "viewport": get_random_viewport(),
                "user_agent": get_random_user_agent(),
            }
            if browser_manager._settings.session_file.exists():
                ctx_args["storage_state"] = str(browser_manager._settings.session_file)
            if browser_manager._settings.proxy_url:
                ctx_args["proxy"] = {"server": browser_manager._settings.proxy_url}

            await browser_manager._ensure_browser()
            context = await browser_manager._browser.new_context(**ctx_args)
            page = await context.new_page()
            page.set_default_timeout(browser_manager._settings.page_timeout)

        await self._go_to_search_page(page, url, anti_fraud)

        count = await self.get_vacancy_count(page)
        logger.info(f"Count: {count}")

        if not await self._wait_for_vacancies(page):
            return page, [], context

        await anti_fraud.random_scroll(page)
        vacancy_data = await self._parse_vacancy_cards(page)

        logger.info(f"Returning {len(vacancy_data)} cards")
        return page, vacancy_data, context

    async def search(
        self,
        anti_fraud: AntiFraud,
        page_num: int = 0,
        url: str = "",
        query: str = "",
        area_code: str = "",
    ) -> list[Vacancy]:
        if url:
            return await self.search_by_url(url, anti_fraud, page_num)

        if not query:
            return []

        logger.info(f"Searching: query='{query}', area={area_code}, page={page_num}")

        search_url = (
            f"https://hh.ru/search/vacancy?"
            f"text={query}&area={area_code}"
            f"&items_on_page=20&page={page_num}"
        )

        async with browser_manager.get_page(use_session=True) as page:
            await self._go_to_search_page(page, search_url, anti_fraud)

            count = await self.get_vacancy_count(page)
            logger.info(f"Total vacancies found: {count}")

            if not await self._wait_for_vacancies(page):
                return []

            await anti_fraud.random_scroll(page)
            vacancy_data = await self._parse_vacancy_cards(page)
            vacancies = await self._fetch_descriptions(page, vacancy_data, anti_fraud)

            logger.info(f"Returning {len(vacancies)} vacancies from page {page_num}")
            return vacancies

    async def search_by_url(
        self,
        url: str,
        anti_fraud: AntiFraud,
        page_num: int = 0,
        max_descriptions: int = 0,
    ) -> list[Vacancy]:
        if "page=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={page_num}"
        else:
            url = re.sub(r"page=\d+", f"page={page_num}", url)

        logger.info(f"Searching by URL: {url}")

        async with browser_manager.get_page(use_session=True) as page:
            await self._go_to_search_page(page, url, anti_fraud)

            count = await self.get_vacancy_count(page)
            logger.info(f"Total vacancies found: {count}")

            if not await self._wait_for_vacancies(page):
                return []

            await anti_fraud.random_scroll(page)
            vacancy_data = await self._parse_vacancy_cards(page)
            vacancies = await self._fetch_descriptions(
                page, vacancy_data, anti_fraud, max_count=max_descriptions
            )

            logger.info(f"Returning {len(vacancies)} vacancies")
            return vacancies

    async def get_search_info(self, url: str, anti_fraud: AntiFraud) -> tuple[int, str]:
        logger.info(f"Getting search info for: {url}")

        async with browser_manager.get_page(use_session=True) as page:
            await self._go_to_search_page(page, url, anti_fraud)

            count = await self.get_vacancy_count(page)

            page_title = await page.title()
            logger.info(f"Search info: count={count}, title={page_title}")
            return count, page_title

    async def search_with_info(
        self,
        url: str,
        anti_fraud: AntiFraud,
        page_num: int = 0,
        max_descriptions: int = 0,
    ) -> tuple[int, str, list[Vacancy]]:
        if "page=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={page_num}"
        else:
            url = re.sub(r"page=\d+", f"page={page_num}", url)

        logger.info(f"Search with info: {url}")

        async with browser_manager.get_page(use_session=True) as page:
            await self._go_to_search_page(page, url, anti_fraud)

            count = await self.get_vacancy_count(page)
            page_title = await page.title()
            logger.info(f"Count: {count}, title: {page_title}")

            if not await self._wait_for_vacancies(page):
                return count, page_title, []

            await anti_fraud.random_scroll(page)
            vacancy_data = await self._parse_vacancy_cards(page)
            vacancies = await self._fetch_descriptions(
                page, vacancy_data, anti_fraud, max_count=max_descriptions
            )

            logger.info(f"Returning {len(vacancies)} vacancies")
            return count, page_title, vacancies


search_service = HHSearchService()

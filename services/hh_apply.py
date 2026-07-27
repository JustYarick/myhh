import logging
from typing import Optional

from playwright.async_api import Page

from ..models import ApplyStatus, ApplyResult
from .browser import browser_manager
from .anti_fraud import AntiFraud

logger = logging.getLogger(__name__)


class HHApplyService:
    async def _check_bot_protection(self, page: Page) -> bool:
        title = await page.title()
        content = await page.content()
        return "captcha" in title.lower() or "robot" in content.lower()

    async def _check_already_applied(self, page: Page) -> bool:
        locator = page.locator("text=Вы откликнулись")
        return await locator.count() > 0

    async def _select_resume(self, page: Page, resume_id: str) -> bool:
        if not resume_id:
            return True

        logger.debug(f"Attempting to select resume: {resume_id}")

        for sel in [
            'select[data-qa="resume-selector"]',
            'select[data-qa="resume-select"]',
            '.resume-selector select',
            'select[name="resume_id"]',
        ]:
            try:
                select_el = page.locator(sel).first
                if await select_el.count() > 0:
                    options = await select_el.locator("option").all()
                    for opt in options:
                        val = await opt.get_attribute("value") or ""
                        if resume_id in val:
                            await select_el.select_option(value=val)
                            logger.info(f"Selected resume via {sel}: {val}")
                            return True

                    logger.warning(f"Resume {resume_id} not found in select options")
                    break
            except Exception:
                continue

        for sel in [
            '[data-qa="resume-selector-popup"]',
            '.resume-selector-popup',
        ]:
            try:
                popup = page.locator(sel).first
                if await popup.count() > 0:
                    resume_option = popup.locator(f'[data-resume-id="{resume_id}"]')
                    if await resume_option.count() > 0:
                        await resume_option.click()
                        logger.info(f"Selected resume from popup: {resume_id}")
                        return True

                    links = popup.locator("a, button, div[role='option']")
                    count = await links.count()
                    for i in range(count):
                        try:
                            link = links.nth(i)
                            href = await link.get_attribute("href") or ""
                            data_id = await link.get_attribute("data-resume-id") or ""
                            if resume_id in href or resume_id in data_id:
                                await link.click()
                                logger.info(f"Selected resume from popup link: {resume_id}")
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        logger.debug("Resume selector not found, using default resume")
        return True

    async def _fill_cover_letter_modal(
        self, page: Page, message: str, af: AntiFraud,
    ) -> Optional[ApplyResult]:
        try:
            await page.wait_for_selector(
                "[data-qa='vacancy-response-popup']", timeout=5000
            )
            await page.wait_for_timeout(1000)

            letter_area = page.locator(
                "textarea[data-qa='vacancy-response-popup-form-letter-input']"
            )
            if await letter_area.count() > 0:
                await af.pre_action_delay()
                await letter_area.fill(message)
            else:
                logger.warning("Cover letter field not found in modal")

            submit_btn = page.locator(
                "button[data-qa='vacancy-response-submit-popup']"
            )
            if await submit_btn.count() > 0:
                await af.pre_action_delay()
                await submit_btn.click()
                await af.post_action_delay()
                return ApplyResult(
                    status=ApplyStatus.SUCCESS, message="Applied with cover letter"
                )
            else:
                return ApplyResult(
                    status=ApplyStatus.ERROR, message="Submit button not found"
                )
        except Exception as e:
            logger.error(f"Modal interaction failed: {e}")
            return None

    async def _try_cover_letter_link(
        self, page: Page, message: str, af: AntiFraud,
    ) -> Optional[ApplyResult]:
        cover_letter_link = page.locator(
            "a:has-text('Написать сопроводительное')"
        )
        if await cover_letter_link.count() > 0 and message:
            await af.pre_action_delay()
            await cover_letter_link.first.click()
            result = await self._fill_cover_letter_modal(page, message, af)
            if result:
                return result
        return None

    async def _try_dropdown_apply(
        self, page: Page, message: str, af: AntiFraud,
    ) -> Optional[ApplyResult]:
        dropdown_arrow = page.locator(
            "[data-qa='vacancy-response-link-top'] + button, "
            "[data-qa='vacancy-response-link-bottom'] + button"
        )
        if await dropdown_arrow.count() > 0 and message:
            await af.pre_action_delay()
            await dropdown_arrow.first.click()
            await page.wait_for_timeout(500)

            with_letter_option = page.locator(
                "text=С сопроводительным письмом"
            )
            if await with_letter_option.count() > 0:
                await with_letter_option.first.click()
                result = await self._fill_cover_letter_modal(page, message, af)
                if result:
                    return result
        return None

    async def _try_post_apply_letter(
        self, page: Page, message: str, af: AntiFraud,
    ) -> Optional[ApplyResult]:
        resume_delivered = page.locator("text=Резюме доставлено")
        if await resume_delivered.count() > 0 or await page.locator("textarea").count() > 0:
            letter_area = page.locator("textarea")
            if await letter_area.count() > 0 and message:
                await af.pre_action_delay()
                await letter_area.first.fill(message)

                submit_btn = page.locator("button:has-text('Отправить')")
                if await submit_btn.count() > 0:
                    await submit_btn.first.click()
                    await af.post_action_delay()
                    return ApplyResult(
                        status=ApplyStatus.SUCCESS,
                        message="Applied with post-apply cover letter",
                    )
        return None

    async def _check_application_success(self, page: Page) -> bool:
        for selector in [
            "text=Отклик отправлен",
            "text=Вы откликнулись",
            "text=Резюме доставлено",
        ]:
            if await page.locator(selector).count() > 0:
                return True
        return False

    async def apply(
        self, url: str, message: str = "",
        af: Optional[AntiFraud] = None,
        resume_id: str = "",
    ) -> ApplyResult:
        logger.info(f"Applying to: {url}" + (f" (resume={resume_id})" if resume_id else ""))
        if af is None:
            from .anti_fraud import AntiFraud as AF
            from .flow_entity import get_active_flow
            flow = await get_active_flow()
            af = AF(flow.config if flow else None)

        try:
            async with browser_manager.get_page(use_session=True) as page:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    logger.debug("Vacancy page loaded")
                except Exception as e:
                    logger.warning(f"Navigation timeout: {e}")

                if await self._check_bot_protection(page):
                    af.captcha_detected()
                    logger.warning("Captcha detected on vacancy page")
                    return ApplyResult(
                        status=ApplyStatus.CAPTCHA,
                        message="Bot protection triggered (captcha)",
                    )

                af.captcha_cleared()

                if await self._check_already_applied(page):
                    logger.info("Already applied to this vacancy")
                    return ApplyResult(
                        status=ApplyStatus.SKIPPED, message="Already applied"
                    )

                result = await self._try_cover_letter_link(page, message, af)
                if result:
                    logger.info(f"Cover letter link strategy: {result.status.value}")
                    return result

                apply_btn = page.locator("[data-qa='vacancy-response-link-top']")
                if await apply_btn.count() == 0:
                    apply_btn = page.locator("[data-qa='vacancy-response-link-bottom']")

                if await apply_btn.count() == 0:
                    logger.warning("Apply button not found")
                    return ApplyResult(
                        status=ApplyStatus.ERROR, message="Apply button not found"
                    )

                await self._select_resume(page, resume_id)

                result = await self._try_dropdown_apply(page, message, af)
                if result:
                    logger.info(f"Dropdown strategy: {result.status.value}")
                    return result

                await af.pre_action_delay()
                await apply_btn.first.click()
                await af.post_action_delay()
                logger.debug("Clicked standard apply button")

                await self._select_resume(page, resume_id)

                result = await self._try_post_apply_letter(page, message, af)
                if result:
                    logger.info(f"Post-apply letter strategy: {result.status.value}")
                    return result

                if await self._check_application_success(page):
                    logger.info("Application confirmed successful")
                    return ApplyResult(
                        status=ApplyStatus.SUCCESS,
                        message="Applied successfully",
                    )
                else:
                    logger.info("Applied but status unclear")
                    return ApplyResult(
                        status=ApplyStatus.SUCCESS,
                        message="Applied (status unclear)",
                    )

        except FileNotFoundError as e:
            logger.error(f"Session file not found: {e}")
            return ApplyResult(status=ApplyStatus.ERROR, message=str(e))
        except Exception as e:
            logger.error(f"Application failed: {e}", exc_info=True)
            return ApplyResult(status=ApplyStatus.ERROR, message=str(e))


apply_service = HHApplyService()

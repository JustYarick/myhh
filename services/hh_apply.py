import asyncio
import logging
from typing import Optional

from playwright.async_api import Page

from ..models import ApplyStatus, ApplyResult
from .browser import browser_manager
from .anti_fraud import AntiFraud
from .playwright_utils import check_bot_protection, dump_html
from .hh_apply_strategies import (
    LegacyLinkApplyStrategy,
    DropdownApplyStrategy,
    ModalApplyStrategy,
    PostApplyLetterStrategy,
    select_legacy_resume,
    check_application_success,
)

logger = logging.getLogger(__name__)

async def check_has_questions(page: Page) -> bool:
    try:
        url = page.url
        if "vacancy_response" in url:
            return True
    except Exception:
        pass
    return False


class HHApplyService:
    async def _check_already_applied(self, page: Page) -> bool:
        locator = page.locator("text=Вы откликнулись")
        try:
            return await locator.count() > 0
        except Exception:
            return False

    async def apply(
        self,
        url: str,
        message: str = "",
        af: Optional[AntiFraud] = None,
        resume_id: str = "",
        existing_page: Optional[Page] = None,
    ) -> ApplyResult:
        logger.info(
            f"Applying to: {url}" + (f" (resume={resume_id})" if resume_id else "")
        )
        if af is None:
            from .anti_fraud import AntiFraud as AF
            from .flow_entity import get_active_flow

            flow = await get_active_flow()
            af = AF(flow.config if flow else None)

        try:
            if existing_page:
                context = existing_page.context
                page = await context.new_page()
                page.set_default_timeout(
                    existing_page._timeout_settings._default_timeout
                    if hasattr(existing_page, "_timeout_settings")
                    else 30000
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    logger.debug("Vacancy page loaded")
                except Exception as e:
                    logger.warning(f"Navigation timeout: {e}")

                result = await self._do_apply(page, message, af, resume_id)
                try:
                    await page.close()
                except Exception:
                    pass
                return result
            else:
                async with browser_manager.get_page(use_session=True) as page:
                    try:
                        await page.goto(
                            url, wait_until="domcontentloaded", timeout=30000
                        )
                        logger.debug("Vacancy page loaded")
                    except Exception as e:
                        logger.warning(f"Navigation timeout: {e}")

                    return await self._do_apply(page, message, af, resume_id)

        except FileNotFoundError as e:
            logger.error(f"Session file not found: {e}")
            return ApplyResult(status=ApplyStatus.ERROR, message=str(e))
        except Exception as e:
            logger.error(f"Application failed: {e}", exc_info=True)
            return ApplyResult(status=ApplyStatus.ERROR, message=str(e))

    async def _do_apply(
        self,
        page: Page,
        message: str,
        af: AntiFraud,
        resume_id: str,
    ) -> ApplyResult:
        if await check_bot_protection(page):
            af.captcha_detected()
            logger.warning("Captcha detected on vacancy page")
            return ApplyResult(
                status=ApplyStatus.CAPTCHA,
                message="Bot protection triggered (captcha)",
            )

        af.captcha_cleared()

        if await self._check_already_applied(page):
            logger.info("Already applied to this vacancy")
            return ApplyResult(status=ApplyStatus.SKIPPED, message="Already applied")

        if await check_has_questions(page):
            logger.info("Questionnaire page detected at start. Skipping.")
            return ApplyResult(status=ApplyStatus.ANALYZED_SKIP, message="requires_questions")

        # 1. Legacy link strategy ("Написать сопроводительное" link on page)
        result = await LegacyLinkApplyStrategy().try_apply(page, message, resume_id, af)
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

        # 2. Dropdown strategy ("apply with cover letter" arrow menu next to standard apply)
        result = await DropdownApplyStrategy().try_apply(page, message, resume_id, af)
        if result:
            logger.info(f"Dropdown strategy: {result.status.value}")
            return result

        # Perform the actual standard click on standard apply button
        await af.pre_action_delay()
        await apply_btn.first.click()
        await af.post_action_delay()
        logger.debug("Clicked standard apply button")

        # Wait a moment for page changes and check for questionnaires/questions
        await page.wait_for_timeout(2000)
        if await check_has_questions(page):
            logger.info("Questions questionnaire page detected. Skipping apply.")
            return ApplyResult(
                status=ApplyStatus.ANALYZED_SKIP,
                message="requires_questions"
            )

        # 3. Modern Magritte-based response modal/dialog dialog
        try:
            modal_result = await asyncio.wait_for(
                ModalApplyStrategy().try_apply(page, message, resume_id, af),
                timeout=40,
            )
        except asyncio.TimeoutError:
            logger.error("Response modal handling exceeded hard 40s timeout")
            await dump_html(page, "response_modal_hard_timeout")
            modal_result = ApplyResult(
                status=ApplyStatus.ERROR, message="Response modal handling timed out"
            )

        if modal_result:
            logger.info(f"Response modal strategy: {modal_result.status.value}")
            return modal_result

        # 4. Fallback: select resume + legacy post-apply letter flow
        await select_legacy_resume(page, resume_id)

        result = await PostApplyLetterStrategy().try_apply(page, message, resume_id, af)
        if result:
            logger.info(f"Post-apply letter strategy: {result.status.value}")
            return result

        if await check_application_success(page):
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


apply_service = HHApplyService()

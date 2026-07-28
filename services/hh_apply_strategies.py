import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

from playwright.async_api import Page, Locator

from ..models import ApplyResult, ApplyStatus
from .anti_fraud import AntiFraud
from .playwright_utils import (
    safe_click,
    force_close_stray_overlays,
    dump_html,
    check_bot_protection,
)

logger = logging.getLogger(__name__)


async def check_application_success(page: Page) -> bool:
    for selector in [
        "text=Отклик отправлен",
        "text=Вы откликнулись",
        "text=Резюме доставлено",
    ]:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


class ApplyStrategy(ABC):
    @abstractmethod
    async def try_apply(
        self,
        page: Page,
        message: str,
        resume_id: str,
        af: AntiFraud,
    ) -> Optional[ApplyResult]:
        """
        Attempts to apply using this strategy.
        Returns:
            ApplyResult if the strategy was attempted and resulted in success, error, or captcha.
            None if the strategy is not applicable to the current page state.
        """
        pass


class LegacyLinkApplyStrategy(ApplyStrategy):
    """
    Handles the case where there is a separate 'Написать сопроводительное' link
    available on the vacancy page before clicking any main apply button.
    """
    async def try_apply(
        self, page: Page, message: str, resume_id: str, af: AntiFraud
    ) -> Optional[ApplyResult]:
        cover_letter_link = page.locator("a:has-text('Написать сопроводительное')")
        try:
            if await cover_letter_link.count() > 0 and message:
                await af.pre_action_delay()
                await cover_letter_link.first.click()
                result = await fill_cover_letter_modal(page, message, af)
                if result:
                    return result
        except Exception as e:
            logger.warning(f"Legacy link strategy failed: {e}")
        return None


class DropdownApplyStrategy(ApplyStrategy):
    """
    Handles the case where there is an arrow button next to the main apply button,
    allowing users to choose 'С сопроводительным письмом' from a dropdown.
    """
    async def try_apply(
        self, page: Page, message: str, resume_id: str, af: AntiFraud
    ) -> Optional[ApplyResult]:
        dropdown_arrow = page.locator(
            "[data-qa='vacancy-response-link-top'] + button, "
            "[data-qa='vacancy-response-link-bottom'] + button"
        )
        try:
            if await dropdown_arrow.count() > 0 and message:
                await af.pre_action_delay()
                await dropdown_arrow.first.click()
                await page.wait_for_timeout(500)

                with_letter_option = page.locator("text=С сопроводительным письмом")
                if await with_letter_option.count() > 0:
                    await with_letter_option.first.click()
                    result = await fill_cover_letter_modal(page, message, af)
                    if result:
                        return result
        except Exception as e:
            logger.warning(f"Dropdown apply strategy failed: {e}")
        return None


class ModalApplyStrategy(ApplyStrategy):
    """
    Handles the modern Magritte-based response dialog that appears after clicking the
    standard apply button.
    """
    async def try_apply(
        self, page: Page, message: str, resume_id: str, af: AntiFraud
    ) -> Optional[ApplyResult]:
        dialog = page.locator('div[role="dialog"][aria-modal="true"]:visible').last
        try:
            await dialog.wait_for(state="visible", timeout=5000)
        except Exception:
            return None

        logger.info("Response modal detected")
        await dump_html(page, "response_modal_open")

        form = dialog.locator('form[name="vacancy_response"]').first
        if await form.count() == 0:
            form = page.locator('form[name="vacancy_response"]:visible').last

        # 1. Resume selection
        if resume_id:
            await self._select_resume_in_modal(page, dialog, form, resume_id)
            # Re-locate dialog and form after possible React re-renders
            dialog = page.locator('div[role="dialog"][aria-modal="true"]:visible').last
            form = dialog.locator('form[name="vacancy_response"]').first
            if await form.count() == 0:
                form = page.locator('form[name="vacancy_response"]:visible').last

        # 2. Cover letter input
        if message:
            await self._fill_cover_letter(page, dialog, form, message, af)

        # 3. Submit
        return await self._submit_modal(page, dialog, form, message, af)

    async def _select_resume_in_modal(self, page: Page, dialog, form, resume_id: str) -> None:
        try:
            trigger_candidates = [
                '[data-qa="resume-form-resume"]',
                '[data-qa*="resume"][role="button"]',
                'button[data-qa*="resume"]',
            ]
            trigger = None
            for selector in trigger_candidates:
                candidate = dialog.locator(selector)
                if await candidate.count() > 0 and await candidate.first.is_visible():
                    trigger = candidate.first
                    break

            if trigger is not None and await safe_click(trigger, "resume_trigger"):
                await page.wait_for_timeout(400)
                await dump_html(page, "response_modal_resume_dropdown")

                option = page.locator(
                    f'[role="option"][data-resume-id="{resume_id}"], '
                    f'[data-resume-id="{resume_id}"], '
                    f'[href*="{resume_id}"]'
                ).first

                if await option.count() == 0:
                    matched = await page.evaluate(
                        """(resumeId) => {
                            const nodes = Array.from(document.querySelectorAll(
                                '[role="option"], li, button, a, [data-qa*="resume"]'
                            ));
                            const node = nodes.find((el) => {
                                const attrs = el.getAttributeNames()
                                    .map((name) => el.getAttribute(name) || '')
                                    .join(' ');
                                return attrs.includes(resumeId);
                            });
                            if (!node) return false;
                            node.setAttribute('data-autohh-resume-match', '1');
                            return true;
                        }""",
                        resume_id,
                    )
                    if matched:
                        option = page.locator('[data-autohh-resume-match="1"]').first

                if await option.count() > 0:
                    if await safe_click(option, "resume_option"):
                        logger.info(f"Resume '{resume_id}' selected in modal dropdown")
                    else:
                        logger.warning(f"Resume option for '{resume_id}' was found but not clicked")
                else:
                    logger.warning(
                        f"Resume '{resume_id}' was not found in the modal dropdown; "
                        "keeping the currently selected resume"
                    )

                await force_close_stray_overlays(page)
                await page.wait_for_timeout(300)
                await dialog.wait_for(state="visible", timeout=3000)
        except Exception as e:
            logger.warning(f"Resume selection in modal failed: {e}")

    async def _fill_cover_letter(self, page: Page, dialog, form, message: str, af: AntiFraud) -> None:
        letter_area: Optional[Locator] = None
        try:
            textarea_selectors = [
                'textarea[data-qa="vacancy-response-popup-form-letter-input"]',
                'textarea[name*="letter"]',
                'textarea[data-qa*="letter"]',
                'textarea[data-qa*="cover"]',
                'textarea',
            ]

            async def locate_visible_textarea() -> Optional[Locator]:
                roots = [form, dialog, page]
                for root in roots:
                    for selector in textarea_selectors:
                        candidate = root.locator(selector)
                        count = await candidate.count()
                        for i in range(count):
                            item = candidate.nth(i)
                            try:
                                if await item.is_visible():
                                    return item
                            except Exception:
                                continue
                return None

            letter_area = await locate_visible_textarea()

            if letter_area is None:
                add_selectors = [
                    'button[data-qa="add-cover-letter"]',
                    'button:has-text("Добавить сопроводительное")',
                    '[data-qa="secondary-actions"] button',
                ]
                add_button = None
                for selector in add_selectors:
                    candidate = dialog.locator(selector)
                    if await candidate.count() > 0:
                        for i in range(await candidate.count()):
                            item = candidate.nth(i)
                            try:
                                if await item.is_visible():
                                    add_button = item
                                    break
                            except Exception:
                                continue
                    if add_button is not None:
                        break

                if add_button is not None:
                    await af.pre_action_delay()
                    if await safe_click(add_button, "add_cover_letter"):
                        try:
                            await page.locator(
                                'textarea[data-qa="vacancy-response-popup-form-letter-input"]:visible'
                            ).last.wait_for(state="visible", timeout=4000)
                        except Exception:
                            pass
                        letter_area = await locate_visible_textarea()

            if letter_area is not None:
                await letter_area.scroll_into_view_if_needed(timeout=2000)
                await letter_area.fill(message)
                logger.info("Cover letter filled in response modal")
            else:
                logger.warning("No visible cover-letter textarea found in response modal")
                await dump_html(page, "response_modal_no_textarea")
        except Exception as e:
            logger.warning(f"Cover letter fill in modal failed: {e}")

    async def _submit_modal(self, page: Page, dialog, form, message: str, af: AntiFraud) -> ApplyResult:
        try:
            await af.pre_action_delay()

            submit_result = await page.evaluate(
                r"""() => {
                    const isVisible = (el) => {
                        if (!el || !el.isConnected) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' &&
                               style.visibility !== 'hidden' &&
                               style.pointerEvents !== 'none' &&
                               rect.width > 0 && rect.height > 0;
                    };

                    const dialogs = Array.from(document.querySelectorAll(
                        'div[role="dialog"][aria-modal="true"]'
                    )).filter(isVisible);
                    const dialog = dialogs.at(-1);
                    if (!dialog) {
                        return {ok: false, reason: 'visible response dialog not found'};
                    }

                    const normalize = (value) => (value || '')
                        .replace(/\s+/g, ' ')
                        .trim();

                    const allButtons = Array.from(dialog.querySelectorAll(
                        'button, input[type="submit"], [role="button"]'
                    ));

                    const exactQa = allButtons.find((el) =>
                        isVisible(el) &&
                        (el.getAttribute('data-qa') === 'vacancy-response-submit-popup' ||
                         el.getAttribute('data-qa') === 'vacancy-response-submit')
                    );

                    const submitType = allButtons.find((el) =>
                        isVisible(el) && el.getAttribute('type') === 'submit'
                    );

                    const byText = allButtons.find((el) => {
                        if (!isVisible(el)) return false;
                        const text = normalize(el.innerText || el.textContent || el.value);
                        return text === 'Откликнуться' ||
                               text === 'Отправить' ||
                               text.includes('Откликнуться');
                    });

                    const button = exactQa || submitType || byText;
                    if (!button) {
                        return {
                            ok: false,
                            reason: 'submit button not found',
                            buttons: allButtons.filter(isVisible).map((el) => ({
                                text: normalize(el.innerText || el.textContent || el.value),
                                qa: el.getAttribute('data-qa'),
                                type: el.getAttribute('type'),
                                disabled: Boolean(el.disabled),
                                ariaDisabled: el.getAttribute('aria-disabled')
                            }))
                        };
                    }

                    if (button.disabled || button.getAttribute('aria-disabled') === 'true') {
                        return {
                            ok: false,
                            reason: 'submit button is disabled',
                            text: normalize(button.innerText || button.textContent || button.value)
                        };
                    }

                    button.scrollIntoView({block: 'center', inline: 'center'});
                    button.focus();

                    const form = button.form || dialog.querySelector('form[name="vacancy_response"]');
                    if (form && typeof form.requestSubmit === 'function') {
                        try {
                            if (button.tagName === 'BUTTON' && button.type === 'submit' && button.form === form) {
                                form.requestSubmit(button);
                            } else {
                                form.requestSubmit();
                            }
                            return {ok: true, method: 'requestSubmit', text: normalize(button.innerText || button.textContent || button.value)};
                        } catch (_) {}
                    }

                    button.click();
                    return {ok: true, method: 'dom-click', text: normalize(button.innerText || button.textContent || button.value)};
                }"""
            )

            if not submit_result.get("ok"):
                # A disabled button can become enabled after React finishes processing input.
                try:
                    await page.wait_for_function(
                        r"""() => {
                            const visible = (el) => {
                                if (!el || !el.isConnected) return false;
                                const r = el.getBoundingClientRect();
                                const s = getComputedStyle(el);
                                return r.width > 0 && r.height > 0 &&
                                       s.display !== 'none' && s.visibility !== 'hidden';
                            };
                            const dialog = Array.from(document.querySelectorAll(
                                'div[role="dialog"][aria-modal="true"]'
                            )).filter(visible).at(-1);
                            if (!dialog) return false;
                            return Array.from(dialog.querySelectorAll('button, input[type="submit"], [role="button"]'))
                                .some((el) => {
                                    const text = (el.innerText || el.textContent || el.value || '')
                                        .replace(/\s+/g, ' ').trim();
                                    return visible(el) &&
                                           (text === 'Откликнуться' || text === 'Отправить' ||
                                            el.getAttribute('data-qa') === 'vacancy-response-submit-popup') &&
                                           !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                                });
                        }""",
                        timeout=5000,
                    )
                except Exception:
                    pass

                submit_result = await page.evaluate(
                    r"""() => {
                        const visible = (el) => {
                            if (!el || !el.isConnected) return false;
                            const r = el.getBoundingClientRect();
                            const s = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                   s.display !== 'none' && s.visibility !== 'hidden';
                        };
                        const dialog = Array.from(document.querySelectorAll(
                            'div[role="dialog"][aria-modal="true"]'
                        )).filter(visible).at(-1);
                        if (!dialog) return {ok:false, reason:'dialog disappeared'};
                        const controls = Array.from(dialog.querySelectorAll(
                            'button, input[type="submit"], [role="button"]'
                        ));
                        const button = controls.find((el) => {
                            const text = (el.innerText || el.textContent || el.value || '')
                                .replace(/\s+/g, ' ').trim();
                            return visible(el) &&
                                   (el.getAttribute('data-qa') === 'vacancy-response-submit-popup' ||
                                    el.getAttribute('data-qa') === 'vacancy-response-submit' ||
                                    text === 'Откликнуться' || text === 'Отправить' ||
                                    text.includes('Откликнуться')) &&
                                   !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                        });
                        if (!button) return {ok:false, reason:'enabled submit button not found'};
                        button.scrollIntoView({block:'center'});
                        const form = button.form || dialog.querySelector('form[name="vacancy_response"]');
                        if (form && typeof form.requestSubmit === 'function') {
                            try {
                                button.form === form && button.type === 'submit'
                                    ? form.requestSubmit(button)
                                    : form.requestSubmit();
                                return {ok:true, method:'requestSubmit-retry'};
                            } catch (_) {}
                        }
                        button.click();
                        return {ok:true, method:'dom-click-retry'};
                    }"""
                )

            await af.post_action_delay()

            if not submit_result.get("ok"):
                logger.error(f"Could not submit response modal: {submit_result}")
                await dump_html(page, "response_modal_submit_fail")
                return ApplyResult(
                    status=ApplyStatus.ERROR,
                    message=f"Submit failed: {submit_result.get('reason', 'unknown reason')}",
                )

            logger.info(
                "Response modal submitted via %s (button=%r)",
                submit_result.get("method", "unknown"),
                submit_result.get("text", "Откликнуться"),
            )
        except Exception as e:
            logger.error(f"Failed to submit response modal: {e}", exc_info=True)
            await dump_html(page, "response_modal_submit_exception")
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message=f"Modal submit failed: {e}",
            )

        # Wait for modal close or success message
        try:
            await page.wait_for_function(
                """() => {
                    const visibleDialog = Array.from(
                        document.querySelectorAll('div[role="dialog"][aria-modal="true"]')
                    ).some((el) => el.offsetParent !== null);
                    const text = document.body ? document.body.innerText : '';
                    return !visibleDialog ||
                        text.includes('Отклик отправлен') ||
                        text.includes('Вы откликнулись') ||
                        text.includes('Резюме доставлено');
                }""",
                timeout=7000,
            )
        except Exception:
            pass

        if await check_bot_protection(page):
            af.captcha_detected()
            return ApplyResult(
                status=ApplyStatus.CAPTCHA,
                message="Captcha after modal submit",
            )

        if await check_application_success(page):
            return ApplyResult(
                status=ApplyStatus.SUCCESS,
                message="Applied with cover letter" if message else "Applied via response modal",
            )

        try:
            still_open = await page.locator(
                'div[role="dialog"][aria-modal="true"]:visible'
            ).count() > 0
        except Exception:
            still_open = False

        if still_open:
            await dump_html(page, "response_modal_after_submit_still_open")
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message="Modal did not close after submit; possible validation error",
            )

        return ApplyResult(
            status=ApplyStatus.SUCCESS,
            message="Applied with cover letter" if message else "Applied via response modal",
        )


class PostApplyLetterStrategy(ApplyStrategy):
    """
    Handles legacy page formats where clicking standard apply button submits directly,
    and a cover letter textarea might optionally appear post-apply.
    """
    async def try_apply(
        self, page: Page, message: str, resume_id: str, af: AntiFraud
    ) -> Optional[ApplyResult]:
        # Wait a bit for any post-apply redirection or overlay
        await page.wait_for_timeout(2000)

        for sel in [
            "textarea[data-qa='vacancy-response-popup-form-letter-input']",
            "textarea[data-qa*='letter']",
            "textarea[data-qa*='cover']",
            "[data-qa='vacancy-response-popup'] textarea",
            ".vacancy-response-popup textarea",
            "textarea",
        ]:
            try:
                textarea = page.locator(sel)
                if await textarea.count() > 0 and await textarea.first.is_visible():
                    if not message:
                        # Textarea exists but we don't have a message to fill
                        return None

                    await af.pre_action_delay()
                    await textarea.first.fill(message)
                    logger.info(f"Cover letter filled via '{sel}'")

                    for btn_sel in [
                        "button[data-qa='vacancy-response-submit-popup']",
                        "button[data-qa*='submit']",
                        "button:has-text('Отправить')",
                        "button:has-text('Откликнуться')",
                    ]:
                        try:
                            btn = page.locator(btn_sel)
                            if await btn.count() > 0 and await btn.first.is_visible():
                                await af.pre_action_delay()
                                await btn.first.click()
                                await af.post_action_delay()
                                logger.info(f"Submit clicked via '{btn_sel}'")
                                return ApplyResult(
                                    status=ApplyStatus.SUCCESS,
                                    message="Applied with cover letter",
                                )
                        except Exception:
                            continue
            except Exception:
                continue

        logger.debug("No cover letter textarea found after apply")
        return None


# Helper function used by LegacyLinkApplyStrategy and DropdownApplyStrategy
async def fill_cover_letter_modal(
    page: Page,
    message: str,
    af: AntiFraud,
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

        submit_btn = page.locator("button[data-qa='vacancy-response-submit-popup']")
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


# Helper to select a resume on legacy pages (non-Magritte dialog)
async def select_legacy_resume(page: Page, resume_id: str) -> bool:
    if not resume_id:
        return True

    logger.debug(f"Attempting to select resume: {resume_id}")

    for sel in [
        'select[data-qa="resume-selector"]',
        'select[data-qa="resume-select"]',
        ".resume-selector select",
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
        ".resume-selector-popup",
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

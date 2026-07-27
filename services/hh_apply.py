import asyncio
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, Locator

from ..models import ApplyStatus, ApplyResult
from .browser import browser_manager
from .anti_fraud import AntiFraud

logger = logging.getLogger(__name__)


class HHApplyService:
    # ------------------------------------------------------------------ #
    # Diagnostics (same pattern as hh_auth.py, so dumps land in one place)
    # ------------------------------------------------------------------ #
    async def _safe_click(
        self, locator: Locator, tag: str, timeout: int = 4000
    ) -> bool:
        """
        Click that cannot hang forever and cannot be silently blocked by a
        stray overlay. Tries, in order: normal click, force click, raw JS
        click via element.click() (bypasses Playwright's actionability /
        hit-testing entirely). Each attempt is time-boxed, so worst case is
        bounded (~3 x timeout) instead of the default 30s per attempt.
        """
        el = locator.first
        try:
            await el.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        try:
            await el.click(timeout=timeout)
            return True
        except Exception as e:
            logger.debug(f"[{tag}] normal click failed: {e}")

        try:
            await el.click(timeout=timeout, force=True)
            return True
        except Exception as e:
            logger.debug(f"[{tag}] force click failed: {e}")

        try:
            await el.evaluate("(node) => node.click()")
            return True
        except Exception as e:
            logger.warning(f"[{tag}] JS click failed too: {e}")
            return False

    async def _force_close_stray_overlays(self, page: Page) -> None:
        """Close only a visible resume dropdown, never the response dialog itself."""
        dropdown = page.locator(
            '[role="listbox"]:visible, '
            '[data-qa="resume-form-resume-list"]:visible, '
            '[data-qa="resume-form-resume-popup"]:visible, '
            '[data-qa*="select-list"]:visible'
        )

        try:
            if await dropdown.count() == 0:
                return
        except Exception:
            return

        # Escape is safe only while an actual dropdown is visible. Calling it
        # unconditionally closes the HH response modal and leaves stale locators.
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
        except Exception:
            pass

    async def _dump_html(self, page: Page, tag: str) -> None:
        try:
            dump_dir = Path("/tmp/opencode")
            dump_dir.mkdir(parents=True, exist_ok=True)
            content = await page.content()
            filepath = dump_dir / f"hh_apply_{tag}.html"
            filepath.write_text(content, encoding="utf-8")
            logger.info(f"[{tag}] HTML saved to {filepath} ({len(content)} bytes)")
        except Exception as e:
            logger.debug(f"[{tag}] Could not dump HTML: {e}")

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
            if (
                "подтвердите, что вы не робот" in body_lower
                or "smartcaptcha" in body_lower
            ):
                logger.warning("Captcha detected by body text")
                return True

            return False
        except Exception as e:
            logger.debug(f"Captcha check error: {e}")
            return False

    async def _check_already_applied(self, page: Page) -> bool:
        locator = page.locator("text=Вы откликнулись")
        return await locator.count() > 0

    # ------------------------------------------------------------------ #
    # NEW: response modal (appears when account has >1 resume, or hh runs
    # the new Magritte-based "Отклик на вакансию" dialog). Confirmed from
    # a real DOM dump: div[role="dialog"][aria-modal="true"], button
    # data-qa="add-cover-letter" reveals the textarea, button
    # data-qa="vacancy-response-submit-popup" submits the form.
    # ------------------------------------------------------------------ #
    async def _handle_response_modal(
        self,
        page: Page,
        message: str,
        resume_id: str,
        af: AntiFraud,
    ) -> Optional[ApplyResult]:
        dialog = page.locator('div[role="dialog"][aria-modal="true"]:visible').last
        try:
            await dialog.wait_for(state="visible", timeout=5000)
        except Exception:
            return None

        logger.info("Response modal detected")
        await self._dump_html(page, "response_modal_open")

        # The actual response form is a much more stable anchor than the
        # generated Magritte classes and even more stable than the outer dialog.
        form = dialog.locator('form[name="vacancy_response"]').first
        if await form.count() == 0:
            form = page.locator('form[name="vacancy_response"]:visible').last

        # 1. Select the requested resume when a picker exists.
        if resume_id:
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

                if trigger is not None and await self._safe_click(trigger, "resume_trigger"):
                    await page.wait_for_timeout(400)
                    await self._dump_html(page, "response_modal_resume_dropdown")

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
                        if await self._safe_click(option, "resume_option"):
                            logger.info(
                                f"Resume '{resume_id}' selected in modal dropdown"
                            )
                        else:
                            logger.warning(
                                f"Resume option for '{resume_id}' was found but not clicked"
                            )
                    else:
                        logger.warning(
                            f"Resume '{resume_id}' was not found in the modal dropdown; "
                            "keeping the currently selected resume"
                        )

                    await self._force_close_stray_overlays(page)
                    await page.wait_for_timeout(300)

                    # HH may rerender the whole dialog after resume selection.
                    dialog = page.locator(
                        'div[role="dialog"][aria-modal="true"]:visible'
                    ).last
                    await dialog.wait_for(state="visible", timeout=3000)
                    form = dialog.locator('form[name="vacancy_response"]').first
                    if await form.count() == 0:
                        form = page.locator(
                            'form[name="vacancy_response"]:visible'
                        ).last
            except Exception as e:
                logger.warning(f"Resume selection in modal failed: {e}")

        # 2. Find an already-open letter textarea first. On the current HH UI
        # it can be present immediately and there may be no add-cover-letter button.
        letter_area: Optional[Locator] = None
        if message:
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
                        if await self._safe_click(add_button, "add_cover_letter"):
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
                    logger.warning(
                        "No visible cover-letter textarea found in response modal"
                    )
                    await self._dump_html(page, "response_modal_no_textarea")
            except Exception as e:
                logger.warning(f"Cover letter fill in modal failed: {e}")

        # 3. Submit. HH frequently rerenders the footer, so a Playwright
        # Locator captured earlier can become stale. Resolve and click the button
        # inside the browser at the exact moment of submission. Text is the main
        # fallback: the visible button literally contains "Откликнуться".
        try:
            await af.pre_action_delay()

            submit_result = await page.evaluate(
                """() => {
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

                    // requestSubmit is preferred because it runs native validation
                    // and the form submit handler. HH may keep the button in a footer
                    // outside the form, so fall back to a real DOM click.
                    const form = button.form || dialog.querySelector('form[name="vacancy_response"]');
                    if (form && typeof form.requestSubmit === 'function') {
                        try {
                            if (button.tagName === 'BUTTON' && button.type === 'submit' && button.form === form) {
                                form.requestSubmit(button);
                            } else {
                                form.requestSubmit();
                            }
                            return {ok: true, method: 'requestSubmit', text: normalize(button.innerText || button.textContent || button.value)};
                        } catch (_) {
                            // Continue to direct click.
                        }
                    }

                    button.click();
                    return {ok: true, method: 'dom-click', text: normalize(button.innerText || button.textContent || button.value)};
                }"""
            )

            if not submit_result.get("ok"):
                # A disabled button can become enabled after React finishes processing
                # the textarea input. Wait briefly and resolve it again by its text.
                try:
                    await page.wait_for_function(
                        """() => {
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
                    """() => {
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
                await self._dump_html(page, "response_modal_submit_fail")
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
            await self._dump_html(page, "response_modal_submit_exception")
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message=f"Modal submit failed: {e}",
            )

        # Wait for either modal close or an explicit success marker.
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

        if await self._check_bot_protection(page):
            af.captcha_detected()
            return ApplyResult(
                status=ApplyStatus.CAPTCHA,
                message="Captcha after modal submit",
            )

        if await self._check_application_success(page):
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
            await self._dump_html(page, "response_modal_after_submit_still_open")
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message="Modal did not close after submit; possible validation error",
            )

        return ApplyResult(
            status=ApplyStatus.SUCCESS,
            message="Applied with cover letter" if message else "Applied via response modal",
        )

    # ------------------------------------------------------------------ #
    # Legacy flow (kept as fallback for accounts/pages that don't show
    # the dialog above, e.g. a single-resume account).
    # ------------------------------------------------------------------ #
    async def _select_resume(self, page: Page, resume_id: str) -> bool:
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
                                logger.info(
                                    f"Selected resume from popup link: {resume_id}"
                                )
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        logger.debug("Resume selector not found, using default resume")
        return True

    async def _fill_cover_letter_modal(
        self,
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

    async def _try_cover_letter_link(
        self,
        page: Page,
        message: str,
        af: AntiFraud,
    ) -> Optional[ApplyResult]:
        cover_letter_link = page.locator("a:has-text('Написать сопроводительное')")
        if await cover_letter_link.count() > 0 and message:
            await af.pre_action_delay()
            await cover_letter_link.first.click()
            result = await self._fill_cover_letter_modal(page, message, af)
            if result:
                return result
        return None

    async def _try_dropdown_apply(
        self,
        page: Page,
        message: str,
        af: AntiFraud,
    ) -> Optional[ApplyResult]:
        dropdown_arrow = page.locator(
            "[data-qa='vacancy-response-link-top'] + button, "
            "[data-qa='vacancy-response-link-bottom'] + button"
        )
        if await dropdown_arrow.count() > 0 and message:
            await af.pre_action_delay()
            await dropdown_arrow.first.click()
            await page.wait_for_timeout(500)

            with_letter_option = page.locator("text=С сопроводительным письмом")
            if await with_letter_option.count() > 0:
                await with_letter_option.first.click()
                result = await self._fill_cover_letter_modal(page, message, af)
                if result:
                    return result
        return None

    async def _try_post_apply_letter(
        self,
        page: Page,
        message: str,
        af: AntiFraud,
    ) -> Optional[ApplyResult]:
        if not message:
            return None

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
            return ApplyResult(status=ApplyStatus.SKIPPED, message="Already applied")

        # Legacy path: separate "Написать сопроводительное" link before any click.
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

        # Legacy path: dropdown-style "apply with cover letter" arrow.
        result = await self._try_dropdown_apply(page, message, af)
        if result:
            logger.info(f"Dropdown strategy: {result.status.value}")
            return result

        await af.pre_action_delay()
        await apply_btn.first.click()
        await af.post_action_delay()
        logger.debug("Clicked standard apply button")

        # NEW: the current hh.ru flow - a dialog with resume picker +
        # "add cover letter" button + submit. Try this first now.
        # Hard ceiling so an unfamiliar UI variant in the future can never
        # again silently hang the whole scheduler for 30s+ per click - it
        # will surface as a clear ERROR with a dump instead.
        try:
            modal_result = await asyncio.wait_for(
                self._handle_response_modal(page, message, resume_id, af),
                timeout=40,
            )
        except asyncio.TimeoutError:
            logger.error("Response modal handling exceeded hard 40s timeout")
            await self._dump_html(page, "response_modal_hard_timeout")
            modal_result = ApplyResult(
                status=ApplyStatus.ERROR, message="Response modal handling timed out"
            )

        if modal_result:
            logger.info(f"Response modal strategy: {modal_result.status.value}")
            return modal_result

        # Fallback: legacy single-resume flow (no dialog appeared).
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


apply_service = HHApplyService()

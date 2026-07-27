import asyncio
import logging
import re
from dataclasses import dataclass
from contextlib import AsyncExitStack

from ..config import get_settings
from ..models.resume import HHResume
from .browser import browser_manager

logger = logging.getLogger(__name__)


class _LoginSession:
    __slots__ = ("exit_stack", "context", "page")

    def __init__(self, exit_stack: AsyncExitStack, context, page) -> None:
        self.exit_stack = exit_stack
        self.context = context
        self.page = page


class HHAuthService:
    def __init__(self) -> None:
        self._sessions: dict[int, _LoginSession] = {}

    async def _log_page_state(self, page, tag: str) -> None:
        try:
            url = page.url
            title = await page.title()
            logger.info(f"[{tag}] URL: {url}")
            logger.info(f"[{tag}] Title: {title}")
        except Exception as e:
            logger.debug(f"[{tag}] Could not read page state: {e}")

    async def _dump_inputs(self, page, tag: str) -> None:
        try:
            inputs = page.locator("input")
            count = await inputs.count()
            logger.info(f"[{tag}] Found {count} input elements:")
            for i in range(count):
                try:
                    inp = inputs.nth(i)
                    inp_type = await inp.get_attribute("type") or ""
                    inp_name = await inp.get_attribute("name") or ""
                    inp_qa = await inp.get_attribute("data-qa") or ""
                    inp_placeholder = await inp.get_attribute("placeholder") or ""
                    visible = await inp.is_visible()
                    logger.info(
                        f"  [{tag}] input[{i}]: type={inp_type} name={inp_name} "
                        f"qa={inp_qa} placeholder={inp_placeholder} visible={visible}"
                    )
                except Exception:
                    pass

            buttons = page.locator("button")
            btn_count = await buttons.count()
            logger.info(f"[{tag}] Found {btn_count} button elements:")
            for i in range(btn_count):
                try:
                    btn = buttons.nth(i)
                    btn_text = (await btn.inner_text()).strip()[:50]
                    btn_qa = await btn.get_attribute("data-qa") or ""
                    btn_type = await btn.get_attribute("type") or ""
                    visible = await btn.is_visible()
                    logger.info(
                        f"  [{tag}] button[{i}]: text='{btn_text}' qa={btn_qa} "
                        f"type={btn_type} visible={visible}"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[{tag}] Could not dump inputs: {e}")

    async def _dump_html(self, page, tag: str) -> None:
        try:
            from pathlib import Path
            dump_dir = Path("/tmp/opencode")
            dump_dir.mkdir(parents=True, exist_ok=True)
            content = await page.content()
            filepath = dump_dir / f"hh_page_{tag}.html"
            filepath.write_text(content, encoding="utf-8")
            logger.info(f"[{tag}] HTML saved to {filepath} ({len(content)} bytes)")
        except Exception as e:
            logger.debug(f"[{tag}] Could not dump HTML: {e}")

    async def start_login(self, user_id: int) -> tuple[bool, str]:
        try:
            settings = get_settings()
            settings.ensure_dirs()
            logger.info(f"=== Starting login for user {user_id} ===")

            exit_stack = AsyncExitStack()
            context, page = await exit_stack.enter_async_context(
                browser_manager.get_interactive_context(headless=True)
            )
            logger.info("Browser context opened")

            logger.info("Navigating to hh.ru/login...")
            await page.goto("https://hh.ru/login", wait_until="domcontentloaded", timeout=30000)
            await self._log_page_state(page, "AFTER_GOTO")
            await self._dump_inputs(page, "LOGIN_PAGE")
            await self._dump_html(page, "initial_load")

            try:
                radio = await page.wait_for_selector(
                    'input[data-qa="account-type-card-APPLICANT"]', timeout=5000
                )
                if not await radio.is_checked():
                    await radio.check()
                    logger.info("Selected APPLICANT account type")
                await asyncio.sleep(1)
            except Exception:
                logger.debug("No applicant radio found, APPLICANT may already be selected")

            try:
                submit = await page.wait_for_selector(
                    'button[data-qa="submit-button"]', timeout=5000
                )
                await submit.click()
                logger.info("Clicked initial submit button")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Failed to click initial submit: {e}")
                await self._dump_inputs(page, "SUBMIT_FAIL")
                await self._cleanup(user_id)
                return False, f"Failed to open login form: {e}"

            await self._log_page_state(page, "AFTER_SUBMIT")
            await self._dump_inputs(page, "AFTER_SUBMIT")
            await self._dump_html(page, "after_submit")

            self._sessions[user_id] = _LoginSession(exit_stack, context, page)
            logger.info("Login session created, waiting for credential input")
            return True, "Enter your HH.ru email or phone number:"

        except Exception as e:
            logger.error(f"Login start failed: {e}", exc_info=True)
            await self._cleanup(user_id)
            return False, f"Login error: {e}"

    async def submit_credential(self, user_id: int, credential: str) -> tuple[bool, str]:
        session = self._sessions.get(user_id)
        if not session:
            return False, "Session expired. Start again from Login HH."

        page = session.page
        credential = credential.strip()
        is_phone = (
            credential.startswith("+")
            or (
                "@" not in credential
                and credential.replace(" ", "").replace("-", "").isdigit()
            )
        )

        logger.info(f"=== Submitting credential ===")
        logger.info(f"  Type: {'PHONE' if is_phone else 'EMAIL'}")
        logger.info(f"  Value: {credential[:3]}{'***' if len(credential) > 3 else ''}")

        await self._log_page_state(page, "BEFORE_CRED")
        await self._dump_inputs(page, "BEFORE_CRED")

        try:
            if is_phone:
                return await self._fill_phone(page, user_id, credential)
            else:
                return await self._fill_email(page, user_id, credential)

        except Exception as e:
            logger.error(f"Login credential failed: {e}", exc_info=True)
            await self._cleanup(user_id)
            return False, f"Login error: {e}"

    async def _fill_phone(self, page, user_id: int, credential: str) -> tuple[bool, str]:
        phone = credential.replace("+", "").replace(" ", "").replace("-", "")
        if phone.startswith("7") and len(phone) > 10:
            phone = phone[1:]

        phone_field = None
        for sel in [
            'input[data-qa="magritte-phone-input-national-number-input"]',
            'input[name="phone"]',
            'input[type="tel"]',
            'input[placeholder*="телефон"]',
            'input[placeholder*="phone"]',
        ]:
            try:
                phone_field = await page.wait_for_selector(sel, timeout=3000)
                if phone_field and await phone_field.is_visible():
                    logger.info(f"Phone field found via: {sel}")
                    break
                phone_field = None
            except Exception:
                continue

        if not phone_field:
            logger.error("Phone input field not found!")
            await self._dump_inputs(page, "PHONE_FAIL")
            await self._cleanup(user_id)
            return False, "Phone input field not found"

        await phone_field.fill(phone)
        logger.info(f"Phone filled")

        return await self._submit_and_wait_otp(page, user_id)

    async def _fill_email(self, page, user_id: int, credential: str) -> tuple[bool, str]:
        email_radio = await page.wait_for_selector(
            'input[data-qa="credential-type-email"]', timeout=5000
        )
        if email_radio:
            await email_radio.click(force=True)
            logger.info("Clicked email radio with force=True")
            await asyncio.sleep(3)
            await self._dump_inputs(page, "AFTER_EMAIL_TAB")
            await self._dump_html(page, "after_email_tab")

        filled = False
        for sel in [
            'input[data-qa*="email"][type="text"]',
            'input[data-qa*="email"]:not([type="radio"])',
            'input[data-qa="applicant-login-input-email"]',
            'input[data-qa*="login-input"]',
            'input[data-qa*="credential-input"]',
            'input[type="email"]',
            'input[type="text"][name="login"]',
            'input[type="text"][name="email"]',
            'input[name="login"]',
            'input[name="email"]',
            'input[placeholder*="mail"]',
            'input[placeholder*="почт"]',
            'input[placeholder*="логин"]',
        ]:
            try:
                fields = page.locator(sel)
                count = await fields.count()
                for idx in range(count):
                    field = fields.nth(idx)
                    visible = await field.is_visible()
                    enabled = await field.is_enabled()
                    inp_type = await field.get_attribute("type") or ""
                    if inp_type == "radio":
                        continue
                    if visible and enabled:
                        await field.fill(credential)
                        filled = True
                        logger.info(f"Email filled via '{sel}'")
                        break
                if filled:
                    break
            except Exception:
                continue

        if not filled:
            logger.error("Could not find email input field")
            await self._dump_html(page, "email_fill_fail")
            await self._cleanup(user_id)
            return False, "Could not find email input field"

        return await self._submit_and_wait_otp(page, user_id)

    async def _submit_and_wait_otp(self, page, user_id: int) -> tuple[bool, str]:
        submit_btn = None
        for sel in [
            'button[data-qa="submit-button"]',
            'button[type="submit"]',
            'button:has-text("Дальше")',
            'button:has-text("Войти")',
            'button:has-text("Далее")',
        ]:
            try:
                submit_btn = await page.wait_for_selector(sel, timeout=3000)
                if submit_btn and await submit_btn.is_visible():
                    logger.info(f"Submit button found via: {sel}")
                    break
                submit_btn = None
            except Exception:
                continue

        if submit_btn:
            await submit_btn.click()
            logger.info("Submit clicked, waiting for response...")
        else:
            logger.error("Submit button not found!")
            await self._dump_inputs(page, "NO_SUBMIT")
            await self._cleanup(user_id)
            return False, "Submit button not found"

        await asyncio.sleep(4)
        await self._log_page_state(page, "AFTER_SUBMIT_CRED")
        await self._dump_inputs(page, "AFTER_SUBMIT_CRED")

        for sel in [
            'input[data-qa="otp-code-input"]',
            'input[data-qa*="otp"]',
            'input[data-qa*="code"]',
            'input[inputmode="numeric"]',
            'input[name="code"]',
            'input[placeholder*="код"]',
        ]:
            try:
                otp_field = await page.wait_for_selector(sel, timeout=5000)
                if otp_field and await otp_field.is_visible():
                    logger.info(f"OTP field detected via: {sel}")
                    return True, "Enter the OTP code sent to you:"
            except Exception:
                continue

        page_text = await page.locator("body").inner_text()
        logger.warning(f"OTP field not found. Page text (first 500): {page_text[:500]}")
        return False, "OTP field not detected. Check if credential was accepted."

    async def submit_otp(self, user_id: int, otp: str) -> tuple[bool, str]:
        session = self._sessions.get(user_id)
        if not session:
            return False, "Session expired. Start again from Login HH."

        page = session.page
        otp = otp.strip()
        logger.info(f"=== Submitting OTP ===")

        await self._log_page_state(page, "BEFORE_OTP")

        try:
            otp_field = None
            for sel in [
                'input[data-qa="otp-code-input"]',
                'input[data-qa*="otp"]',
                'input[data-qa*="code"]',
                'input[inputmode="numeric"]',
                'input[name="code"]',
                'input[placeholder*="код"]',
            ]:
                try:
                    otp_field = await page.wait_for_selector(sel, timeout=3000)
                    if otp_field and await otp_field.is_visible():
                        logger.info(f"OTP field found via: {sel}")
                        break
                    otp_field = None
                except Exception:
                    continue

            if not otp_field:
                logger.error("OTP field not found!")
                await self._dump_inputs(page, "OTP_FAIL")
                await self._cleanup(user_id)
                return False, "OTP field not found"

            await otp_field.fill(otp)
            await asyncio.sleep(1)

            submit_btn = None
            for btn_sel in [
                'button[data-qa="submit-button"]',
                'button[type="submit"]',
                'button:has-text("Дальше")',
                'button:has-text("Войти")',
                'button:has-text("Далее")',
            ]:
                try:
                    btn = await page.wait_for_selector(btn_sel, timeout=3000)
                    if btn and await btn.is_visible():
                        submit_btn = btn
                        break
                except Exception:
                    continue

            if submit_btn:
                await submit_btn.click()
                logger.info("OTP submit clicked")

            await asyncio.sleep(5)
            await self._log_page_state(page, "AFTER_OTP")

            cookies = await session.context.cookies()
            hhtoken = any(c["name"] == "hhtoken" for c in cookies)

            if hhtoken:
                await session.context.storage_state(
                    path=str(get_settings().session_file)
                )
                logger.info(f"Session saved to {get_settings().session_file}")

                account_name = await self._get_account_name(page)
                await self._cleanup(user_id)

                if account_name:
                    return True, f"Login successful!\nAccount: {account_name}\nSession saved."
                return True, "Login successful! Session saved."
            else:
                page_text = await page.locator("body").inner_text()
                logger.error(f"hhtoken not found. Page text (first 500): {page_text[:500]}")
                await self._cleanup(user_id)
                return False, "hhtoken not found. Login may have failed."

        except Exception as e:
            logger.error(f"Login OTP failed: {e}", exc_info=True)
            await self._cleanup(user_id)
            return False, f"Login error: {e}"

    async def _get_account_name(self, page) -> str:
        try:
            name = await page.evaluate(
                "() => { try { return window.globalVars && window.globalVars.login; } catch(e) { return null; } }"
            )
            if name:
                return name

            await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            name = await page.evaluate(
                "() => { try { return window.globalVars && window.globalVars.login; } catch(e) { return null; } }"
            )
            if name:
                return name

            name = await page.evaluate(
                "() => { try { const el = document.querySelector('[data-qa=\"header__user-name\"]'); return el ? el.textContent.trim() : null; } catch(e) { return null; } }"
            )
            return name or ""

        except Exception as e:
            logger.warning(f"Failed to get account name: {e}")
            return ""

    async def get_resumes(self) -> list[HHResume]:
        settings = get_settings()
        if not settings.session_file.exists():
            return []

        resumes: list[HHResume] = []
        try:
            async with browser_manager.get_page(use_session=True) as page:
                await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)

                has_captcha = await page.evaluate(
                    "() => { try { return document.title.toLowerCase().includes('captcha') || document.title.toLowerCase().includes('robot'); } catch(e) { return false; } }"
                )
                if has_captcha:
                    return []

                data = await page.evaluate("""
                    async () => {
                        try {
                            const resp = await fetch('/shards/applicant/resumes', {
                                credentials: 'same-origin',
                                headers: { 'Accept': 'application/json' }
                            });
                            if (!resp.ok) return { error: 'status=' + resp.status };
                            return await resp.json();
                        } catch(e) {
                            return { error: 'fetch_exception: ' + e.message };
                        }
                    }
                """)

                if isinstance(data, dict):
                    if data.get('error'):
                        return []
                    items = data.get('items', data.get('resumes', []))
                elif isinstance(data, list):
                    items = data
                else:
                    items = []

                seen_ids = set()
                for item in items:
                    if isinstance(item, dict):
                        rid = item.get('id', '')
                        if rid and rid not in seen_ids:
                            seen_ids.add(rid)
                            title = item.get('title', '') or f"Resume {len(resumes)+1}"
                            status = item.get('status', 'active') or 'active'
                            resumes.append(HHResume(id=rid, title=title, status=status))

        except Exception as e:
            logger.error(f"Failed to fetch resumes: {e}", exc_info=True)

        return resumes

    async def get_resume_text(self, resume_id: str) -> str:
        """Delegate to hh_resume service."""
        from .hh_resume import fetch_resume_text
        return await fetch_resume_text(resume_id)

    async def logout(self) -> bool:
        settings = get_settings()
        session_file = settings.session_file
        if session_file.exists():
            session_file.unlink()
            logger.info(f"Session file deleted: {session_file}")
            return True
        return False

    async def cancel_login(self, user_id: int) -> None:
        await self._cleanup(user_id)

    async def _cleanup(self, user_id: int) -> None:
        session = self._sessions.pop(user_id, None)
        if session:
            try:
                await session.exit_stack.aclose()
            except Exception:
                pass

    def session_exists(self) -> bool:
        return get_settings().session_file.exists()


hh_auth = HHAuthService()

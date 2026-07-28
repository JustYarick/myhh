import logging
from pathlib import Path
from playwright.async_api import Page, Locator

logger = logging.getLogger(__name__)


async def safe_click(locator: Locator, tag: str, timeout: int = 4000) -> bool:
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


async def force_close_stray_overlays(page: Page) -> None:
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


async def dump_html(page: Page, tag: str) -> None:
    try:
        dump_dir = Path("/tmp/opencode")
        dump_dir.mkdir(parents=True, exist_ok=True)
        content = await page.content()
        filepath = dump_dir / f"hh_apply_{tag}.html"
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"[{tag}] HTML saved to {filepath} ({len(content)} bytes)")
    except Exception as e:
        logger.debug(f"[{tag}] Could not dump HTML: {e}")


async def check_bot_protection(page: Page) -> bool:
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

import asyncio
import json
import logging

from ..config import get_settings
from ..constants import (
    HHSelectors,
    PAGE_LOAD_TIMEOUT_MS,
    ELEMENT_WAIT_TIMEOUT_MS,
    ELEMENT_SHORT_TIMEOUT_MS,
    POST_CLICK_SLEEP_S,
    POST_NAV_SLEEP_S,
    RESUME_ALREADY_RAISED_MARKERS,
)
from ..models.resume import ResumeData
from .browser import browser_manager as default_browser_manager

logger = logging.getLogger(__name__)

RESUME_JS_PARSER = """
() => {
    const result = {
        title: '',
        skills: [],
        experience: [],
        education: [],
        courses: [],
        contacts: {},
        preferences: {},
        about: ''
    };

    // helper: strictly match data-qa == prefix + digits (no extra suffix like -org/-name)
    function findExactIndexedItems(prefix) {
        const items = [];
        const re = new RegExp('^' + prefix + '-\\\\d+$');
        document.querySelectorAll('[data-qa]').forEach(el => {
            const qa = el.getAttribute('data-qa');
            if (qa && re.test(qa)) items.push(el);
        });
        return items;
    }

    try {
        const h1 = document.querySelector('h1[data-qa="resume-block-title-position"]');
        if (h1) result.title = h1.innerText.trim();

        // Skills
        document.querySelectorAll('[data-qa^="skill-tag-"]').forEach(tag => {
            const t = tag.innerText.trim();
            if (t) result.skills.push(t);
        });

        // Experience
        document.querySelectorAll('[data-qa="profile-experience-company-card"]').forEach(card => {
            const companyCells = Array.from(
                card.querySelectorAll('[data-qa="cell-text-content"]')
            ).filter(el => {
                return !el.closest('[data-qa="magritte-stepper-step-content"]');
            });

            const company = companyCells[0]?.innerText.trim() || '';
            const companyPeriod = companyCells[1]?.innerText.trim() || '';

            const steps = card.querySelectorAll(
                '[data-qa="magritte-stepper-step-content"]'
            );

            steps.forEach(step => {
                const cells = step.querySelectorAll(
                    '[data-qa="cell-text-content"]'
                );

                const position = cells[0]?.innerText.trim() || '';
                const period = cells[1]?.innerText.trim() || companyPeriod;

                const clone = step.cloneNode(true);

                clone.querySelectorAll(
                    '[data-qa="cell-text-content"], button, svg'
                ).forEach(el => el.remove());

                const description = clone.innerText.trim();

                result.experience.push({
                    company,
                    period,
                    position,
                    description
                });
            });
        });

        // Education (strict indexed match to avoid catching nested sub-fields)
        findExactIndexedItems('resume-list-card-education-item').forEach(item => {
            const cells = item.querySelectorAll('[data-qa="cell-text-content"]');
            const texts = [];
            cells.forEach(c => { const t = c.innerText.trim(); if (t) texts.push(t); });
            if (texts.length > 0) {
                result.education.push({
                    university: texts[0] || '',
                    faculty: texts[1] || '',
                    year_degree: texts[2] || ''
                });
            }
        });

        // Courses (strict indexed match — this is what fixes the duplicate/garbled entries)
        findExactIndexedItems('resume-list-card-additionalEducation-item').forEach(item => {
            const cells = item.querySelectorAll('[data-qa="cell-text-content"]');
            const texts = [];
            cells.forEach(c => { const t = c.innerText.trim(); if (t) texts.push(t); });
            if (texts.length > 0) {
                result.courses.push({
                    org: texts[0] || '',
                    name: texts[1] || '',
                    year: texts[2] || ''
                });
            }
        });

        // About
        const aboutCard = document.querySelector('[data-qa="resume-about-card"]');
        if (aboutCard) {
            const el = aboutCard.querySelector('[data-qa="cell-text-content"]');
            if (el) {
                const t = el.innerText.trim();
                // Filter out hh.ru's own placeholder text for empty "about" field
                if (t && t.toLowerCase() !== 'обо мне') result.about = t;
            }
        }

        // Contacts
        const phoneEl = document.querySelector('[data-qa="resume-contact-phone-value-preferred"]');
        if (phoneEl) result.contacts.phone = phoneEl.innerText.trim();
        const emailEl = document.querySelector('[data-qa="resume-contact-email-value"]');
        if (emailEl) result.contacts.email = emailEl.innerText.trim();

        // Preferences
        const empEl = document.querySelector('[data-qa="resume-position-field-employmentForms"]');
        if (empEl) {
            const val = empEl.innerText.trim().replace(/^Тип занятости:\\s*/, '');
            if (val) result.preferences.employment = val;
        }
        const workEl = document.querySelector('[data-qa="resume-position-field-workFormats"]');
        if (workEl) {
            const val = workEl.innerText.trim().replace(/^Формат работы:\\s*/, '');
            if (val) result.preferences.work_format = val;
        }
        const travelEl = document.querySelector('[data-qa="resume-position-field-travelTime"]');
        if (travelEl) {
            const val = travelEl.innerText.trim().replace(/^Желательное время в пути до работы:\\s*/, '');
            if (val) result.preferences.travel_time = val;
        }
        const bizEl = document.querySelector('[data-qa="resume-position-field-businessTripReadiness"]');
        if (bizEl) {
            const val = bizEl.innerText.trim().replace(/^Командировки:\\s*/, '');
            if (val) result.preferences.business_trips = val;
        }
        const salaryEl = document.querySelector('[data-qa="resume-block-salary"]');
        if (salaryEl) {
            const val = salaryEl.innerText.trim();
            if (val && !val.includes('не указан')) result.preferences.salary = val;
        }

    } catch(e) { result._error = e.message; }

    return result;
}
"""


def _parse_raw_to_data(raw: dict) -> ResumeData:
    """Convert raw JS parser output to ResumeData model."""
    return ResumeData(
        title=raw.get("title", ""),
        skills=raw.get("skills", []),
        experience=raw.get("experience", []),
        education=raw.get("education", []),
        courses=raw.get("courses", []),
        contacts=raw.get("contacts", {}),
        preferences=raw.get("preferences", {}),
        about=raw.get("about", ""),
    )


class HHResumeService:
    def __init__(self, browser_manager=None) -> None:
        self.browser_manager = browser_manager or default_browser_manager

    async def parse_resume_page(self, page) -> ResumeData:
        """Execute JS parser on a Playwright page and return structured ResumeData."""
        raw = await page.evaluate(RESUME_JS_PARSER)

        if raw.get("_error"):
            logger.error(f"Resume JS parser error: {raw['_error']}")

        data = _parse_raw_to_data(raw)

        logger.info(
            f"Resume parsed: title={data.title!r} "
            f"skills={len(data.skills)} "
            f"experience={len(data.experience)} "
            f"education={len(data.education)} "
            f"courses={len(data.courses)} "
            f"about={len(data.about)}"
        )
        logger.debug(f"[RESUME JSON]\n{json.dumps(raw, ensure_ascii=False, indent=2)}")

        return data

    async def fetch_resume_text(self, resume_id: str) -> str:
        """Navigate to resume page, parse it, return formatted text for Gemini."""
        settings = get_settings()
        if not settings.session_file.exists():
            logger.warning("No session file, cannot fetch resume text")
            return ""

        try:
            async with self.browser_manager.get_page(use_session=True) as page:
                url = f"https://hh.ru/resume/{resume_id}"
                logger.info(f"Fetching resume text: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

                for sel in [HHSelectors.RESUME_CONTENT_MAGRITTE, 'h1', HHSelectors.RESUME_NAME, HHSelectors.RESUME_CONTENT]:
                    try:
                        await page.wait_for_selector(sel, timeout=ELEMENT_WAIT_TIMEOUT_MS)
                        logger.debug(f"Found element: {sel}")
                        break
                    except Exception:
                        continue

                await asyncio.sleep(2)

                page_url = page.url
                page_title = await page.title()
                logger.info(f"Resume page: URL={page_url} title={page_title}")

                has_captcha = await page.evaluate(HHSelectors.CAPTCHA_JS)
                if has_captcha:
                    logger.warning("CAPTCHA detected on resume page")
                    return ""

                data = await self.parse_resume_page(page)
                resume_text = data.to_text()

                logger.debug(f"[RESUME STRUCTURED]\n{'='*60}\n{resume_text}\n{'='*60}")
                return resume_text

        except Exception as e:
            logger.error(f"Failed to fetch resume text: {e}")
            return ""

    async def publish_resume(self, resume_id: str) -> tuple[bool, str]:
        """Navigate to the resume page on HH.ru and click the 'Update date' button."""
        settings = get_settings()
        if not settings.session_file.exists():
            logger.warning("No session file, cannot publish resume")
            return False, "Нет файла сессии"

        try:
            async with self.browser_manager.get_page(use_session=True) as page:
                url = f"https://hh.ru/resume/{resume_id}"
                logger.info(f"Navigating to resume page to raise: {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                except Exception as navigation_err:
                    logger.warning(f"Navigation warning (non-fatal): {navigation_err}")
                await asyncio.sleep(POST_NAV_SLEEP_S)

                has_captcha = await page.evaluate(HHSelectors.CAPTCHA_JS)
                if has_captcha:
                    logger.warning("CAPTCHA detected on resume page during publish")
                    return False, "Обнаружена капча"

                btn = page.locator(HHSelectors.RESUME_UPDATE_BUTTON)
                if await btn.count() > 0:
                    is_visible = await btn.is_visible()
                    if not is_visible:
                        return False, "Кнопка поднятия найдена, но скрыта"

                    is_disabled = await btn.get_attribute("disabled")
                    if is_disabled is not None:
                        return False, "Уже поднято (кнопка заблокирована)"

                    await btn.click()
                    logger.info(f"Clicked raise button for resume {resume_id}")
                    await asyncio.sleep(POST_CLICK_SLEEP_S)

                    is_disabled_after = None
                    try:
                        is_disabled_after = await btn.get_attribute("disabled", timeout=ELEMENT_SHORT_TIMEOUT_MS)
                    except Exception:
                        pass

                    btn_text = ""
                    try:
                        btn_text = await btn.inner_text(timeout=ELEMENT_SHORT_TIMEOUT_MS)
                    except Exception:
                        pass

                    if (
                        is_disabled_after is not None 
                        or "обновлено" in btn_text.lower() 
                        or "поднимать" in btn_text.lower() 
                        or "подминать" in btn_text.lower()
                    ):
                        return True, "Успешно поднято"

                    content = ""
                    try:
                        content = await page.content()
                    except Exception:
                        pass

                    if (
                        "вы сможете обновить" in content.lower() 
                        or "можно обновить через" in content.lower() 
                        or "поднимать автоматически" in content.lower() 
                        or "подминать" in content.lower()
                    ):
                        return True, "Успешно поднято"

                    return True, "Кнопка нажата"
                else:
                    content = await page.content()
                    content_lower = content.lower()
                    if any(marker in content_lower for marker in RESUME_ALREADY_RAISED_MARKERS):
                        return False, "Уже поднято ранее"

                    buttons = page.locator('button')
                    for i in range(await buttons.count()):
                        btn_text = await buttons.nth(i).inner_text()
                        if "обновить" in btn_text.lower():
                            is_disabled = await buttons.nth(i).get_attribute("disabled")
                            if is_disabled is not None:
                                return False, "Уже поднято (кнопка заблокирована)"
                            await buttons.nth(i).click()
                            await asyncio.sleep(POST_CLICK_SLEEP_S)
                            return True, "Кнопка поднятия найдена по тексту и нажата"

                    try:
                        from .notification_service import notify_photo
                        screenshot_bytes = await page.screenshot(full_page=True)
                        await notify_photo(screenshot_bytes, f"⚠️ Ошибка автоподнятия резюме {resume_id}: Кнопка поднятия не найдена")
                    except Exception as screenshot_err:
                        logger.error(f"Failed to take/send error screenshot: {screenshot_err}")

                    return False, "Кнопка поднятия не найдена"

        except Exception as e:
            logger.error(f"Failed to raise resume {resume_id}: {e}", exc_info=True)
            try:
                from .notification_service import notify_photo
                screenshot_bytes = await page.screenshot(full_page=True)
                await notify_photo(screenshot_bytes, f"⚠️ Ошибка автоподнятия резюме {resume_id}: {str(e)}")
            except Exception as screenshot_err:
                logger.error(f"Failed to take/send error screenshot on exception: {screenshot_err}")
            return False, f"Ошибка: {str(e)}"


resume_service = HHResumeService()

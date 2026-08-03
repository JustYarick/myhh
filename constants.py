from pathlib import Path

# ---------------------------------------------------------------------------
# Timeouts (milliseconds unless noted)
# ---------------------------------------------------------------------------
PAGE_LOAD_TIMEOUT_MS: int = 20_000
PAGE_NAV_TIMEOUT_MS: int = 15_000
ELEMENT_WAIT_TIMEOUT_MS: int = 5_000
ELEMENT_SHORT_TIMEOUT_MS: int = 2_000

# Seconds (for asyncio.sleep)
POST_CLICK_SLEEP_S: float = 3.0
POST_NAV_SLEEP_S: float = 1.0
POST_RESUME_NAV_SLEEP_S: float = 1.0

# Scheduler / daemon
SCHEDULER_RUN_TIMEOUT_S: float = 180.0
DAEMON_ERROR_SLEEP_S: float = 10.0

# Monitoring intervals (minutes)
MONITORING_INTERVAL_DEFAULT_MIN: int = 30
RESUME_UPDATE_INTERVAL_MIN_MIN: int = 240  # 4 hours – HH.ru hard limit

# ---------------------------------------------------------------------------
# HH.ru selectors
# ---------------------------------------------------------------------------
class HHSelectors:
    # Resume page
    RESUME_UPDATE_BUTTON: str = "[data-qa='resume-update-button']"
    RESUME_NAME: str = "[data-qa='resume-personal-name']"
    RESUME_CONTENT_MAGRITTE: str = "[class*='magritte-v-spacing-container']"
    RESUME_CONTENT: str = ".resume-content"

    # Vacancy search
    VACANCY_CARD: str = "[data-qa='vacancy-serp__vacancy']"
    VACANCY_LINK: str = "[data-qa='serp-item__title']"
    VACANCY_EMPLOYER: str = "[data-qa='vacancy-serp__vacancy-employer']"

    # Apply form
    APPLY_BUTTON: str = "[data-qa='vacancy-response-link-top']"
    COVER_LETTER_TEXTAREA: str = "[data-qa='vacancy-response-popup-form-letter-input']"
    COVER_SUBMIT_BUTTON: str = "[data-qa='vacancy-response-submit-popup']"

    # Auth
    LOGIN_PHONE_INPUT: str = "[data-qa='login-input-username']"
    LOGIN_SUBMIT: str = "[data-qa='login-submit']"
    OTP_INPUT: str = "[data-qa='login-input-otp']"

    # Generic captcha
    CAPTCHA_JS: str = (
        "() => { try { return document.title.toLowerCase().includes('captcha')"
        " || document.title.toLowerCase().includes('robot'); } catch(e) { return false; } }"
    )

# ---------------------------------------------------------------------------
# Already-raised resume markers
# ---------------------------------------------------------------------------
RESUME_ALREADY_RAISED_MARKERS: tuple[str, ...] = (
    "вы сможете обновить",
    "можно обновить через",
    "поднимать автоматически",
)

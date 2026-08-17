"""
HH.ru vacancy apply service — uses the mobile REST API.

Replaces the previous Playwright-based strategy chain with a single
POST /negotiations call.  The public interface (``apply_service``,
``apply``) is kept identical to the old implementation.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..models import ApplyStatus, ApplyResult
from .hh_api_client import (
    hh_api,
    HHApiError,
    HHNegotiationsLimitExceeded,
    HHCaptchaRequired,
    HHForbidden,
    HHNotFound,
)

logger = logging.getLogger(__name__)

# AntiFraud is kept in the signature for backward compat; delays are
# now handled at a higher level (Scheduler._run_loop).
from .anti_fraud import AntiFraud


class HHApplyService:
    """Apply to a vacancy using the HH.ru REST API (POST /negotiations)."""

    async def apply(
        self,
        url: str,
        message: str = "",
        *,
        af: Optional[AntiFraud] = None,
        resume_id: str = "",
        # Kept for backward compat; ignored (no browser used)
        existing_page=None,
    ) -> ApplyResult:
        """
        Apply to the vacancy at *url* using the mobile API.

        Parameters
        ----------
        url:
            Canonical web URL of the vacancy, e.g.
            ``https://hh.ru/vacancy/12345678``.  The vacancy ID is
            extracted automatically.
        message:
            Cover letter text.
        resume_id:
            HH resume identifier (hash string).  Required.
        af, existing_page:
            Accepted for backward-compat; not used.

        Returns
        -------
        ApplyResult
            ``SUCCESS``  — application was sent.
            ``SKIPPED``  — already applied (API reported it).
            ``ERROR``    — unrecoverable error.
            ``CAPTCHA``  — HH asked for captcha (very rare via API).
        """
        if not resume_id:
            # Try to load the resume_id from the active flow as a last resort
            try:
                from .flow_entity import get_active_flow
                flow = await get_active_flow()
                if flow and flow.config.resume_id:
                    resume_id = flow.config.resume_id
            except Exception:
                pass

        if not resume_id:
            logger.error("apply called without resume_id for %s", url)
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message="resume_id is required for API apply",
            )

        vacancy_id = self._extract_vacancy_id(url)
        if not vacancy_id:
            logger.error("Cannot extract vacancy id from url: %s", url)
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message=f"Cannot extract vacancy ID from URL: {url}",
            )

        logger.info("Applying to vacancy %s (resume=%s)", vacancy_id, resume_id)

        # Ensure we have a valid API token
        if not hh_api.is_authenticated:
            await hh_api.load_token()
        if not hh_api.is_authenticated:
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message="HH API token not available. Please re-login via 'Авторизация HH (API)'.",
            )

        try:
            await hh_api.apply_to_vacancy(
                vacancy_id=vacancy_id,
                resume_id=resume_id,
                message=message,
            )
            logger.info("Applied successfully to vacancy %s", vacancy_id)
            return ApplyResult(status=ApplyStatus.SUCCESS, message="Applied via API")

        except HHNegotiationsLimitExceeded as e:
            logger.warning("Daily negotiations limit reached: %s", e)
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message="Достигнут суточный лимит откликов на HH.ru. Попробуйте завтра.",
            )

        except HHCaptchaRequired as e:
            logger.warning("Captcha required by HH API: %s", e.captcha_url)
            return ApplyResult(
                status=ApplyStatus.CAPTCHA,
                message=f"Captcha required: {e.captcha_url}",
            )

        except HHForbidden as e:
            msg = str(e)
            errors = (e.data.get("errors") or []) if isinstance(e.data, dict) else []
            for err in errors:
                if err.get("value") in ("already_applied", "already-applied"):
                    logger.info("Already applied to vacancy %s", vacancy_id)
                    return ApplyResult(status=ApplyStatus.SKIPPED, message="Already applied")
                if err.get("value") == "test_required":
                    logger.info("Test required for vacancy %s — skipping", vacancy_id)
                    return ApplyResult(status=ApplyStatus.ANALYZED_SKIP, message="requires_questions")
            logger.warning("Forbidden when applying to %s: %s", vacancy_id, msg)
            return ApplyResult(status=ApplyStatus.ERROR, message=f"Forbidden: {msg}")

        except HHNotFound:
            logger.warning("Vacancy %s not found (404)", vacancy_id)
            return ApplyResult(
                status=ApplyStatus.SKIPPED,
                message="Vacancy not found or archived",
            )

        except HHApiError as e:
            logger.error("API error applying to %s: %s", vacancy_id, e)
            # Check for "already applied" in error body
            errors = (e.data.get("errors") or []) if isinstance(e.data, dict) else []
            for err in errors:
                if err.get("value") in ("already_applied", "already-applied"):
                    return ApplyResult(status=ApplyStatus.SKIPPED, message="Already applied")
            return ApplyResult(
                status=ApplyStatus.ERROR,
                message=f"API error {e.status}: {e.data}",
            )

        except Exception as e:
            logger.error("Unexpected error applying to %s: %s", vacancy_id, e, exc_info=True)
            return ApplyResult(status=ApplyStatus.ERROR, message=str(e))

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_vacancy_id(url: str) -> str:
        import re
        m = re.search(r"/vacancy/(\d+)", url)
        if m:
            return m.group(1)
        # URL might already be a bare id
        if url.isdigit():
            return url
        return ""


apply_service = HHApplyService()

"""
HH.ru vacancy search service — uses the mobile REST API.

Replaces the previous Playwright-based scraping approach with clean
API calls via :mod:`hh_api_client`.

The public interface (``search_service``, ``prepare_search_url``,
``search_cards``, ``get_vacancy_description``) is kept **identical** to
the old implementation so that :mod:`scheduler.runner` needs no changes.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from ..models import Vacancy
from .anti_fraud import AntiFraud
from .hh_api_client import hh_api, HHApiError, HHNotFound

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def prepare_search_url(url: str) -> str:
    """Ensure the search URL has ``order_by=publication_time``."""
    if not url:
        return ""
    if "order_by=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}order_by=publication_time"
    return url


def _url_to_api_params(search_url: str, page_num: int = 0) -> dict:
    """
    Convert a hh.ru web search URL (with query-string filters) into a
    dict of parameters suitable for GET /vacancies.

    Keys that HH web uses are mostly the same as the API, except:
    - ``items_on_page``  → ``per_page``   (we force 20)
    - ``page``           → ``page``       (0-based, same)
    """
    parsed = urlparse(search_url)
    qs: dict[str, list[str]] = parse_qs(parsed.query, keep_blank_values=False)

    params: dict[str, Any] = {}

    # Copy through all params that the HH API accepts
    _pass_through = {
        "text", "area", "professional_role", "industry", "employer_id",
        "experience", "employment", "schedule", "work_format",
        "salary", "currency", "only_with_salary",
        "period", "date_from", "date_to",
        "label", "order_by",
        "top_lat", "bottom_lat", "left_lng", "right_lng",
        "sort_point_lat", "sort_point_lng",
    }
    for key, values in qs.items():
        if key in _pass_through:
            # Multi-value params → keep as comma-separated or first value
            params[key] = values[0] if len(values) == 1 else values

    # Force publication_time ordering if not explicitly set
    params.setdefault("order_by", "publication_time")
    params["page"] = page_num
    params["per_page"] = 20

    return params


def _vacancy_url(vacancy: dict) -> str:
    """Return the canonical web URL for a vacancy dict returned by the API."""
    # The API returns 'alternate_url' (web page) and 'url' (API URL)
    return vacancy.get("alternate_url") or vacancy.get("url", "")


def _vacancy_id_from_url(url: str) -> str:
    """Extract numeric vacancy id from alternate_url."""
    m = re.search(r"/vacancy/(\d+)", url)
    return m.group(1) if m else ""


def _parse_description(vac_detail: dict) -> str:
    """
    Build a plain-text description from a full vacancy detail response.
    Combines structured fields + HTML description (stripped of tags).
    """
    parts: list[str] = []

    exp = (vac_detail.get("experience") or {}).get("name", "")
    if exp:
        parts.append(f"Требуемый опыт работы: {exp}")

    salary = vac_detail.get("salary")
    if salary:
        sal_from = salary.get("from")
        sal_to = salary.get("to")
        sal_cur = salary.get("currency", "")
        sal_txt = ""
        if sal_from:
            sal_txt += f"от {sal_from} "
        if sal_to:
            sal_txt += f"до {sal_to} "
        sal_txt += sal_cur
        parts.append(f"Зарплата: {sal_txt.strip()}")

    # Strip HTML tags from description
    raw_desc: str = vac_detail.get("description", "") or ""
    clean_desc = re.sub(r"<[^>]+>", " ", raw_desc)
    clean_desc = re.sub(r"&nbsp;", " ", clean_desc)
    clean_desc = re.sub(r"&amp;", "&", clean_desc)
    clean_desc = re.sub(r"&lt;", "<", clean_desc)
    clean_desc = re.sub(r"&gt;", ">", clean_desc)
    clean_desc = re.sub(r"\s{2,}", " ", clean_desc).strip()

    if parts:
        header = "ТРЕБОВАНИЯ ВАКАНСИИ:\n" + "\n".join(parts) + "\n\nОПИСАНИЕ:\n"
    else:
        header = ""

    return header + clean_desc


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class HHSearchService:
    """Search vacancies and fetch descriptions via HH REST API."""

    # ------------------------------------------------------------------
    # Card list (called from runner._run_loop)
    # ------------------------------------------------------------------

    async def search_cards(
        self,
        anti_fraud: AntiFraud,
        page_num: int = 0,
        url: str = "",
        # Kept for backward-compat; ignored (no browser used)
        existing_page=None,
    ) -> tuple[None, list[dict], None]:
        """
        Fetch one page of vacancy cards from the API.

        Returns ``(None, cards, None)`` — the first and third elements
        used to be the Playwright page/context objects; they are now
        always ``None`` since we use the REST API.

        Each card dict has: ``url``, ``title``, ``employer``, ``salary``.
        """
        url = prepare_search_url(url)
        params = _url_to_api_params(url, page_num)

        logger.info("search_cards API: page=%d params=%s", page_num, params)

        try:
            rv = await hh_api.search_vacancies(params)
        except HHApiError as e:
            logger.error("search_cards API error: %s", e)
            return None, [], None

        items: list[dict] = rv.get("items", [])
        cards = []
        for item in items:
            vac_url = _vacancy_url(item)
            if not vac_url:
                continue
            cards.append({
                "url": vac_url,
                "title": item.get("name", ""),
                "employer": (item.get("employer") or {}).get("name", "Unknown"),
                "salary": _fmt_salary(item.get("salary")),
                "_api_id": item.get("id", ""),
            })

        logger.info("search_cards: %d cards on page %d", len(cards), page_num)
        return None, cards, None

    # ------------------------------------------------------------------
    # Vacancy description (called from runner._process_card)
    # ------------------------------------------------------------------

    async def get_vacancy_description(
        self,
        page,  # kept for signature compat; ignored
        url: str,
    ) -> str:
        """
        Fetch the full vacancy description via GET /vacancies/{id}.

        Falls back to empty string on any error.
        Caches results in the database.
        """
        from .. import database as db

        try:
            cached = await db.get_cached_vacancy_description(url)
            if cached:
                logger.debug("Using cached description for: %s", url)
                return cached
        except Exception as e:
            logger.debug("Failed to read description cache: %s", e)

        vacancy_id = _vacancy_id_from_url(url)
        if not vacancy_id:
            logger.warning("Cannot extract vacancy id from url: %s", url)
            return ""

        try:
            detail = await hh_api.get_vacancy(vacancy_id)
        except HHNotFound:
            logger.warning("Vacancy not found: %s", url)
            return ""
        except HHApiError as e:
            logger.warning("Failed to get vacancy %s: %s", url, e)
            return ""

        description = _parse_description(detail)

        # Cache for future runs
        try:
            title = detail.get("name", "Unknown")
            employer = (detail.get("employer") or {}).get("name", "Unknown")
            await db.save_vacancy_description_to_cache(url, title, employer, description)
        except Exception as cache_err:
            logger.warning("Failed to cache description: %s", cache_err)

        return description

    # ------------------------------------------------------------------
    # Convenience helpers (kept for compat)
    # ------------------------------------------------------------------

    async def get_vacancy_count(self, *_args, **_kwargs) -> int:
        """Not used by runner; kept for backward compat."""
        return 0

    async def search_by_url(self, url: str, anti_fraud: AntiFraud, page_num: int = 0, **_kw) -> list[Vacancy]:
        _, cards, _ = await self.search_cards(anti_fraud, page_num, url)
        result = []
        for card in cards:
            desc = await self.get_vacancy_description(None, card["url"])
            result.append(Vacancy(
                title=card["title"],
                url=card["url"],
                employer=card["employer"],
                description=desc,
            ))
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_salary(salary: Optional[dict]) -> str:
    if not salary:
        return ""
    sal_from = salary.get("from")
    sal_to = salary.get("to")
    cur = salary.get("currency", "")
    parts = []
    if sal_from:
        parts.append(f"от {sal_from}")
    if sal_to:
        parts.append(f"до {sal_to}")
    return " ".join(parts) + (f" {cur}" if cur else "")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

search_service = HHSearchService()

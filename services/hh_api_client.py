"""
Async HH.ru Mobile API client.

Uses the official Android app's OAuth credentials to interact with
the HH.ru REST API at https://api.hh.ru/ — no browser required for
search or apply operations.

The module exposes a single module-level singleton ``hh_api`` that is
lazily initialised from the database token store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Android app credentials (same as in hh-applicant-tool)
# ---------------------------------------------------------------------------
_ANDROID_CLIENT_ID = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
_ANDROID_CLIENT_SECRET = "V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS"

HH_API_URL = "https://api.hh.ru/"
HH_OAUTH_URL = "https://hh.ru/oauth/"

# HH Android app scheme used as redirect_uri during OAuth
HH_ANDROID_SCHEME = "hhandroid"

_DEFAULT_DELAY = 0.4  # minimum seconds between requests

# Real media device models (from hh-applicant-tool reference)
MOBILE_MODELS: list[str] = [
    "23053RN02A",
    "23053RN02Y",
    "23053RN02I",
    "23053RN02L",
    "23077RABDC",
    "2411DRN47C",
    "2409BRN2CA",
    "2409BRN2CG",
    "2409BRN2CY",
    "2508CRN2BE",
    "2508CRN2BC",
    "2508CRN2BG",
    "SM-A165F",
    "SM-A165F/DS",
    "SM-A165M",
    "SM-A165M/DS",
    "SM-A165F/DSB",
    "24108PCE2I",
    "MZB0KE1IN",
]


def _generate_android_user_agent() -> str:
    """Generate a randomized Android HH app User-Agent (anti-fingerprinting)."""
    model = random.choice(MOBILE_MODELS)
    minor = random.randint(100, 150)
    patch = random.randint(10000, 15000)
    android = random.randint(11, 15)
    return (
        f"ru.hh.android/7.{minor}.{patch}, Device: {model}, "
        f"Android OS: {android} (UUID: {uuid.uuid4()})"
    )


# ---------------------------------------------------------------------------
# Token storage helpers (uses the app database)
# ---------------------------------------------------------------------------

async def _load_token() -> dict:
    """Load OAuth token from database settings table."""
    from .. import database as db
    access_token = await db.get_setting("hh_access_token", "")
    refresh_token = await db.get_setting("hh_refresh_token", "")
    expires_at_str = await db.get_setting("hh_token_expires_at", "0")
    try:
        expires_at = int(expires_at_str)
    except ValueError:
        expires_at = 0
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }


async def _save_token(access_token: str, refresh_token: str, expires_at: int) -> None:
    """Persist OAuth token to database settings table."""
    from .. import database as db
    await db.set_setting("hh_access_token", access_token)
    await db.set_setting("hh_refresh_token", refresh_token)
    await db.set_setting("hh_token_expires_at", str(expires_at))


async def _clear_token() -> None:
    """Remove OAuth token from database."""
    await _save_token("", "", 0)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HHApiError(Exception):
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self.data = data
        super().__init__(f"HH API error {status}: {data}")


class HHNegotiationsLimitExceeded(HHApiError):
    pass


class HHCaptchaRequired(HHApiError):
    @property
    def captcha_url(self) -> str:
        for err in (self.data.get("errors") or []):
            if err.get("value") == "captcha_required":
                return err.get("captcha_url", "")
        return ""


class HHForbidden(HHApiError):
    pass


class HHTokenExpired(HHApiError):
    pass


class HHNotFound(HHApiError):
    pass


# ---------------------------------------------------------------------------
# Async API client
# ---------------------------------------------------------------------------

class HHApiClient:
    """
    Async HTTP client for the HH.ru mobile REST API.

    Handles:
    - Bearer token authentication + auto-refresh
    - Rate limiting (minimum delay between requests)
    - JSON parsing
    - Error mapping to typed exceptions
    """

    def __init__(self) -> None:
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._expires_at: int = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._last_request_at: float = 0.0
        self._loaded = False
        self._user_agent: str = _generate_android_user_agent()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            settings = get_settings()
            proxy = settings.proxy_url or None
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
                proxy=proxy,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def load_token(self) -> bool:
        """Load token from DB. Returns True if a valid token was found."""
        tok = await _load_token()
        self._access_token = tok["access_token"]
        self._refresh_token = tok["refresh_token"]
        self._expires_at = tok["expires_at"]
        self._loaded = True
        return bool(self._access_token)

    async def save_token(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        """Store a new token pair received from HH OAuth."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = int(time.time()) + expires_in
        await _save_token(access_token, refresh_token, self._expires_at)
        logger.info("HH OAuth token saved (expires in %ds)", expires_in)

    async def clear_token(self) -> None:
        """Revoke and remove the stored token."""
        self._access_token = ""
        self._refresh_token = ""
        self._expires_at = 0
        await _clear_token()

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    @property
    def is_token_expired(self) -> bool:
        if not self._expires_at:
            return False  # no expiry info — treat as valid
        return time.time() >= self._expires_at - 60  # 60s margin

    async def refresh_token(self) -> bool:
        """Try to refresh the access token using the refresh token. Returns True on success."""
        if not self._refresh_token:
            return False
        logger.info("Refreshing HH access token...")
        try:
            session = await self._ensure_session()
            async with session.post(
                urljoin(HH_OAUTH_URL, "token"),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                headers=self._base_headers(),
                allow_redirects=False,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and "access_token" in data:
                    await self.save_token(
                        data["access_token"],
                        data.get("refresh_token", self._refresh_token),
                        data.get("expires_in", 86400),
                    )
                    logger.info("HH access token refreshed successfully")
                    return True
                else:
                    logger.warning("Token refresh failed: %s %s", resp.status, data)
                    return False
        except Exception as e:
            logger.error("Token refresh exception: %s", e)
            return False

    # ------------------------------------------------------------------
    # Exchange OAuth code for token
    # ------------------------------------------------------------------

    async def authenticate_with_code(self, code: str) -> bool:
        """Exchange an OAuth authorization code for access+refresh tokens."""
        try:
            session = await self._ensure_session()
            async with session.post(
                urljoin(HH_OAUTH_URL, "token"),
                data={
                    "grant_type": "authorization_code",
                    "client_id": _ANDROID_CLIENT_ID,
                    "client_secret": _ANDROID_CLIENT_SECRET,
                    "code": code,
                },
                headers=self._base_headers(),
                allow_redirects=False,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and "access_token" in data:
                    await self.save_token(
                        data["access_token"],
                        data.get("refresh_token", ""),
                        data.get("expires_in", 86400),
                    )
                    return True
                logger.warning("authenticate_with_code failed: %s %s", resp.status, data)
                return False
        except Exception as e:
            logger.error("authenticate_with_code exception: %s", e)
            return False

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "X-HH-App-Active": "true",
            "Accept": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = _DEFAULT_DELAY - elapsed
        if wait > 0:
            await asyncio.sleep(wait)

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        json_body: Optional[dict] = None,
        retry_on_expired: bool = True,
    ) -> Any:
        """
        Perform an authenticated API request.

        Raises HHApiError subclasses on non-2xx responses.
        Automatically retries once on token expiry.
        """
        if not self._loaded:
            await self.load_token()

        if self.is_token_expired and self._refresh_token:
            await self.refresh_token()

        try:
            return await self._request_once(method, endpoint, params, data, json_body)
        except HHForbidden as e:
            # Token expired server-side → refresh and retry once.
            # NOTE: the retry runs OUTSIDE the rate-limit lock: asyncio.Lock is
            # not reentrant, and re-acquiring it inside _request_once() would
            # deadlock the whole scheduler forever.
            if (
                retry_on_expired
                and self._refresh_token
                and (self._has_oauth_error(e.data) or self.is_token_expired)
            ):
                if await self.refresh_token():
                    return await self._request_once(method, endpoint, params, data, json_body)
            raise

    def _has_oauth_error(self, data: Any) -> bool:
        errors = data.get("errors", []) if isinstance(data, dict) else []
        return any(e.get("type") == "oauth" for e in errors)

    async def _request_once(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        async with self._lock:
            await self._wait_rate_limit()
            session = await self._ensure_session()
            url = urljoin(HH_API_URL, endpoint.lstrip("/"))

            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=self._base_headers(),
                    allow_redirects=False,
                ) as resp:
                    self._last_request_at = time.monotonic()
                    body = await resp.text()
                    try:
                        rv = json.loads(body) if body.strip() else {}
                    except json.JSONDecodeError:
                        rv = {}

                    logger.debug("%s %s -> %d", method, url, resp.status)

                    self._raise_for_status(resp.status, rv)
                    return rv

            except HHApiError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Request failed %s %s: %s", method, url, e)
                raise HHApiError(0, str(e)) from e

    def _raise_for_status(self, status: int, data: Any) -> None:
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise HHApiError(status, data)
        errors = data.get("errors", []) if isinstance(data, dict) else []
        error_values = [e.get("value", "") for e in errors]
        if status == 403:
            if "captcha_required" in error_values:
                raise HHCaptchaRequired(status, data)
            raise HHForbidden(status, data)
        if status == 400 and "limit_exceeded" in error_values:
            raise HHNegotiationsLimitExceeded(status, data)
        if status == 404:
            raise HHNotFound(status, data)
        raise HHApiError(status, data)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def get(self, endpoint: str, **params) -> Any:
        return await self.request("GET", endpoint, params=params or None)

    async def post(self, endpoint: str, data: Optional[dict] = None) -> Any:
        return await self.request("POST", endpoint, data=data)

    # ------------------------------------------------------------------
    # High-level API calls
    # ------------------------------------------------------------------

    async def get_me(self) -> dict:
        """GET /me — current user info."""
        return await self.get("me")

    async def get_resumes(self) -> list[dict]:
        """GET /resumes/mine — list of the user's resumes."""
        rv = await self.get("resumes/mine")
        return rv if isinstance(rv, list) else rv.get("items", [])

    async def search_vacancies(self, params: dict) -> dict:
        """GET /vacancies — paginated vacancy search."""
        return await self.request("GET", "vacancies", params=params)

    async def get_vacancy(self, vacancy_id: str) -> dict:
        """GET /vacancies/{id} — full vacancy description."""
        return await self.get(f"vacancies/{vacancy_id}")

    async def apply_to_vacancy(
        self,
        vacancy_id: str,
        resume_id: str,
        message: str = "",
    ) -> dict:
        """
        POST /negotiations — send an application.

        Returns empty dict on success (HH API returns 201 with empty body).
        Raises HHNegotiationsLimitExceeded when daily limit is hit.
        """
        data: dict = {
            "vacancy_id": vacancy_id,
            "resume_id": resume_id,
        }
        if message:
            data["message"] = message
        return await self.post("negotiations", data)

    async def publish_resume(self, resume_id: str) -> dict:
        """POST /resumes/{id}/publish — raise (publish) a resume."""
        return await self.post(f"resumes/{resume_id}/publish")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

hh_api = HHApiClient()

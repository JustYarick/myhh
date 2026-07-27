import json
import logging
import re

import httpx
from google import genai

from ..config import get_settings
from ..models import VacancyAnalysis

logger = logging.getLogger(__name__)

DEFAULT_COVER_PROMPT = (
    "Generate a short cover letter for: {title} at {employer}\n"
    "Requirements: {description}\n"
    "My resume summary: {resume}\n"
    "2-3 sentences, no greetings, professional but casual, Russian."
)

DEFAULT_ANALYSIS_PROMPT = (
    "Rate relevance of vacancy: {title} at {employer}\n"
    "Description: {description}\n"
    "Salary: {salary}\n"
    "My resume: {resume}\n"
    'Return JSON: {{"relevance": 0-10, "salary_match": bool, "summary": "...", "apply": bool}}'
)


def _get_proxy_url() -> str | None:
    settings = get_settings()
    return settings.gemini_proxy_url


def _build_http_client() -> httpx.Client:
    proxy = _get_proxy_url()
    if proxy:
        return httpx.Client(proxy=proxy, timeout=30)
    return httpx.Client(timeout=30)


def list_models(api_key: str) -> list[dict]:
    proxy = _get_proxy_url()
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        with _build_http_client() as client:
            r = client.get(url, params={"key": api_key})
            r.raise_for_status()
            data = r.json()
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m.get("name", "").replace("models/", "")
                    models.append({
                        "name": name,
                        "display": m.get("displayName", name),
                    })
            return models
    except Exception as e:
        logger.error(f"Failed to list Gemini models: {e}")
        return []


class GeminiService:
    def __init__(self) -> None:
        self._client = None
        self._model = "gemini-2.0-flash"

    def set_model(self, model: str) -> None:
        self._model = model
        self._client = None

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self):
        if self._client is None:
            settings = get_settings()
            api_key = settings.gemini_api_key
            if not api_key:
                raise ValueError("GEMINI_API_KEY not configured")
            proxy = _get_proxy_url()
            http_client = _build_http_client()
            self._client = genai.Client(
                api_key=api_key,
                http_options={"httpx_client": http_client},
            )
        return self._client

    async def generate_cover_letter(
        self, vacancy: dict, prompt_template: str = "", resume_text: str = ""
    ) -> str:
        template = prompt_template or DEFAULT_COVER_PROMPT
        if "{resume}" not in template:
            template += "\nMy resume: {resume}"
        prompt = template.format(
            title=vacancy.get("title", ""),
            employer=vacancy.get("employer", ""),
            description=vacancy.get("description", ""),
            resume=resume_text if resume_text else "No resume provided",
        )

        logger.info(f"[GEMINI] Cover letter request | model={self._model} | resume_len={len(resume_text)} | vacancy={vacancy.get('title', '?')}")
        logger.debug(f"[GEMINI] Cover letter FULL prompt:\n{'='*60}\n{prompt}\n{'='*60}")

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=self._model,
                contents=prompt,
            )
            text = response.text.strip()
            logger.info(f"[GEMINI] Cover letter FULL response:\n{'='*60}\n{text}\n{'='*60}")
            text = re.sub(r'^["\']|["\']$', "", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:1000]
        except Exception as e:
            logger.error(f"Cover letter generation failed: {e}")
            return ""

    async def analyze_vacancy(
        self, vacancy: dict, prompt_template: str = "", resume_text: str = ""
    ) -> VacancyAnalysis:
        template = prompt_template or DEFAULT_ANALYSIS_PROMPT
        if "{resume}" not in template:
            template += "\nMy resume: {resume}"
        salary = vacancy.get("salary", "Not specified")
        prompt = template.format(
            title=vacancy.get("title", ""),
            employer=vacancy.get("employer", ""),
            description=vacancy.get("description", ""),
            salary=salary,
            resume=resume_text if resume_text else "No resume provided",
        )

        logger.info(f"[GEMINI] Analysis request | model={self._model} | resume_len={len(resume_text)} | vacancy={vacancy.get('title', '?')}")
        logger.debug(f"[GEMINI] Analysis FULL prompt:\n{'='*60}\n{prompt}\n{'='*60}")

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=self._model,
                contents=prompt,
            )
            text = response.text.strip()
            logger.info(f"[GEMINI] Analysis FULL response:\n{'='*60}\n{text}\n{'='*60}")

            json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                result = VacancyAnalysis(
                    relevance=data.get("relevance", 0),
                    salary_match=data.get("salary_match", False),
                    summary=data.get("summary", ""),
                    apply=data.get("apply", False),
                )
                logger.info(f"[GEMINI] Analysis result: relevance={result.relevance} apply={result.apply} summary={result.summary[:100]}")
                return result
            else:
                logger.warning(f"Could not parse AI analysis: {text[:200]}")
                return VacancyAnalysis(relevance=5, apply=True, summary="AI parse error")

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in analysis: {e}")
            return VacancyAnalysis(relevance=5, apply=True, summary="JSON parse error")
        except Exception as e:
            logger.error(f"Vacancy analysis failed: {e}")
            return VacancyAnalysis(relevance=5, apply=True, summary=f"Error: {e}")


gemini_service = GeminiService()

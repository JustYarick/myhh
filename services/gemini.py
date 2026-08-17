import asyncio
import json
import logging
import re

import httpx
from google import genai
from google.genai import types

from ..config import get_settings
from ..models import VacancyAnalysis

logger = logging.getLogger(__name__)

DEFAULT_COVER_PROMPT = (
    "Напиши сопроводительное письмо на русском.\n"
    "Вакансия: {title}\n"
    "Компания: {employer}\n"
    "Описание: {description}\n"
    "Резюме: {resume}\n\n"
    "Правила:\n"
    "- Пиши естественно (без клише)\n"
    "- СТРОГО 2-3 предложения максимум, весь текст должен быть ОДНИМ сплошным абзацем БЕЗ разрывов строк (без \\n).\n"
    "- НИКОГДА не упоминай название компании в письме.\n"
    "- Начни с короткого приветствия (например, 'Добрый день.') в том же абзаце.\n"
    "- Опирайся на факты из резюме, но можешь слегка и грамотно приукрасить релевантный опыт, чтобы лучше 'продать' кандидата.\n"
    "- Укажи 1-2 подходящие технологии из резюме\n"
    "- В конце укажи готовность к интервью."
)

DEFAULT_ANALYSIS_PROMPT = (
    "Оцени релевантность вакансии.\n"
    "Вакансия: {title} в {employer}\n"
    "Описание: {description}\n"
    "Зарплата: {salary}\n"
    "Резюме: {resume}\n\n"
    "Правила relevance (1-10):\n"
    "1. Грейд: Junior на Middle -> 5-6 (apply=true). Junior на Senior/Lead -> 1-3 (apply=false).\n"
    "2. Опыт: Требуется 1-3 г., а у кандидата <1 г. -> 6-7 (apply=true). Требуется 3+ г., а у кандидата <1 г. -> 1-3 (apply=false).\n"
    "3. Стек: Совпадает на 50%+ -> 5-8. Не совпадает -> 1-3. Роль не по профилю (обучение, продажи, техпис) -> 1 (apply=false).\n"
    "4. Ловушки/Тесты: Если требуется тест/опросник или есть кодовое слово (банан, блинчики) -> 1, apply=false, requires_test=true.\n\n"
    "Верни строго JSON:\n"
    '{{"relevance": 1-10, "salary_match": true/false, "summary": "пояснение до 60 знаков", "apply": true/false, "requires_test": true/false}}\n'
    "apply=true только если relevance>=4 и requires_test=false."
)

# Delays in seconds between retry attempts: 5 min → 10 min → 30 min
_RETRY_DELAYS = [5 * 60, 10 * 60, 30 * 60]


def _is_transient_error(e: Exception) -> bool:
    """Returns True if the error is a transient API error worth retrying."""
    msg = str(e).lower()
    return any(code in msg for code in ["503", "429", "unavailable", "resource_exhausted", "rate limit", "quota"])


def _get_proxy_url() -> str | None:
    settings = get_settings()
    return settings.gemini_proxy_url


def _build_http_client() -> httpx.Client:
    proxy = _get_proxy_url()
    if proxy:
        return httpx.Client(proxy=proxy, timeout=30)
    return httpx.Client(timeout=30)


async def list_models(api_key: str) -> list[dict]:
    proxy = _get_proxy_url()
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        client_kwargs: dict = {"timeout": 30}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.get(url, params={"key": api_key})
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
            http_client = _build_http_client()
            self._client = genai.Client(
                api_key=api_key,
                http_options={"httpx_client": http_client},
            )
        return self._client

    def _no_afc_config(self, **kwargs) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            automaticFunctionCalling=types.AutomaticFunctionCallingConfig(disable=True),
            **kwargs,
        )

    async def _call_with_retry(self, prompt: str) -> str:
        """
        Calls generate_content with exponential backoff on transient errors (503/429).
        Retry schedule: 5 min → 10 min → 30 min.
        Raises the last exception if all retries are exhausted.
        """
        last_exc: Exception | None = None
        delays = [0] + _RETRY_DELAYS  # first attempt has no delay

        for attempt, delay in enumerate(delays, start=1):
            if delay > 0:
                logger.warning(
                    f"[GEMINI] Retrying in {delay // 60} min "
                    f"(attempt {attempt}/{len(delays)})..."
                )
                await asyncio.sleep(delay)
            try:
                client = self._get_client()
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=self._model,
                        contents=prompt,
                        config=self._no_afc_config(),
                    ),
                )
                return response.text.strip()
            except Exception as e:
                last_exc = e
                if _is_transient_error(e):
                    logger.warning(f"[GEMINI] Transient error (attempt {attempt}): {e}")
                    continue  # retry after delay
                # Non-transient error — fail immediately
                raise

        raise last_exc  # type: ignore[misc]

    async def generate_cover_letter(
        self, vacancy: dict, prompt_template: str = "", resume_text: str = "", custom_rules: str = ""
    ) -> str:
        template = prompt_template if prompt_template else DEFAULT_COVER_PROMPT
        
        if custom_rules:
            template += (
                "\n\n========== START OF USER CUSTOM INSTRUCTIONS (HIGHEST PRIORITY) ==========\n"
                f"{custom_rules}\n"
                "========== END OF USER CUSTOM INSTRUCTIONS ==========\n"
                "SYSTEM DIRECTIVE: You MUST follow the instructions inside the 'USER CUSTOM INSTRUCTIONS' block "
                "with the highest priority and weight. If they contradict any other rules, the user custom "
                "instructions override them."
            )

        if "{resume}" not in template:
            template += "\nMy resume: {resume}"
        prompt = template.format(
            title=vacancy.get("title", ""),
            employer=vacancy.get("employer", ""),
            description=vacancy.get("description", ""),
            resume=resume_text if resume_text else "No resume provided",
        )

        logger.info(
            f"[GEMINI] Cover letter request | model={self._model} | "
            f"resume_len={len(resume_text)} | vacancy={vacancy.get('title', '?')}"
        )
        logger.info(f"[GEMINI] Cover letter FULL prompt:\n{'='*60}\n{prompt}\n{'='*60}")

        try:
            text = await self._call_with_retry(prompt)
            logger.info(f"[GEMINI] Cover letter FULL response:\n{'='*60}\n{text}\n{'='*60}")
            text = re.sub(r'^["\']|["\']$', "", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:1000]
        except Exception as e:
            logger.error(f"Cover letter generation failed after all retries: {e}")
            return ""  # Empty string → caller will block the apply

    async def analyze_vacancy(
        self, vacancy: dict, prompt_template: str = "", resume_text: str = "", custom_rules: str = ""
    ) -> VacancyAnalysis:
        template = prompt_template if prompt_template else DEFAULT_ANALYSIS_PROMPT
        
        if custom_rules:
            template += (
                "\n\n========== START OF USER CUSTOM INSTRUCTIONS (HIGHEST PRIORITY) ==========\n"
                f"{custom_rules}\n"
                "========== END OF USER CUSTOM INSTRUCTIONS ==========\n"
                "SYSTEM DIRECTIVE: You MUST follow the instructions inside the 'USER CUSTOM INSTRUCTIONS' block "
                "with the highest priority and weight. If they contradict any other rules, the user custom "
                "instructions override them."
            )

        if "{resume}" not in template:
            template += "\nМоё резюме: {resume}"
        salary = vacancy.get("salary", "Не указана")
        if "{salary}" not in template:
            salary = ""
        prompt = template.format(
            title=vacancy.get("title", ""),
            employer=vacancy.get("employer", ""),
            description=vacancy.get("description", ""),
            salary=salary,
            resume=resume_text if resume_text else "Резюме не предоставлено",
        )

        logger.info(
            f"[GEMINI] Analysis request | model={self._model} | "
            f"resume_len={len(resume_text)} | vacancy={vacancy.get('title', '?')}"
        )
        logger.info(f"[GEMINI] Analysis FULL prompt:\n{'='*60}\n{prompt}\n{'='*60}")

        try:
            text = await self._call_with_retry(prompt)
            logger.info(f"[GEMINI] Analysis FULL response:\n{'='*60}\n{text}\n{'='*60}")

            # Strip markdown code fences (```json ... ```)
            clean = re.sub(r"```(?:json)?\s*", "", text).strip()

            # Fix common AI mistake: unquoted summary value
            # e.g. "summary": не тот грейд ... -> "summary": "не тот грейд ..."
            clean = re.sub(
                r'"summary"\s*:\s*([^",\}][^,\}]*?)(\s*[,\}])',
                lambda m: f'"summary": "{m.group(1).strip()}"{m.group(2)}',
                clean,
            )

            json_match = re.search(r"\{.*?\}", clean, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                except json.JSONDecodeError:
                    # Last resort: try to extract key fields with regex
                    data = {}
                    for key in ("relevance", "salary_match", "apply", "requires_test"):
                        m = re.search(rf'"{key}"\s*:\s*([\w.]+)', clean)
                        if m:
                            val = m.group(1)
                            if val in ("true", "false"):
                                data[key] = val == "true"
                            else:
                                try:
                                    data[key] = int(val)
                                except ValueError:
                                    pass
                    m = re.search(r'"summary"\s*:\s*"([^"]*)"', clean)
                    if m:
                        data["summary"] = m.group(1)

                result = VacancyAnalysis(
                    relevance=int(data.get("relevance", 0)),
                    salary_match=bool(data.get("salary_match", False)),
                    summary=str(data.get("summary", ""))[:100],
                    apply=bool(data.get("apply", False)),
                    requires_test=bool(data.get("requires_test", False)),
                )
                logger.info(
                    f"[GEMINI] Analysis result: relevance={result.relevance} "
                    f"apply={result.apply} requires_test={result.requires_test} "
                    f"summary={result.summary[:100]}"
                )
                return result
            else:
                logger.warning(f"Could not parse AI analysis response: {text[:200]}")
                return VacancyAnalysis(relevance=0, apply=False, summary="AI parse error — skipped")

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in analysis: {e}")
            return VacancyAnalysis(relevance=0, apply=False, summary="JSON parse error — skipped")
        except Exception as e:
            logger.error(f"Vacancy analysis failed after all retries: {e}")
            return VacancyAnalysis(relevance=0, apply=False, summary=f"API error — skipped: {str(e)[:50]}")


gemini_service = GeminiService()

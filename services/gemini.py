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
    "Напиши сопроводительное письмо к отклику на вакансию.\n"
    "Вакансия: {title}\n"
    "Компания: {employer}\n"
    "Описание вакансии: {description}\n"
    "Моё резюме: {resume}\n\n"
    "Стиль:\n"
    "- Пиши как живой человек, а не как HR-бот\n"
    "- 2-4 предложения максимум\n"
    "- Начни с короткого приветствия (Здравствуйте / Добрый день)\n"
    "- Без восклицательных знаков и пафоса\n"
    "- Конкретно: назови 2-3 навыка из резюме которые подходят\n"
    "- Если навыков мало — честно скажи что готов изучать, не придумывай\n"
    "- Последнее предложение — что готов к собеседованию\n"
    "- Не повторяй описание вакансию слово в слово\n"
    "- На русском языке"
)

DEFAULT_ANALYSIS_PROMPT = (
    "Ты строгий рекрутер. Не завышай оценки. Будь честен.\n\n"
    "ВАКАНСИЯ:\n"
    "Должность: {title}\n"
    "Компания: {employer}\n"
    "Описание и требования:\n{description}\n"
    "Зарплата: {salary}\n\n"
    "РЕЗЮМЕ КАНДИДАТА:\n{resume}\n\n"
    "ШКАЛА ОЦЕНКИ (строго):\n"
    "10 — полное совпадение: опыт, технологии, уровень\n"
    "7-9 — есть релевантный опыт, основные технологии совпадают\n"
    "4-6 — частичное совпадение, есть похожий опыт или часть навыков\n"
    "1-3 — мало общего, нет опыта или навыков\n\n"
    "ВАЖНЫЕ ПРАВИЛА:\n"
    "- Если в резюме нет опыта работы по направлению — maximum 3\n"
    "- Если в резюме стажёр/ junior, а вакансия mid/senior — maximum 4\n"
    "- Если описания вакансии нет или оно пустое — relevance = 3, apply = false\n"
    "- Если вакансия не по профилю (учитель, продавец, водитель) — apply = false\n"
    "- Не ставь высокие оценки за ' potential' — оценивай реальный опыт\n"
    "- Совпадение по 1-2 навыкам из 10 требуемых — это maximum 4\n\n"
    "ОТСЕЧЕНИЕ (apply=false если):\n"
    "- Нет описания или описание пустое\n"
    "- Вакансия не по специальности\n"
    "- Требуемый опыт明显не совпадает с резюме\n"
    "- Стажировка при наличии опыта\n"
    "- В описании нет конкретных требований\n\n"
    "ВЕРНИ JSON строго:\n"
    '{{"relevance": число 1-10, "salary_match": true/false, "summary": "одно предложение до 60 символов", "apply": true/false}}\n\n'
    "summary — коротко и по делу, без кавычек."
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

    def _no_afc_config(self, **kwargs) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            automaticFunctionCalling=types.AutomaticFunctionCallingConfig(disable=True),
            **kwargs,
        )

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
                config=self._no_afc_config(),
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
                config=self._no_afc_config(),
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

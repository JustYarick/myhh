"""
HH Resume service — fetch and format resume text for AI prompts.
Based on hh-applicant-tool reference implementation.
"""
import logging
import re

logger = logging.getLogger(__name__)


class HHResumeService:

    async def fetch_resume_text(self, resume_id: str) -> str:
        """
        Fetch the full resume via GET /resumes/{id} and format as plain text.

        CRITICAL: In the HH API, `skill_set` is a list of plain strings like
        ["Docker", "Kubernetes"], NOT a list of dicts. The old code called
        s.get("name") on strings which raised AttributeError — caught silently,
        always returning empty string (causing "Резюме не предоставлено").
        """
        if not resume_id:
            return ""
        try:
            from .hh_api_client import hh_api
            detail = await hh_api.request("GET", f"resumes/{resume_id}")
            if not detail:
                logger.warning(f"fetch_resume_text: empty response for resume_id={resume_id}")
                return ""

            parts = []

            # Должность
            title = detail.get("title", "")
            if title:
                parts.append(f"Должность: {title}")

            # О себе (свободный текст)
            skills_text = detail.get("skills", "")
            if skills_text:
                parts.append("\n---------- О СЕБЕ ----------")
                parts.append(skills_text)

            # Навыки — skill_set это список строк, НЕ список словарей!
            skill_set = detail.get("skill_set") or []
            if skill_set:
                parts.append("\n---------- НАВЫКИ ----------")
                parts.append(", ".join(str(s) for s in skill_set))

            # Опыт работы
            experience = detail.get("experience") or []
            if experience:
                parts.append("\n---------- ОПЫТ РАБОТЫ ----------")
                for exp in experience:
                    company = exp.get("company") or "Не указано"
                    position = exp.get("position") or "Не указано"
                    start = exp.get("start") or ""
                    end = exp.get("end") or "по настоящее время"
                    parts.append(f"\n- {company}")
                    parts.append(f"  Должность: {position}")
                    parts.append(f"  Период: {start} — {end}")
                    description = exp.get("description") or ""
                    if description:
                        description = re.sub(r"<[^>]+>", " ", description).strip()
                        description = re.sub(r"\s{2,}", " ", description)
                        parts.append(f"  Описание: {description[:600]}")

            # Образование
            education = (detail.get("education") or {}).get("primary") or []
            if education:
                parts.append("\n---------- ОБРАЗОВАНИЕ ----------")
                for edu in education:
                    name = edu.get("name", "")
                    year = edu.get("year", "")
                    if name:
                        parts.append(f"- {name} ({year})")

            result = "\n".join(parts)
            if result:
                logger.info(f"fetch_resume_text: OK for resume_id={resume_id}, {len(result)} chars")
            else:
                logger.warning(
                    f"fetch_resume_text: empty result for resume_id={resume_id}, "
                    f"raw keys={list(detail.keys())}"
                )
            return result

        except Exception as e:
            logger.error(
                f"fetch_resume_text FAILED for resume_id={resume_id}: "
                f"{type(e).__name__}: {e}",
                exc_info=True
            )
            return ""

    async def publish_resume(self, resume_id: str) -> tuple[bool, str]:
        """Raise (publish) resume using Mobile API."""
        try:
            from .hh_api_client import hh_api
            await hh_api.request("POST", f"resumes/{resume_id}/publish")
            return True, "Резюме успешно поднято!"
        except Exception as e:
            status = getattr(e, "status", 0)
            if status == 403:
                return False, "Слишком рано для поднятия (лимит)."
            if status == 429:
                return False, "Слишком много запросов."
            logger.error(f"Failed to raise resume {resume_id}: {e}")
            return False, f"Ошибка API: {e}"

    async def get_resumes(self) -> list:
        """Fetch the user's resumes. Returns list of objects with .id, .title, .status."""
        try:
            from .hh_api_client import hh_api
            from dataclasses import dataclass

            @dataclass
            class ResumeInfo:
                id: str
                title: str
                status: str

            # hh_api.get_resumes() already unwraps 'items'
            raw = await hh_api.get_resumes()
            if not raw:
                return []

            result = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                status_val = item.get("status", {})
                status_id = status_val.get("id", "unknown") if isinstance(status_val, dict) else str(status_val)
                result.append(ResumeInfo(
                    id=item.get("id", ""),
                    title=item.get("title", "Без названия"),
                    status=status_id,
                ))
            return result
        except Exception as e:
            logger.error(f"Failed to fetch resumes: {e}", exc_info=True)
            return []


resume_service = HHResumeService()

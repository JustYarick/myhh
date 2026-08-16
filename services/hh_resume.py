import logging
import asyncio

logger = logging.getLogger(__name__)

class HHResumeService:
    async def fetch_resume_text(self, resume_id: str) -> str:
        """
        Fetch the text of a resume using the HH mobile API.
        Returns plain text combining key resume fields.
        Falls back to empty string on error.
        """
        try:
            from .hh_api_client import hh_api
            detail = await hh_api.request("GET", f"resumes/{resume_id}")
            if not detail:
                return ""

            parts = []

            # Title
            title = detail.get("title", "")
            if title:
                parts.append(f"Должность: {title}")

            # Skills
            skills = [s.get("name", "") for s in (detail.get("skill_set") or [])]
            if skills:
                parts.append("Навыки: " + ", ".join(skills))

            # Experience
            for exp in (detail.get("experience") or []):
                company = (exp.get("company") or "")
                position = (exp.get("position") or "")
                start = (exp.get("start") or "")
                end = (exp.get("end") or "по н.в.")
                description = (exp.get("description") or "")
                if company or position:
                    parts.append(f"\nОпыт: {position} в {company} ({start} — {end})")
                    if description:
                        import re as _re
                        description = _re.sub(r"<[^>]+>", " ", description).strip()
                        parts.append(description[:500])

            # Education
            for edu in (detail.get("education", {}).get("primary") or []):
                name = edu.get("name", "")
                year = edu.get("year", "")
                if name:
                    parts.append(f"Образование: {name} ({year})")

            return "\n".join(parts)

        except Exception as e:
            logger.warning(f"fetch_resume_text via API failed for {resume_id}: {e}")
            return ""

    async def publish_resume(self, resume_id: str) -> tuple[bool, str]:
        """
        Raise (publish) resume using Mobile API.
        Returns (success, message).
        """
        try:
            from .hh_api_client import hh_api
            await hh_api.request("POST", f"resumes/{resume_id}/publish")
            return True, "Резюме успешно поднято!"
        except Exception as e:
            msg = str(e)
            if hasattr(e, "status") and getattr(e, "status") == 403:
                return False, "Слишком рано для поднятия (лимит)."
            if "status" in dir(e) and e.status == 429: # type: ignore
                return False, "Слишком много запросов."
            logger.error(f"Failed to raise resume {resume_id}: {e}")
            return False, f"Ошибка API: {msg}"

    async def get_resumes(self) -> list:
        """
        Fetch the user's resumes using the mobile API.
        Returns a list of dataclass-like objects or dicts (since we need .id, .title, .status).
        """
        try:
            from .hh_api_client import hh_api
            from dataclasses import dataclass
            
            @dataclass
            class ResumeInfo:
                id: str
                title: str
                status: str
                
            data = await hh_api.request("GET", "resumes/mine")
            if not data or "items" not in data:
                return []
                
            return [
                ResumeInfo(
                    id=item.get("id", ""),
                    title=item.get("title", "Без названия"),
                    status=item.get("status", {}).get("id", "unknown")
                )
                for item in data["items"]
            ]
        except Exception as e:
            logger.error(f"Failed to fetch resumes: {e}")
            return []

resume_service = HHResumeService()

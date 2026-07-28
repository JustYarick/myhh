from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HHResume:
    id: str
    title: str
    status: str


@dataclass
class ResumeData:
    """Structured resume data parsed from HH.ru page."""
    title: str = ""
    skills: list[str] = field(default_factory=list)
    experience: list[dict] = field(default_factory=list)
    education: list[dict] = field(default_factory=list)
    courses: list[dict] = field(default_factory=list)
    contacts: dict = field(default_factory=dict)
    preferences: dict = field(default_factory=dict)
    about: str = ""

    def to_text(self) -> str:
        """Convert to compact text for Gemini."""
        lines = []

        if self.title:
            lines.append(f"Позиция: {self.title}")

        if self.skills:
            lines.append(f"Навыки: {', '.join(self.skills)}")

        if self.preferences:
            parts = [f"{k}: {v}" for k, v in self.preferences.items() if v]
            if parts:
                lines.append("Предпочтения: " + "; ".join(parts))

        if self.experience:
            lines.append("Опыт работы:")
            for exp in self.experience:
                lines.append(
                    f"  {exp.get('period', '')} — {exp.get('position', '')} @ {exp.get('company', '')}"
                )
        else:
            lines.append("Опыт работы: отсутствует (0 лет)")

        if self.education:
            lines.append("Образование:")
            for edu in self.education:
                lines.append(
                    f"  {edu.get('university', '')} — {edu.get('faculty', '')} ({edu.get('year_degree', '')})"
                )

        if self.courses:
            lines.append("Повышение квалификации, курсы:")
            for c in self.courses:
                lines.append(
                    f"  {c.get('org', '')} — {c.get('name', '')} ({c.get('year', '')})"
                )

        if self.contacts:
            if self.contacts.get("phone"):
                lines.append(f"Телефон: {self.contacts['phone']}")
            if self.contacts.get("email"):
                lines.append(f"Email: {self.contacts['email']}")

        if self.about:
            lines.append(f"О себе: {self.about}")

        return "\n".join(lines)

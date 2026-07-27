from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class FlowConfig:
    search_url: str = ""
    resume_id: str = ""
    resume_text: str = ""
    vacancy_count: int = 0
    max_pages: int = 3
    max_apps_per_day: int = 50
    max_apps_per_hour: int = 10
    delay_min: float = 5.0
    delay_max: float = 15.0
    delay_between_pages: float = 10.0
    auto_start_hour: Optional[int] = None
    auto_stop_hour: Optional[int] = None
    cover_letter_prompt: str = (
        "Generate a short cover letter for: {title} at {employer}\n"
        "Requirements: {description}\n"
        "My resume: {resume}\n"
        "2-3 sentences, no greetings, professional but casual, Russian."
    )
    analysis_prompt: str = (
        "Rate relevance of vacancy: {title} at {employer}\n"
        "Description: {description}\n"
        "Salary: {salary}\n"
        "My resume: {resume}\n"
        'Return JSON: {{"relevance": 0-10, "salary_match": bool, "summary": "...", "apply": bool}}'
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FlowConfig":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    def summary(self) -> str:
        lines = []
        if self.search_url:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.search_url)
            params = parse_qs(parsed.query)
            text = params.get("text", [""])[0]
            if text:
                lines.append(f"🔍 Search: <b>{text}</b>")
            if self.vacancy_count:
                lines.append(f"📋 Found: <b>{self.vacancy_count}</b> vacancies")
            resume_status = f"loaded ({len(self.resume_text)} chars)" if self.resume_text else "not loaded"
            resume_icon = "✅" if self.resume_text else "❌"
            lines.append(f"{resume_icon} Resume: {resume_status}")
            lines.append(f"📄 Pages: <b>{self.max_pages}</b>")
            lines.append(f"⏱ Limits: <b>{self.max_apps_per_day}</b>/day, <b>{self.max_apps_per_hour}</b>/hour")
        else:
            lines.append("❌ <i>No search URL set</i>")
        return "\n".join(lines)


@dataclass
class FlowEntity:
    id: int
    name: str
    config: FlowConfig
    created_at: str = ""
    updated_at: str = ""

    def summary(self) -> str:
        return f"[{self.id}] {self.name}\n{self.config.summary()}"

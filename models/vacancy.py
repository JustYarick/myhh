from enum import Enum

from pydantic import BaseModel, field_validator


class ApplyStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    ERROR = "error"
    CAPTCHA = "captcha"
    ANALYZED_SKIP = "analyzed_skip"


class Vacancy(BaseModel):
    title: str
    url: str
    employer: str
    description: str = ""


class ApplyResult(BaseModel):
    status: ApplyStatus
    message: str


class VacancyAnalysis(BaseModel):
    relevance: int = 0
    salary_match: bool = False
    summary: str = ""
    apply: bool = False
    requires_test: bool = False

    @field_validator("relevance", mode="before")
    @classmethod
    def coerce_relevance(cls, v):
        if v is None:
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    @field_validator("salary_match", "apply", "requires_test", mode="before")
    @classmethod
    def coerce_bool(cls, v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "yes", "1", "да")
        return bool(v)

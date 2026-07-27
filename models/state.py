from datetime import date
from typing import Optional

from pydantic import BaseModel


class BotState(BaseModel):
    is_running: bool = False
    current_page: int = 0
    vacancies_processed: int = 0
    applied_today: int = 0
    last_error: Optional[str] = None
    captcha_detected: bool = False
    paused: bool = False


class DailyStats(BaseModel):
    date: str
    total_applied: int = 0
    successful: int = 0
    errors: int = 0
    skipped: int = 0
    analyzed_skip: int = 0

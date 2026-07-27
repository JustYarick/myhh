from pathlib import Path
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    tg_bot_token: str = Field(default="", alias="TG_BOT_TOKEN")
    tg_allowed_users: str = Field(default="", alias="TG_ALLOWED_USERS")
    tg_proxy_url: Optional[str] = Field(default=None, alias="TG_PROXY_URL")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_proxy_url: Optional[str] = Field(default=None, alias="GEMINI_PROXY_URL")

    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_slow_mo: int = Field(default=0, alias="BROWSER_SLOW_MO")
    page_timeout: int = Field(default=30000, alias="PAGE_TIMEOUT")
    proxy_url: Optional[str] = Field(default=None, alias="PROXY_URL")

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")

    @property
    def session_file(self) -> Path:
        return self.data_dir / "hh_session.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "autohh.db"

    @property
    def allowed_user_ids(self) -> list[int]:
        if not self.tg_allowed_users:
            return []
        return [int(uid.strip()) for uid in self.tg_allowed_users.split(",") if uid.strip()]

    def is_allowed_user(self, user_id: int) -> bool:
        allowed = self.allowed_user_ids
        if not allowed:
            return True
        return user_id in allowed

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()

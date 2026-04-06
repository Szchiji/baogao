from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    BOT_TOKEN: str
    ADMIN_IDS: str = ""
    BOT_MODE: str = "polling"
    WEBHOOK_URL: str = ""
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_MINUTES: int = 60 * 24
    OTP_EXPIRE_MINUTES: int = 5
    ADMIN_CHAT_POLL_INTERVAL: int = 3
    BASE_URL: str = "http://localhost:8000"
    PORT: int = 8000

    @property
    def admin_ids_list(self) -> list[int]:
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

from __future__ import annotations

import warnings

from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULT_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    DATABASE_URL: str
    BOT_TOKEN: str
    ADMIN_IDS: str = ""
    BOT_MODE: str = "polling"
    WEBHOOK_URL: str = ""
    JWT_SECRET: str = _INSECURE_DEFAULT_SECRET
    JWT_EXPIRE_MINUTES: int = 60 * 24
    OTP_EXPIRE_MINUTES: int = 5
    ADMIN_CHAT_POLL_INTERVAL: int = 3
    BASE_URL: str = "http://localhost:8000"
    PORT: int = 8000

    @property
    def admin_ids_list(self) -> list[int]:
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    def warn_insecure_defaults(self) -> None:
        if self.JWT_SECRET == _INSECURE_DEFAULT_SECRET:
            warnings.warn(
                "JWT_SECRET is set to the insecure default value. "
                "Set a strong random secret in production.",
                stacklevel=2,
            )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
settings.warn_insecure_defaults()

"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs for the sync service.

    Field names map to upper-case environment variables (case-insensitive),
    e.g. ``readwise_token`` <- ``READWISE_TOKEN``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Readwise
    readwise_token: str = Field(default="", description="Readwise access token.")
    readwise_category: str = Field(default="pdf")
    readwise_location: str | None = Field(default=None)
    readwise_base_url: str = Field(default="https://readwise.io/api")

    # reMarkable
    remarkable_folder: str = Field(default="Readwise")
    rmapi_path: str = Field(default="rmapi")
    rmapi_config: str = Field(default="/data/rmapi.conf")

    # Sync engine
    sync_interval_seconds: int = Field(default=900)
    state_path: str = Field(default="/data/state.json")
    work_dir: str = Field(default="/data/work")
    inbox_dir: str = Field(default="/data/inbox")
    dry_run: bool = Field(default=False)
    log_level: str = Field(default="INFO")

    # Heartbeat / alerting
    status_path: str = Field(default="/data/status.json")
    alert_webhook_url: str | None = Field(default=None)
    alert_after_failures: int = Field(default=3)

    def require_readwise_token(self) -> str:
        if not self.readwise_token:
            raise RuntimeError(
                "READWISE_TOKEN is not set. Get one from https://readwise.io/access_token"
            )
        return self.readwise_token


def load_settings() -> Settings:
    return Settings()

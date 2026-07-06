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
    archive_removed: bool = Field(
        default=True,
        description="Move device copies of docs gone from Reader into the archive folder.",
    )
    archive_folder: str | None = Field(
        default=None,
        description="Where archived copies go; defaults to <REMARKABLE_FOLDER>/Archive.",
    )

    finish_to_reader: bool = Field(
        default=True,
        description="Docs moved to the Done folder on the tablet get archived in Reader.",
    )
    done_folder: str | None = Field(
        default=None,
        description="Finish queue on the tablet; defaults to <REMARKABLE_FOLDER>/Done.",
    )

    @property
    def effective_archive_folder(self) -> str:
        return self.archive_folder or f"{self.remarkable_folder}/Archive"

    @property
    def effective_done_folder(self) -> str:
        return self.done_folder or f"{self.remarkable_folder}/Done"

    # Sync engine
    sync_interval_seconds: int = Field(default=900)
    state_path: str = Field(default="/data/state.json")
    state_backups: int = Field(
        default=3,
        description="Rotating backups of the state file (state.json.1..N); 0 disables.",
    )
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

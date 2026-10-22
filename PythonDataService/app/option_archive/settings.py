"""Opt-in preservation settings; production activation is an operator action."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The archive's receipt document, at the base of each lake root.
STATE_FILE_NAME = "option-archive-state.json"


class ArchiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPTION_ARCHIVE_", extra="ignore")

    ENABLED: bool = False
    CAPTURE_START_MINUTE_ET: int = Field(default=60, ge=0, le=1439)
    #: Owner opt-in: capture keeps fetching through regular trading hours instead of yielding to them.
    #: Backup and restore still keep out of regular hours, and capture still yields to the evening observation.
    CAPTURE_DURING_REGULAR_HOURS: bool = False
    BACKUP_START_MINUTE_ET: int = Field(default=300, ge=0, le=1439)
    SNAPSHOT_DELAY_MS: int = Field(default=1_800_000, ge=0)
    SNAPSHOT_WINDOW_MS: int = Field(default=7_200_000, ge=60_000)
    RETRY_BACKOFF_MS: int = Field(default=300_000, ge=1)
    BACKUP_DESTINATION: Path = Path("/lake-backup")
    #: The absolute host path Compose mounts at ``BACKUP_DESTINATION``; no backup runs without it.
    BACKUP_HOST_DESTINATION: str | None = None
    HOST_CHECKOUT: Path | None = None
    HOST_LAKE_ROOT: Path | None = None
    BACKUP_DAILY_KEEP: int = Field(default=7, ge=1)
    BACKUP_WEEKLY_KEEP: int = Field(default=4, ge=1)
    BACKUP_STALE_MS: int = Field(default=172_800_000, ge=1)
    RESTORE_STALE_MS: int = Field(default=691_200_000, ge=1)
    AT_RISK_DAYS: int = Field(default=3, ge=0)
    MAX_SNAPSHOT_PAGES: int = Field(default=100, ge=1, le=1000)
    REQUEST_OPEN_MARGIN_MS: int = Field(default=60_000, ge=30_000)


archive_settings = ArchiveSettings()

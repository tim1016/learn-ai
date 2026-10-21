"""Opt-in preservation settings; production activation is an operator action."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ArchiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPTION_ARCHIVE_", extra="ignore")

    ENABLED: bool = False
    CAPTURE_START_MINUTE_ET: int = Field(default=60, ge=0, le=1439)
    BACKUP_START_MINUTE_ET: int = Field(default=300, ge=0, le=1439)
    SNAPSHOT_DELAY_MS: int = Field(default=1_800_000, ge=0)
    SNAPSHOT_WINDOW_MS: int = Field(default=7_200_000, ge=60_000)
    BACKUP_DESTINATION: Path = Path("/lake-backup")
    BACKUP_MOUNT: Path = Path("/lake-backup")
    BACKUP_HOST_DESTINATION: str = "~/learn-ai-lake-backup"
    BACKUP_DAILY_KEEP: int = Field(default=7, ge=1)
    BACKUP_WEEKLY_KEEP: int = Field(default=4, ge=1)
    BACKUP_STALE_MS: int = Field(default=172_800_000, ge=1)
    RESTORE_STALE_MS: int = Field(default=691_200_000, ge=1)
    AT_RISK_DAYS: int = Field(default=3, ge=0)
    MAX_SNAPSHOT_PAGES: int = Field(default=100, ge=1, le=1000)
    REQUEST_OPEN_MARGIN_MS: int = Field(default=60_000, ge=30_000)


archive_settings = ArchiveSettings()

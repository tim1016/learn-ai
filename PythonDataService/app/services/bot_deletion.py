"""Durable soft-delete markers for operator-managed bots.

Soft delete hides the selected bot runs from catalog/status surfaces while
preserving run artifacts for audit, account attribution, and postmortems.
The marker is run-id scoped so a future redeploy with the same
``strategy_instance_id`` can become visible again with a new run id.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.identity import (
    STRATEGY_INSTANCE_ID_PATTERN,
    strategy_instance_artifact_dir,
    validate_strategy_instance_id,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

BOT_DELETION_FILENAME = "bot_deletion.json"


class BotDeletionCorruptError(RuntimeError):
    """Raised when a bot deletion marker exists but cannot be parsed."""

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"bot deletion marker at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause


class BotDeletionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    deleted_run_ids: tuple[str, ...] = ()
    deleted_at_ms: int = Field(ge=0)
    deleted_by: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)
    version: int = Field(default=1, ge=1)


def stable_bot_deletion_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    validate_strategy_instance_id(strategy_instance_id)
    match = STRATEGY_INSTANCE_ID_PATTERN.fullmatch(strategy_instance_id)
    if match is None:
        raise ValueError(f"strategy_instance_id rejected on second check: {strategy_instance_id!r}")
    # Keep the regex capture in this path-builder frame. CodeQL recognizes
    # Match.group output as sanitized, while it does not follow a sanitizer
    # returned through another helper before the filesystem sink.
    safe_strategy_instance_id = match.group(0)
    return (
        strategy_instance_artifact_dir(
            artifacts_root,
            "live_state",
            safe_strategy_instance_id,
        )
        / BOT_DELETION_FILENAME
    )


def read_bot_deletion(artifacts_root: Path, strategy_instance_id: str) -> BotDeletionRecord | None:
    # Keep validation, regex reconstruction, and containment in the same frame
    # as the read sinks. CodeQL does not propagate sanitizer evidence through
    # a custom path-builder return value.
    validate_strategy_instance_id(strategy_instance_id)
    match = STRATEGY_INSTANCE_ID_PATTERN.fullmatch(strategy_instance_id)
    if match is None:
        raise ValueError(f"strategy_instance_id rejected on second check: {strategy_instance_id!r}")
    safe_strategy_instance_id = match.group(0)
    live_state_root = (artifacts_root / "live_state").resolve()
    path = (live_state_root / safe_strategy_instance_id / BOT_DELETION_FILENAME).resolve(strict=False)
    try:
        common = os.path.commonpath([str(path), str(live_state_root)])
    except ValueError as exc:
        raise ValueError(f"bot deletion path {path} cannot share root {live_state_root}") from exc
    if common != str(live_state_root):
        raise ValueError(f"bot deletion path {path} escapes root {live_state_root}")
    if not path.exists():
        return None
    try:
        return BotDeletionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise BotDeletionCorruptError(path, exc) from exc


def bot_run_is_soft_deleted(artifacts_root: Path, strategy_instance_id: str, run_id: str) -> bool:
    record = read_bot_deletion(artifacts_root, strategy_instance_id)
    return record is not None and run_id in record.deleted_run_ids


def bot_has_soft_deletion(artifacts_root: Path, strategy_instance_id: str) -> bool:
    return read_bot_deletion(artifacts_root, strategy_instance_id) is not None


def soft_delete_bot_runs(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: list[str],
    deleted_by: str,
    reason: str | None,
    now_ms: int,
) -> BotDeletionRecord:
    path = stable_bot_deletion_path(artifacts_root, strategy_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        existing = _read_locked(path)
        deleted_run_ids = tuple(sorted({*(existing.deleted_run_ids if existing else ()), *run_ids}))
        record = BotDeletionRecord(
            strategy_instance_id=strategy_instance_id,
            deleted_run_ids=deleted_run_ids,
            deleted_at_ms=now_ms,
            deleted_by=deleted_by,
            reason=reason,
            version=(existing.version + 1) if existing is not None else 1,
        )
        _write_locked(path, record)
        return record


def _read_locked(path: Path) -> BotDeletionRecord | None:
    if not path.exists():
        return None
    try:
        return BotDeletionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise BotDeletionCorruptError(path, exc) from exc


def _write_locked(path: Path, record: BotDeletionRecord) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = record.model_dump_json().encode("utf-8")
    with open(tmp_path, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    _fsync_parent_dir(path)

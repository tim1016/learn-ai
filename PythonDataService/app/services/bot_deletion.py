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

from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_registry import retire_soft_deleted_instance_bindings
from app.engine.live.identity import validate_strategy_instance_id
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.live_runs import BotDeleteResponse

BOT_DELETION_FILENAME = "bot_deletion.json"


class BotDeletionCorruptError(RuntimeError):
    """Raised when a bot deletion marker exists but cannot be parsed."""

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"bot deletion marker at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause


class BotDeletionRegistryRetirementError(RuntimeError):
    """The deletion marker was durable, but binding retirement was not."""

    def __init__(self, record: BotDeletionRecord, cause: BaseException) -> None:
        super().__init__(
            "bot deletion marker was written but account binding retirement could not be recorded"
        )
        self.record = record
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
    return artifacts_root / "live_state" / strategy_instance_id / BOT_DELETION_FILENAME


def read_bot_deletion(artifacts_root: Path, strategy_instance_id: str) -> BotDeletionRecord | None:
    path = stable_bot_deletion_path(artifacts_root, strategy_instance_id)
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


def bot_delete_response(artifacts_root: Path, record: BotDeletionRecord) -> BotDeleteResponse:
    """Project a durable marker into the stable operator delete receipt."""

    return BotDeleteResponse(
        strategy_instance_id=record.strategy_instance_id,
        deleted_at_ms=record.deleted_at_ms,
        deleted_by=record.deleted_by,
        reason=record.reason,
        deleted_run_ids=list(record.deleted_run_ids),
        marker_path=str(stable_bot_deletion_path(artifacts_root, record.strategy_instance_id)),
    )


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


def soft_delete_and_retire_bot_runs(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: list[str],
    deleted_by: str,
    reason: str | None,
    now_ms: int,
) -> BotDeletionRecord:
    """Durably hide selected runs, then retire their current account bindings.

    The marker is intentionally written first: hiding a stopped bot is safe on
    its own, while a failed append-only registry retirement is visible to the
    operator as a retryable partial outcome.  Keeping the ordering here makes
    every delete caller preserve that crash-safe contract.
    """

    record = soft_delete_bot_runs(
        artifacts_root,
        strategy_instance_id,
        run_ids=run_ids,
        deleted_by=deleted_by,
        reason=reason,
        now_ms=now_ms,
    )
    try:
        retire_soft_deleted_instance_bindings(
            artifacts_root,
            strategy_instance_id=strategy_instance_id,
            run_ids=run_ids,
            now_ms=record.deleted_at_ms,
        )
    except (AccountArtifactError, OSError, ValueError) as exc:
        raise BotDeletionRegistryRetirementError(record, exc) from exc
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

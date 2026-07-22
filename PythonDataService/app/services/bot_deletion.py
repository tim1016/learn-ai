"""Durable soft-delete markers for operator-managed bots.

Soft delete hides the selected bot runs from catalog/status surfaces while
preserving run artifacts for audit, account attribution, and postmortems.
The marker is run-id scoped so a future redeploy with the same
``strategy_instance_id`` can become visible again with a new run id.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.account_registry import (
    ACTIVE_INSTANCE_BINDING_STATES,
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    index_account_instance_bindings,
    read_account_instance_registry,
    write_fenced_lifecycle_retirement_binding,
)
from app.engine.live.bot_lifecycle_evaluator import BotLifecycleEvaluator
from app.engine.live.bot_lifecycle_fence import (
    BOT_LIFECYCLE_OPERATION_FENCE_FILENAME,
    bot_lifecycle_operation_fence,
    stable_bot_lifecycle_operation_fence_path,
)
from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.identity import validate_strategy_instance_id
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

BOT_DELETION_FILENAME = "bot_deletion.json"
BOT_RETIREMENT_TRANSITION_FILENAME = "retirement_transition.json"

# Stable re-exports for callers that adopted the original S1 fence location.
__all__ = [
    "BOT_LIFECYCLE_OPERATION_FENCE_FILENAME",
    "bot_lifecycle_operation_fence",
    "stable_bot_lifecycle_operation_fence_path",
]


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


class BotRetirementBindingTarget(BaseModel):
    """One account-registry successor required by a retirement transaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128)


class BotRetirementTransitionRecord(BaseModel):
    """Fail-closed, replayable commit record for retiring one bot identity.

    Registry rows and lifecycle state live in different files, so a filesystem
    crash cannot make their writes physically atomic. ``PENDING`` is therefore
    the durable commit fence: admissions and Clerk intake reject the identity
    until replay has written every RETIRED successor and the lifecycle/roster
    state. ``COMMITTED`` is retained as an audit receipt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    targets: tuple[BotRetirementBindingTarget, ...]
    prepared_at_ms: int = Field(ge=0)
    updated_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    state: Literal["PENDING", "COMMITTED"] = "PENDING"
    version: int = Field(default=1, ge=1)


def stable_bot_deletion_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    validate_strategy_instance_id(strategy_instance_id)
    return artifacts_root / "live_state" / strategy_instance_id / BOT_DELETION_FILENAME


def stable_bot_retirement_transition_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    validate_strategy_instance_id(strategy_instance_id)
    return artifacts_root / "live_state" / strategy_instance_id / BOT_RETIREMENT_TRANSITION_FILENAME


def read_bot_deletion(artifacts_root: Path, strategy_instance_id: str) -> BotDeletionRecord | None:
    path = stable_bot_deletion_path(artifacts_root, strategy_instance_id)
    if not path.exists():
        return None
    try:
        return BotDeletionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise BotDeletionCorruptError(path, exc) from exc


def read_bot_retirement_transition(
    artifacts_root: Path,
    strategy_instance_id: str,
) -> BotRetirementTransitionRecord | None:
    path = stable_bot_retirement_transition_path(artifacts_root, strategy_instance_id)
    if not path.exists():
        return None
    try:
        return BotRetirementTransitionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise BotDeletionCorruptError(path, exc) from exc


def bot_retirement_is_pending(artifacts_root: Path, strategy_instance_id: str) -> bool:
    """Whether a partially applied retirement must fence this bot identity."""

    record = read_bot_retirement_transition(artifacts_root, strategy_instance_id)
    return record is not None and record.state == "PENDING"


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


def retire_bot_lifecycle_and_bindings(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: Iterable[str],
    updated_by: str,
    reason: str,
    now_ms: int,
) -> BotLifecycleStateRecord:
    """Commit a fenced retirement across account bindings and bot lifecycle."""

    with bot_lifecycle_operation_fence(artifacts_root, strategy_instance_id):
        return retire_bot_lifecycle_and_bindings_under_operation_fence(
            artifacts_root,
            strategy_instance_id,
            run_ids=run_ids,
            updated_by=updated_by,
            reason=reason,
            now_ms=now_ms,
        )


def retire_bot_lifecycle_and_bindings_under_operation_fence(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: Iterable[str],
    updated_by: str,
    reason: str,
    now_ms: int,
) -> BotLifecycleStateRecord:
    """Retire while the caller holds :func:`bot_lifecycle_operation_fence`.

    This is deliberately public only for the router's process-state check:
    that check and the retirement commit must share one fence with Start.
    """

    validate_strategy_instance_id(strategy_instance_id)
    path = stable_bot_retirement_transition_path(artifacts_root, strategy_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        transition = _read_retirement_transition_locked(path)
        if transition is not None and transition.state == "COMMITTED":
            lifecycle = BotLifecycleStateRepo(
                stable_bot_lifecycle_state_path(artifacts_root, strategy_instance_id)
            ).read()
            if lifecycle is not None and lifecycle.phase.value == "RETIRED":
                return lifecycle
            transition = None
        if transition is None:
            bindings = _retirement_bindings(
                artifacts_root,
                strategy_instance_id,
                run_ids=run_ids,
                now_ms=now_ms,
            )
            transition = BotRetirementTransitionRecord(
                strategy_instance_id=strategy_instance_id,
                targets=tuple(
                    BotRetirementBindingTarget(account_id=binding.account_id, run_id=binding.run_id)
                    for binding in bindings
                ),
                prepared_at_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
            )
            _write_retirement_transition_locked(path, transition)
        return _complete_retirement_transition_locked(artifacts_root, path, transition)


def recover_pending_bot_retirements(artifacts_root: Path) -> tuple[str, ...]:
    """Replay incomplete retirements before the daemon admits a new process."""

    live_state_root = artifacts_root / "live_state"
    if not live_state_root.is_dir():
        return ()
    recovered: list[str] = []
    for entry in sorted(live_state_root.iterdir(), key=lambda path: path.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        path = entry / BOT_RETIREMENT_TRANSITION_FILENAME
        if not path.exists():
            continue
        with bot_lifecycle_operation_fence(artifacts_root, entry.name), _file_lock(path):
            transition = _read_retirement_transition_locked(path)
            if transition is None or transition.state == "COMMITTED":
                continue
            if transition.strategy_instance_id != entry.name:
                raise OSError(
                    "retirement transition identity does not match its artifact directory: "
                    f"{transition.strategy_instance_id!r} != {entry.name!r}"
                )
            _complete_retirement_transition_locked(artifacts_root, path, transition)
            recovered.append(transition.strategy_instance_id)
    return tuple(recovered)


def soft_delete_and_retire_bot_runs(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: list[str],
    deleted_by: str,
    reason: str | None,
    now_ms: int,
) -> BotDeletionRecord:
    """Retire account ownership before hiding a bot from control surfaces."""

    with bot_lifecycle_operation_fence(artifacts_root, strategy_instance_id):
        return soft_delete_and_retire_bot_runs_under_operation_fence(
            artifacts_root,
            strategy_instance_id,
            run_ids=run_ids,
            deleted_by=deleted_by,
            reason=reason,
            now_ms=now_ms,
        )


def soft_delete_and_retire_bot_runs_under_operation_fence(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: list[str],
    deleted_by: str,
    reason: str | None,
    now_ms: int,
) -> BotDeletionRecord:
    """Soft-delete after retiring, while the caller holds the operation fence."""

    retire_bot_lifecycle_and_bindings_under_operation_fence(
        artifacts_root,
        strategy_instance_id,
        run_ids=run_ids,
        updated_by=deleted_by,
        reason=reason or "soft_deleted",
        now_ms=now_ms,
    )
    return soft_delete_bot_runs(
        artifacts_root,
        strategy_instance_id,
        run_ids=run_ids,
        deleted_by=deleted_by,
        reason=reason,
        now_ms=now_ms,
    )


def _retirement_bindings(
    artifacts_root: Path,
    strategy_instance_id: str,
    *,
    run_ids: Iterable[str],
    now_ms: int,
) -> list[AccountInstanceBinding]:
    """Discover retirement targets from ledgers *and* authoritative registry folds."""

    targets: dict[tuple[str, str], AccountInstanceBinding] = {}
    account_ids: set[str] = set()
    for run_id in sorted(set(run_ids)):
        ledger_path = artifacts_root / "live_runs" / run_id / "run_ledger.json"
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OSError(f"could not read ledger for retiring run {run_id!r}: {exc}") from exc
        account_id = ledger.get("account_id") if isinstance(ledger, dict) else None
        if isinstance(account_id, str) and account_id:
            account_ids.add(account_id)
            targets[(account_id, run_id)] = _retired_binding(
                account_id,
                strategy_instance_id,
                run_id,
                now_ms,
            )
    accounts_root = artifacts_root / "accounts"
    if accounts_root.is_dir():
        account_ids.update(
            entry.name
            for entry in accounts_root.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        )
    for account_id in account_ids:
        registry = read_account_instance_registry(artifacts_root, account_id)
        latest = index_account_instance_bindings(registry).latest_by_instance.get(strategy_instance_id)
        if latest is not None and latest.lifecycle_state in ACTIVE_INSTANCE_BINDING_STATES:
            targets[(account_id, latest.run_id)] = _retired_binding(
                account_id,
                strategy_instance_id,
                latest.run_id,
                now_ms,
            )
    return list(targets.values())


def _complete_retirement_transition_locked(
    artifacts_root: Path,
    path: Path,
    transition: BotRetirementTransitionRecord,
) -> BotLifecycleStateRecord:
    """Finish a pending transaction while its identity fence remains held."""

    if transition.state == "COMMITTED":
        record = BotLifecycleStateRepo(
            stable_bot_lifecycle_state_path(artifacts_root, transition.strategy_instance_id)
        ).read()
        if record is None:
            raise OSError("committed retirement has no lifecycle state")
        return record
    bindings = [
        _retired_binding(
            target.account_id,
            transition.strategy_instance_id,
            target.run_id,
            transition.prepared_at_ms,
        )
        for target in transition.targets
    ]
    for binding in bindings:
        write_fenced_lifecycle_retirement_binding(
            artifacts_root,
            binding,
            transition_path=path,
        )
    _verify_retired_registry_bindings(artifacts_root, transition.strategy_instance_id, bindings)
    disposition = BotLifecycleEvaluator(
        artifacts_root, transition.strategy_instance_id
    ).retire(
        now_ms=transition.prepared_at_ms,
        updated_by=transition.updated_by,
        reason=transition.reason,
        operation_fence_held=True,
    )
    lifecycle = disposition.lifecycle_state
    if lifecycle is None:
        raise OSError("lifecycle evaluator did not return a retirement state")
    _write_retirement_transition_locked(path, transition.model_copy(update={"state": "COMMITTED"}))
    return lifecycle


def _retired_binding(
    account_id: str,
    strategy_instance_id: str,
    run_id: str,
    now_ms: int,
) -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
        lifecycle_state="RETIRED",
        recorded_at_ms=now_ms,
        source="lifecycle.retire",
    )


def _verify_retired_registry_bindings(
    artifacts_root: Path,
    strategy_instance_id: str,
    bindings: Iterable[AccountInstanceBinding],
) -> None:
    """Fail before lifecycle retirement if an account still folds to ACTIVE ownership."""

    for account_id in {binding.account_id for binding in bindings}:
        latest = index_account_instance_bindings(
            read_account_instance_registry(artifacts_root, account_id)
        ).latest_by_instance.get(strategy_instance_id)
        if latest is None or latest.lifecycle_state != "RETIRED":
            raise OSError(
                f"account registry still has no retired binding for {strategy_instance_id!r} on {account_id!r}"
            )


def _read_retirement_transition_locked(path: Path) -> BotRetirementTransitionRecord | None:
    if not path.exists():
        return None
    try:
        return BotRetirementTransitionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise BotDeletionCorruptError(path, exc) from exc


def _write_retirement_transition_locked(path: Path, record: BotRetirementTransitionRecord) -> None:
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

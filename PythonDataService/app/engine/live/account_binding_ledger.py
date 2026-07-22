"""Durable Clerk-owned command ledger for account-instance bindings.

The legacy ``instance_registry.jsonl`` remains the production read model while
this ledger shadows every forward binding decision.  A daemon can append a
retirement *proposal* while the Clerk is unavailable, but it cannot retire a
binding itself.  The next Clerk startup folds proposals in ledger order before
it accepts requests.

This deliberately uses a separate append-only file rather than rewriting the
registry: a failed dual write is visible as a parity defect and the legacy read
can be retained or restored with ``ACCOUNT_BINDING_LEDGER_READ_ENABLED``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    _safe_account_path_segment,
    account_artifacts_root,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

BINDING_COMMAND_LEDGER_FILENAME = "binding_commands.jsonl"
ACCOUNT_BINDING_LEDGER_READ_ENABLED_ENV = "ACCOUNT_BINDING_LEDGER_READ_ENABLED"


class AccountBindingCommand(BaseModel):
    """One sequenced Clerk binding decision or daemon retirement proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    seq: int = Field(ge=1)
    entry_kind: Literal["decision", "retirement_proposal", "retirement_folded"]
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    lifecycle_state: Literal["DEPLOYED", "ACTIVE", "RETIRED"]
    recorded_at_ms: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=256)
    proposal_seq: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class BindingLedgerParity:
    """Observable comparison between legacy rows and Clerk command decisions."""

    legacy_only_instances: tuple[str, ...]
    ledger_only_instances: tuple[str, ...]
    mismatched_instances: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not (
            self.legacy_only_instances
            or self.ledger_only_instances
            or self.mismatched_instances
        )


@dataclass(frozen=True)
class BindingRetirementFoldResult:
    """Ordered outcomes from Clerk folding daemon retirement proposals."""

    proposals_seen: int
    retirements_applied: int
    superseded_proposals: int


def account_binding_ledger_read_enabled() -> bool:
    """Return the explicit future read-cutover flag; default remains legacy."""

    return os.environ.get(ACCOUNT_BINDING_LEDGER_READ_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def binding_command_ledger_path(artifacts_root: Path, account_id: str) -> Path:
    """Return one confined ledger path for a canonical account id."""

    safe_account_id = _safe_account_path_segment(account_id)
    root = os.path.realpath(os.fspath(account_artifacts_root(artifacts_root, safe_account_id)))
    filename = os.path.basename(BINDING_COMMAND_LEDGER_FILENAME)
    if filename != BINDING_COMMAND_LEDGER_FILENAME:
        raise AccountArtifactError("invalid binding command ledger filename")
    path = os.path.realpath(os.path.join(root, filename))
    root_prefix = root if root.endswith(os.sep) else f"{root}{os.sep}"
    if not path.startswith(root_prefix):
        raise AccountArtifactError(f"binding command ledger path traversal for account_id: {account_id!r}")
    return Path(path)


def read_account_binding_commands(artifacts_root: Path, account_id: str) -> list[AccountBindingCommand]:
    """Replay the ledger strictly and reject malformed/non-monotonic rows."""

    path = binding_command_ledger_path(artifacts_root, account_id)
    try:
        return _read_commands_direct(path, account_id)
    except IsADirectoryError as exc:
        raise AccountArtifactError(f"binding command ledger is not a file: {path}") from exc


def append_binding_decision(
    artifacts_root: Path,
    *,
    binding: dict[str, object],
    write_legacy_row: Callable[[], None],
) -> AccountBindingCommand:
    """Append a Clerk decision, fsync it, then append the legacy compatibility row.

    The ledger lock serializes every modern binding writer.  A legacy-write
    exception intentionally leaves a ledger-only decision so parity exposes a
    repairable dual-write failure instead of concealing it.
    """

    account_id = _required_string(binding, "account_id")
    path = binding_command_ledger_path(artifacts_root, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        command = _append_command_locked(path, binding, entry_kind="decision")
        write_legacy_row()
    return command


def append_binding_retirement_proposal(
    artifacts_root: Path,
    *,
    binding: dict[str, object],
) -> AccountBindingCommand:
    """Record an untrusted daemon liveness observation without mutating registry."""

    account_id = _required_string(binding, "account_id")
    path = binding_command_ledger_path(artifacts_root, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        return _append_command_locked(path, binding, entry_kind="retirement_proposal")


def pending_binding_retirement_proposals(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str | None = None,
) -> tuple[AccountBindingCommand, ...]:
    """Return unmatched retirement proposals in deterministic ledger order."""

    pending: dict[int, AccountBindingCommand] = {}
    for command in read_account_binding_commands(artifacts_root, account_id):
        if command.entry_kind == "retirement_proposal":
            pending[command.seq] = command
        elif command.entry_kind == "retirement_folded" and command.proposal_seq is not None:
            pending.pop(command.proposal_seq, None)
    proposals = tuple(pending.values())
    if strategy_instance_id is None:
        return proposals
    return tuple(proposal for proposal in proposals if proposal.strategy_instance_id == strategy_instance_id)


def fold_binding_retirement_proposals(
    artifacts_root: Path,
    *,
    account_id: str,
    read_current_binding: Callable[[str], dict[str, object] | None],
    write_legacy_retirement: Callable[[dict[str, object]], None],
) -> BindingRetirementFoldResult:
    """Fold outstanding daemon proposals under the Clerk's serialized ledger lock.

    A proposal is applied only when the current legacy binding still names the
    exact run and namespace observed by the daemon.  A later deployment makes
    the proposal superseded, never a stale retirement of the replacement run.
    """

    path = binding_command_ledger_path(artifacts_root, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        commands = _read_commands_direct(path, account_id)
        pending = _pending_from_commands(commands)
        applied = 0
        superseded = 0
        for proposal in pending:
            current = read_current_binding(proposal.strategy_instance_id)
            current_matches = current is not None and all(
                current.get(field) == getattr(proposal, field)
                for field in ("account_id", "strategy_instance_id", "run_id", "bot_order_namespace")
            )
            if current_matches and current.get("lifecycle_state") == "ACTIVE":
                retirement = dict(current)
                retirement.update(
                    lifecycle_state="RETIRED",
                    recorded_at_ms=max(
                        _required_int(current, "recorded_at_ms") + 1,
                        proposal.recorded_at_ms,
                    ),
                    source=proposal.source,
                )
                _append_command_locked(path, retirement, entry_kind="decision")
                write_legacy_retirement(retirement)
                applied += 1
                folded_source = "account_clerk.binding_retirement_folded"
            else:
                superseded += 1
                folded_source = "account_clerk.binding_retirement_superseded"
            folded = _command_payload(proposal.model_dump(mode="json"))
            folded["source"] = folded_source
            _append_command_locked(
                path,
                folded,
                entry_kind="retirement_folded",
                proposal_seq=proposal.seq,
            )
    return BindingRetirementFoldResult(
        proposals_seen=len(pending),
        retirements_applied=applied,
        superseded_proposals=superseded,
    )


def binding_ledger_parity(
    artifacts_root: Path,
    *,
    account_id: str,
    legacy_bindings: Iterable[dict[str, object]],
) -> BindingLedgerParity:
    """Compare latest per-instance registry and ledger decisions without repair."""

    ledger_latest: dict[str, AccountBindingCommand] = {}
    for command in read_account_binding_commands(artifacts_root, account_id):
        if command.entry_kind != "decision":
            continue
        ledger_latest[command.strategy_instance_id] = command
    legacy_latest: dict[str, dict[str, object]] = {}
    for binding in legacy_bindings:
        strategy_instance_id = _required_string(binding, "strategy_instance_id")
        legacy_latest[strategy_instance_id] = dict(binding)
    legacy_ids = set(legacy_latest)
    ledger_ids = set(ledger_latest)
    mismatch: list[str] = []
    comparison_fields = (
        "account_id",
        "strategy_instance_id",
        "run_id",
        "bot_order_namespace",
        "lifecycle_state",
        "recorded_at_ms",
        "source",
    )
    for strategy_instance_id in sorted(legacy_ids & ledger_ids):
        legacy = legacy_latest[strategy_instance_id]
        command = ledger_latest[strategy_instance_id]
        if any(legacy.get(field) != getattr(command, field) for field in comparison_fields):
            mismatch.append(strategy_instance_id)
    return BindingLedgerParity(
        legacy_only_instances=tuple(sorted(legacy_ids - ledger_ids)),
        ledger_only_instances=tuple(sorted(ledger_ids - legacy_ids)),
        mismatched_instances=tuple(mismatch),
    )


def _pending_from_commands(commands: Iterable[AccountBindingCommand]) -> tuple[AccountBindingCommand, ...]:
    pending: dict[int, AccountBindingCommand] = {}
    for command in commands:
        if command.entry_kind == "retirement_proposal":
            pending[command.seq] = command
        elif command.entry_kind == "retirement_folded" and command.proposal_seq is not None:
            pending.pop(command.proposal_seq, None)
    return tuple(pending.values())


def _append_command_locked(
    path: Path,
    binding: dict[str, object],
    *,
    entry_kind: Literal["decision", "retirement_proposal", "retirement_folded"],
    proposal_seq: int | None = None,
) -> AccountBindingCommand:
    # Avoid routing through an artifacts root here: callers already hold this
    # exact ledger's lock and need one read/write critical section.
    account_id = _required_string(binding, "account_id")
    commands = _read_commands_direct(path, account_id)
    payload = _command_payload(binding)
    command = AccountBindingCommand(
        seq=1 if not commands else commands[-1].seq + 1,
        entry_kind=entry_kind,
        proposal_seq=proposal_seq,
        **payload,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(command.model_dump_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_parent_dir(path)
    return command


def _read_commands_direct(path: Path, account_id: str) -> list[AccountBindingCommand]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    commands: list[AccountBindingCommand] = []
    previous = 0
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            command = AccountBindingCommand.model_validate_json(line)
        except ValueError as exc:
            raise AccountArtifactError(f"invalid binding command row {line_no} in {path}: {exc}") from exc
        if command.account_id != account_id or command.seq <= previous:
            raise AccountArtifactError(f"invalid binding command sequence at row {line_no} in {path}")
        if command.entry_kind == "retirement_folded" and command.proposal_seq is None:
            raise AccountArtifactError(f"binding command folded row {line_no} is missing proposal_seq")
        if command.entry_kind != "retirement_folded" and command.proposal_seq is not None:
            raise AccountArtifactError(f"binding command row {line_no} has an unexpected proposal_seq")
        commands.append(command)
        previous = command.seq
    return commands


def _command_payload(binding: dict[str, object]) -> dict[str, object]:
    return {
        "account_id": _required_string(binding, "account_id"),
        "strategy_instance_id": _required_string(binding, "strategy_instance_id"),
        "run_id": _required_string(binding, "run_id"),
        "bot_order_namespace": _required_string(binding, "bot_order_namespace"),
        "lifecycle_state": _required_string(binding, "lifecycle_state"),
        "recorded_at_ms": _required_int(binding, "recorded_at_ms"),
        "source": _required_string(binding, "source"),
    }


def _required_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise AccountArtifactError(f"binding command {field} must be a non-empty string")
    return value


def _required_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise AccountArtifactError(f"binding command {field} must be a non-negative integer")
    return value


__all__ = [
    "ACCOUNT_BINDING_LEDGER_READ_ENABLED_ENV",
    "BINDING_COMMAND_LEDGER_FILENAME",
    "AccountBindingCommand",
    "BindingLedgerParity",
    "BindingRetirementFoldResult",
    "account_binding_ledger_read_enabled",
    "append_binding_decision",
    "append_binding_retirement_proposal",
    "binding_command_ledger_path",
    "binding_ledger_parity",
    "fold_binding_retirement_proposals",
    "pending_binding_retirement_proposals",
    "read_account_binding_commands",
]

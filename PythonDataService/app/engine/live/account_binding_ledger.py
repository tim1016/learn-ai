"""Read-compatible historical account-instance binding command ledger.

The retained models and folds expose legacy evidence without authoring a new
binding decision, retirement proposal, or migration baseline.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    _safe_account_path_segment,
    account_artifacts_root,
)

BINDING_COMMAND_LEDGER_FILENAME = "binding_commands.jsonl"
ACCOUNT_BINDING_LEDGER_READ_ENABLED_ENV = "ACCOUNT_BINDING_LEDGER_READ_ENABLED"

# Fields compared to decide whether the two historical binding formats agree.
_COMPARISON_FIELDS = (
    "account_id",
    "strategy_instance_id",
    "run_id",
    "bot_order_namespace",
    "lifecycle_state",
    "recorded_at_ms",
    "source",
)


class AccountBindingCommand(BaseModel):
    """One historical binding decision or retirement-proposal row."""

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
        return not (self.legacy_only_instances or self.ledger_only_instances or self.mismatched_instances)


def account_binding_ledger_read_enabled() -> bool:
    """Select the historical command ledger as the compatibility read source."""

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
    for strategy_instance_id in sorted(legacy_ids & ledger_ids):
        legacy = legacy_latest[strategy_instance_id]
        command = ledger_latest[strategy_instance_id]
        if any(legacy.get(field) != getattr(command, field) for field in _COMPARISON_FIELDS):
            mismatch.append(strategy_instance_id)
    return BindingLedgerParity(
        legacy_only_instances=tuple(sorted(legacy_ids - ledger_ids)),
        ledger_only_instances=tuple(sorted(ledger_ids - legacy_ids)),
        mismatched_instances=tuple(mismatch),
    )


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


def _required_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise AccountArtifactError(f"binding command {field} must be a non-empty string")
    return value


__all__ = [
    "ACCOUNT_BINDING_LEDGER_READ_ENABLED_ENV",
    "BINDING_COMMAND_LEDGER_FILENAME",
    "AccountBindingCommand",
    "BindingLedgerParity",
    "account_binding_ledger_read_enabled",
    "binding_command_ledger_path",
    "binding_ledger_parity",
    "pending_binding_retirement_proposals",
    "read_account_binding_commands",
]

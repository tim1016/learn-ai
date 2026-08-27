"""Test-only builders for immutable historical IBKR evidence.

Production deliberately exposes no API that authors these retired artifacts.
Tests which exercise the surviving readers write representative old rows here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.engine.live.account_artifacts import (
    ACCOUNT_CLERK_GENERATION_FILENAME,
    ACCOUNT_CLERK_LEASE_FILENAME,
    ACCOUNT_OWNER_GENERATION_FILENAME,
    AccountClerkGeneration,
    AccountClerkLease,
    AccountOwnerGeneration,
    account_artifact_file_path,
    account_artifacts_root,
)
from app.engine.live.account_binding_ledger import (
    AccountBindingCommand,
    binding_command_ledger_path,
    read_account_binding_commands,
)
from app.engine.live.account_clerk_journal import account_clerk_journal_path
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_registry import (
    ACCOUNT_INSTANCE_REGISTRY_FILENAME,
    AccountInstanceBinding,
)
from app.engine.live.live_state_sidecar import _file_lock


def write_historical_account_binding(
    artifacts_root: Path,
    binding: AccountInstanceBinding,
) -> Path:
    """Append matching historical registry and command-ledger rows."""

    registry = account_artifacts_root(artifacts_root, binding.account_id) / ACCOUNT_INSTANCE_REGISTRY_FILENAME
    command_path = binding_command_ledger_path(artifacts_root, binding.account_id)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(command_path):
        commands = read_account_binding_commands(artifacts_root, binding.account_id)
        command = AccountBindingCommand(
            seq=1 if not commands else commands[-1].seq + 1,
            entry_kind="decision",
            **binding.model_dump(mode="json"),
        )
        command_path.write_text(
            "".join(item.model_dump_json() + "\n" for item in (*commands, command)),
            encoding="utf-8",
        )
        with registry.open("a", encoding="utf-8") as stream:
            stream.write(binding.model_dump_json() + "\n")
    return registry


def write_historical_owner_generation(
    artifacts_root: Path,
    generation: AccountOwnerGeneration,
) -> Path:
    return _write_model(
        account_artifact_file_path(
            artifacts_root,
            generation.account_id,
            ACCOUNT_OWNER_GENERATION_FILENAME,
        ),
        generation,
    )


def write_historical_clerk_generation(
    artifacts_root: Path,
    account_id: str,
    *,
    phase: str,
    recorded_at_ms: int,
    source: str,
) -> AccountClerkGeneration:
    path = account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_CLERK_GENERATION_FILENAME,
    )
    existing = (
        AccountClerkGeneration.model_validate_json(path.read_text(encoding="utf-8"))
        if path.is_file()
        else None
    )
    generation = AccountClerkGeneration(
        account_id=account_id,
        generation=1 if existing is None else existing.generation + 1,
        phase=phase,
        recorded_at_ms=recorded_at_ms,
        source=source,
    )
    _write_model(path, generation)
    return generation


def write_historical_clerk_lease(
    artifacts_root: Path,
    lease: AccountClerkLease,
) -> Path:
    return _write_model(
        account_artifact_file_path(
            artifacts_root,
            lease.account_id,
            ACCOUNT_CLERK_LEASE_FILENAME,
        ),
        lease,
    )


def write_historical_clerk_journal(
    artifacts_root: Path,
    account_id: str,
    entries: Sequence[AccountClerkJournalEntry],
) -> Path:
    """Write an explicit immutable snapshot of historical Clerk rows."""

    path = account_clerk_journal_path(artifacts_root, account_id)
    return _write_jsonl(path, entries)


def _write_jsonl(path: Path, rows: Sequence[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8")
    return path


def _write_model(path: Path, model: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.model_dump(mode="json")), encoding="utf-8")
    return path

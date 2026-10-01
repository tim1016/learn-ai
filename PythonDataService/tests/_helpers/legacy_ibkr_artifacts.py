"""Test-only builders for immutable historical IBKR evidence.

Production deliberately exposes no API that authors these retired artifacts.
Tests which exercise the surviving readers write representative old rows here.
"""

from __future__ import annotations

from pathlib import Path

from app.engine.live.account_artifacts import account_artifacts_root
from app.engine.live.account_binding_ledger import (
    AccountBindingCommand,
    binding_command_ledger_path,
    read_account_binding_commands,
)
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

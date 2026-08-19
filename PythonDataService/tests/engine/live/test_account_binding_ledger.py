"""Read-only compatibility coverage for retired IBKR binding evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_binding_ledger import (
    AccountBindingCommand,
    binding_command_ledger_path,
    binding_ledger_parity,
    pending_binding_retirement_proposals,
    read_account_binding_commands,
)
from app.engine.live.account_registry import AccountInstanceBinding
from tests._helpers.legacy_ibkr_artifacts import write_historical_account_binding

ACCOUNT = "DU1234567"


def _binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=ACCOUNT,
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        lifecycle_state="ACTIVE",
        recorded_at_ms=100,
        source="historical-test",
    )


def test_historical_binding_ledger_and_registry_have_clean_read_parity(tmp_path: Path) -> None:
    binding = _binding()
    write_historical_account_binding(tmp_path, binding)

    commands = read_account_binding_commands(tmp_path, ACCOUNT)
    parity = binding_ledger_parity(
        tmp_path,
        account_id=ACCOUNT,
        legacy_bindings=[binding.model_dump(mode="json")],
    )

    assert [command.entry_kind for command in commands] == ["decision"]
    assert parity.is_clean


def test_pending_historical_retirement_proposal_remains_observable(tmp_path: Path) -> None:
    binding = _binding()
    proposal = AccountBindingCommand(
        seq=1,
        entry_kind="retirement_proposal",
        **binding.model_copy(update={"lifecycle_state": "RETIRED", "source": "historical-daemon"}).model_dump(
            mode="json"
        ),
    )
    path = binding_command_ledger_path(tmp_path, ACCOUNT)
    path.parent.mkdir(parents=True)
    path.write_text(proposal.model_dump_json() + "\n", encoding="utf-8")

    assert pending_binding_retirement_proposals(
        tmp_path,
        account_id=ACCOUNT,
    ) == (proposal,)


def test_malformed_historical_binding_row_fails_closed(tmp_path: Path) -> None:
    path = binding_command_ledger_path(tmp_path, ACCOUNT)
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(AccountArtifactError, match="invalid binding command row"):
        read_account_binding_commands(tmp_path, ACCOUNT)

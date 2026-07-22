"""Failure-injection seams for Clerk-owned account binding recovery."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import app.engine.live.account_registry as account_registry
from app.engine.live.account_binding_ledger import read_account_binding_commands
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    account_binding_ledger_parity,
    evaluate_account_instance_binding,
    fold_account_binding_retirements,
    latest_account_instance_binding,
    pending_account_binding_retirements,
    read_account_instance_registry,
    retire_unmanaged_active_bindings_on_daemon_boot,
    write_account_instance_binding,
    write_fenced_direct_cli_start_binding,
    write_fenced_lifecycle_retirement_binding,
)
from app.services.account_directory import AccountDirectoryService, CurrentBrokerAccount

ACCOUNT = "DU1157"


def _binding(
    *,
    strategy_instance_id: str = "spy-ema",
    run_id: str = "run-spy",
    recorded_at_ms: int = 100,
    lifecycle_state: str = "ACTIVE",
) -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=ACCOUNT,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        bot_order_namespace=f"learn-ai/{strategy_instance_id}/v1",
        lifecycle_state=lifecycle_state,
        recorded_at_ms=recorded_at_ms,
        source="test",
    )


def test_binding_decision_dual_write_has_clean_parity_and_ordered_replay(tmp_path: Path) -> None:
    active = _binding()
    retired = active.model_copy(update={"lifecycle_state": "RETIRED", "recorded_at_ms": 101})

    write_account_instance_binding(tmp_path, active)
    write_account_instance_binding(tmp_path, retired)

    commands = read_account_binding_commands(tmp_path, ACCOUNT)
    parity = account_binding_ledger_parity(tmp_path, account_id=ACCOUNT)

    assert [command.seq for command in commands] == [1, 2]
    assert [command.entry_kind for command in commands] == ["decision", "decision"]
    assert parity.is_clean


def test_direct_cli_compatibility_authority_accepts_only_its_scoped_active_transition(
    tmp_path: Path,
) -> None:
    binding = _binding().model_copy(update={"source": "run.start"})

    write_fenced_direct_cli_start_binding(tmp_path, binding)

    assert read_account_instance_registry(tmp_path, ACCOUNT) == [binding]
    with pytest.raises(ValueError, match="direct CLI binding authority"):
        write_fenced_direct_cli_start_binding(
            tmp_path,
            binding.model_copy(update={"lifecycle_state": "RETIRED"}),
        )


def test_lifecycle_retirement_authority_requires_its_pending_durable_transaction(
    tmp_path: Path,
) -> None:
    binding = _binding(
        lifecycle_state="RETIRED",
        recorded_at_ms=200,
    ).model_copy(update={"source": "lifecycle.retire"})
    transition_path = (
        tmp_path / "live_state" / binding.strategy_instance_id / "retirement_transition.json"
    )
    transition_path.parent.mkdir(parents=True)
    transition_path.write_text(
        json.dumps(
            {
                "strategy_instance_id": binding.strategy_instance_id,
                "state": "PENDING",
                "targets": [{"account_id": binding.account_id, "run_id": binding.run_id}],
            }
        ),
        encoding="utf-8",
    )

    write_fenced_lifecycle_retirement_binding(
        tmp_path,
        binding,
        transition_path=transition_path,
    )

    assert read_account_instance_registry(tmp_path, ACCOUNT) == [binding]
    transition_path.write_text(
        json.dumps(
            {
                "strategy_instance_id": binding.strategy_instance_id,
                "state": "COMMITTED",
                "targets": [{"account_id": binding.account_id, "run_id": binding.run_id}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OSError, match="does not authorize"):
        write_fenced_lifecycle_retirement_binding(
            tmp_path,
            binding,
            transition_path=transition_path,
        )


def test_legacy_dual_write_failure_leaves_an_observable_ledger_parity_defect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binding = _binding()

    def fail_legacy_write(_root: Path, _binding: AccountInstanceBinding) -> Path:
        raise OSError("injected legacy registry failure")

    monkeypatch.setattr(account_registry, "_write_account_instance_binding_legacy", fail_legacy_write)

    with pytest.raises(OSError, match="injected legacy registry failure"):
        write_account_instance_binding(tmp_path, binding)

    assert [command.entry_kind for command in read_account_binding_commands(tmp_path, ACCOUNT)] == ["decision"]
    parity = account_binding_ledger_parity(tmp_path, account_id=ACCOUNT)
    assert parity.ledger_only_instances == (binding.strategy_instance_id,)


def test_explicit_ledger_read_flip_replays_clerk_decisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = _binding()
    retired = active.model_copy(update={"lifecycle_state": "RETIRED", "recorded_at_ms": 101})
    write_account_instance_binding(tmp_path, active)
    write_account_instance_binding(tmp_path, retired)

    monkeypatch.setenv("ACCOUNT_BINDING_LEDGER_READ_ENABLED", "true")

    assert read_account_instance_registry(tmp_path, ACCOUNT) == [active, retired]


def test_explicit_ledger_read_flip_falls_back_until_the_legacy_corpus_has_parity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = _binding()
    write_account_instance_binding(tmp_path, active)
    legacy_only = _binding(strategy_instance_id="legacy-only", run_id="run-legacy")
    registry_path = tmp_path / "accounts" / ACCOUNT / "instance_registry.jsonl"
    with registry_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(legacy_only.model_dump(mode="json")) + "\n")

    monkeypatch.setenv("ACCOUNT_BINDING_LEDGER_READ_ENABLED", "true")

    assert read_account_instance_registry(tmp_path, ACCOUNT) == [active, legacy_only]


def test_daemon_downtime_proposal_blocks_admission_until_clerk_folds_in_order(tmp_path: Path) -> None:
    active = _binding()
    write_account_instance_binding(tmp_path, active)

    result = retire_unmanaged_active_bindings_on_daemon_boot(
        tmp_path,
        managed_run_ids=frozenset(),
        now_ms=200,
    )
    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id=ACCOUNT,
        strategy_instance_id=active.strategy_instance_id,
        run_id=active.run_id,
        bot_order_namespace=active.bot_order_namespace,
    )
    folded = fold_account_binding_retirements(tmp_path, account_id=ACCOUNT)
    latest = latest_account_instance_binding(
        read_account_instance_registry(tmp_path, ACCOUNT),
        account_id=ACCOUNT,
        strategy_instance_id=active.strategy_instance_id,
    )

    assert result.retirement_proposals_recorded == 1
    assert gate.operator_reason == "ACCOUNT_BINDING_RETIREMENT_PENDING"
    assert folded.retirements_applied == 1
    assert pending_account_binding_retirements(tmp_path, account_id=ACCOUNT) == ()
    assert latest is not None
    assert latest.lifecycle_state == "RETIRED"
    assert [command.entry_kind for command in read_account_binding_commands(tmp_path, ACCOUNT)] == [
        "decision",
        "retirement_proposal",
        "decision",
        "retirement_folded",
    ]


def test_account_service_contract_reports_pending_binding_retirement_proposal(tmp_path: Path) -> None:
    active = _binding()
    write_account_instance_binding(tmp_path, active)
    retire_unmanaged_active_bindings_on_daemon_boot(
        tmp_path,
        managed_run_ids=frozenset(),
        now_ms=200,
    )

    status = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=CurrentBrokerAccount(account_id=ACCOUNT, is_paper=True),
        now_ms=lambda: 300,
    ).service_status(account_id=ACCOUNT)

    assert status.binding.pending_retirement_proposals == 1
    assert status.binding.ledger_read_authority == "legacy_registry"
    assert status.binding.ledger_parity == "clean"
    assert status.binding.ledger_parity_issue_count == 0
    assert status.operating_state == "ATTENTION"
    assert status.headline == "Binding retirement reconciliation pending"


def test_account_service_contract_reports_dirty_binding_ledger_as_fail_closed(tmp_path: Path) -> None:
    active = _binding()
    write_account_instance_binding(tmp_path, active)
    registry_path = tmp_path / "accounts" / ACCOUNT / "instance_registry.jsonl"
    legacy_only = _binding(strategy_instance_id="legacy-only", run_id="run-legacy")
    with registry_path.open("a", encoding="utf-8") as fh:
        fh.write(legacy_only.model_dump_json() + "\n")

    status = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=CurrentBrokerAccount(account_id=ACCOUNT, is_paper=True),
        now_ms=lambda: 300,
    ).service_status(account_id=ACCOUNT)

    assert status.binding.ledger_read_authority == "legacy_registry"
    assert status.binding.ledger_parity == "dirty"
    assert status.binding.ledger_parity_issue_count == 1
    assert status.operating_state == "ATTENTION"
    assert status.headline == "Binding ledger parity needs attention"


def test_clerk_fold_never_retires_a_replacement_run_from_a_stale_proposal(tmp_path: Path) -> None:
    old = _binding(run_id="run-old", recorded_at_ms=100)
    replacement = _binding(run_id="run-new", recorded_at_ms=300)
    write_account_instance_binding(tmp_path, old)
    retire_unmanaged_active_bindings_on_daemon_boot(
        tmp_path,
        managed_run_ids=frozenset(),
        now_ms=200,
    )
    # This models a later, serialized deployment decision while the Clerk was
    # unavailable. The old liveness fact must not retire the replacement.
    write_account_instance_binding(tmp_path, replacement)

    folded = fold_account_binding_retirements(tmp_path, account_id=ACCOUNT)
    latest = latest_account_instance_binding(
        read_account_instance_registry(tmp_path, ACCOUNT),
        account_id=ACCOUNT,
        strategy_instance_id=old.strategy_instance_id,
    )

    assert folded.retirements_applied == 0
    assert folded.superseded_proposals == 1
    assert latest == replacement
    assert pending_account_binding_retirements(tmp_path, account_id=ACCOUNT) == ()


def test_concurrent_binding_decisions_are_serialized_with_one_deterministic_ledger_order(
    tmp_path: Path,
) -> None:
    bindings = tuple(
        _binding(
            strategy_instance_id=f"concurrent-{index}",
            run_id=f"run-{index}",
            recorded_at_ms=100 + index,
        )
        for index in range(24)
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda binding: write_account_instance_binding(tmp_path, binding), bindings))

    commands = read_account_binding_commands(tmp_path, ACCOUNT)
    parity = account_binding_ledger_parity(tmp_path, account_id=ACCOUNT)

    assert [command.seq for command in commands] == list(range(1, len(bindings) + 1))
    assert len(read_account_instance_registry(tmp_path, ACCOUNT)) == len(bindings)
    assert parity.is_clean


def test_concurrent_conflicting_binding_states_share_one_final_clerk_order(tmp_path: Path) -> None:
    active = _binding(recorded_at_ms=100, lifecycle_state="ACTIVE")
    retired = active.model_copy(
        update={"lifecycle_state": "RETIRED", "source": "concurrent-retirement"}
    )
    barrier = Barrier(2)

    def write_after_barrier(binding: AccountInstanceBinding) -> None:
        barrier.wait()
        write_account_instance_binding(tmp_path, binding)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_after_barrier, binding) for binding in (active, retired)]
        for future in futures:
            future.result()

    commands = read_account_binding_commands(tmp_path, ACCOUNT)
    latest = latest_account_instance_binding(
        read_account_instance_registry(tmp_path, ACCOUNT),
        account_id=ACCOUNT,
        strategy_instance_id=active.strategy_instance_id,
    )

    assert [command.seq for command in commands] == [1, 2]
    assert latest is not None
    assert latest.lifecycle_state == commands[-1].lifecycle_state
    assert latest.source == commands[-1].source

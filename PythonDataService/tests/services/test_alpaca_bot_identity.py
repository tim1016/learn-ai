from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.identity import strategy_instance_artifact_dir
from app.services.alpaca_bot_identity import (
    AlpacaBotIdentityGuard,
    AlpacaBotIdentityRefusedError,
)
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)


def _binding(artifacts_root: Path, *, broker: str) -> None:
    repository = BotBindingRepository(
        artifacts_root,
        instance_dir_for=lambda strategy_instance_id: strategy_instance_artifact_dir(
            artifacts_root,
            "live_state",
            strategy_instance_id,
        ),
    )
    repository.record_launch(
        BrokerBotBinding(
            strategy_instance_id="paper-guard",
            broker=broker,
            symbol="SPY",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id="run-1",
            created_at_ms=1,
        ),
        launch_reason="deploy",
    )


def _legacy_run_ledger(artifacts_root: Path, *, strategy_instance_id: str) -> None:
    run_dir = artifacts_root / "live_runs" / f"legacy-{strategy_instance_id}"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "strategy_instance_id": strategy_instance_id,
                "account_id": "DU123456",
            }
        ),
        encoding="utf-8",
    )


def test_sqlite_authority_with_no_conflicting_binding_is_alpaca(tmp_path: Path) -> None:
    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_sqlite_authority_accepts_matching_alpaca_binding(tmp_path: Path) -> None:
    _binding(tmp_path, broker="alpaca")

    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_missing_sqlite_authority_is_never_inferred_from_binding(tmp_path: Path) -> None:
    _binding(tmp_path, broker="alpaca")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="no active SQLite"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=False)


def test_non_alpaca_binding_conflicts_with_sqlite_authority(tmp_path: Path) -> None:
    _binding(tmp_path, broker="ibkr")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="non-Alpaca"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_legacy_ibkr_run_ledger_conflicts_with_sqlite_authority(tmp_path: Path) -> None:
    _legacy_run_ledger(tmp_path, strategy_instance_id="paper-guard")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="historical IBKR"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_unrelated_legacy_run_ledger_does_not_block_sqlite_authority(
    tmp_path: Path,
) -> None:
    _legacy_run_ledger(tmp_path, strategy_instance_id="other-paper-bot")

    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_unreadable_legacy_run_ledger_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "unreadable-legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_non_string_historical_strategy_identity_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "malformed-legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps({"strategy_instance_id": 42}),
        encoding="utf-8",
    )

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"strategy_instance_id": ""},
        {"strategy_instance_id": "   "},
    ],
)
def test_non_object_or_blank_historical_identity_fails_closed(
    tmp_path: Path,
    payload: object,
) -> None:
    run_dir = tmp_path / "live_runs" / "invalid-legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_symlinked_historical_run_ledger_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "symlinked-legacy-run"
    run_dir.mkdir(parents=True)
    target = tmp_path / "historical-ledger-target.json"
    target.write_text(json.dumps({"strategy_instance_id": "paper-guard"}), encoding="utf-8")
    (run_dir / "run_ledger.json").symlink_to(target)

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_broken_symlinked_historical_run_ledger_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "broken-symlink-legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").symlink_to(tmp_path / "missing-ledger.json")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_broken_symlinked_live_runs_root_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "live_runs").symlink_to(tmp_path / "missing-live-runs", target_is_directory=True)

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_symlinked_historical_run_directory_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "historical-run-target"
    target.mkdir()
    (target / "run_ledger.json").write_text(
        json.dumps({"strategy_instance_id": "other-paper-bot"}),
        encoding="utf-8",
    )
    live_runs_root = tmp_path / "live_runs"
    live_runs_root.mkdir()
    (live_runs_root / "symlinked-run").symlink_to(target, target_is_directory=True)

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable historical"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_unreadable_binding_fails_closed(tmp_path: Path) -> None:
    instance_dir = strategy_instance_artifact_dir(tmp_path, "live_state", "paper-guard")
    instance_dir.mkdir(parents=True)
    (instance_dir / "broker_binding.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)

from __future__ import annotations

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


def test_sqlite_authority_with_no_conflicting_binding_is_alpaca(tmp_path: Path) -> None:
    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


def test_sqlite_authority_accepts_matching_alpaca_binding(tmp_path: Path) -> None:
    _binding(tmp_path, broker="alpaca")

    AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)


@pytest.mark.parametrize(
    ("binding_broker", "sqlite_claim", "refusal"),
    [
        pytest.param("alpaca", False, "no active SQLite", id="missing-sqlite-authority-is-never-inferred-from-binding"),
        pytest.param("ibkr", True, "non-Alpaca", id="non-alpaca-binding-conflicts-with-sqlite-authority"),
    ],
)
def test_a_binding_that_disagrees_with_the_sqlite_claim_is_refused(
    tmp_path: Path, binding_broker: str, sqlite_claim: bool, refusal: str
) -> None:
    _binding(tmp_path, broker=binding_broker)

    with pytest.raises(AlpacaBotIdentityRefusedError, match=refusal):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=sqlite_claim)


def test_unreadable_binding_fails_closed(tmp_path: Path) -> None:
    instance_dir = strategy_instance_artifact_dir(tmp_path, "live_state", "paper-guard")
    instance_dir.mkdir(parents=True)
    (instance_dir / "broker_binding.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(AlpacaBotIdentityRefusedError, match="unreadable"):
        AlpacaBotIdentityGuard(tmp_path).require("paper-guard", sqlite_claim=True)

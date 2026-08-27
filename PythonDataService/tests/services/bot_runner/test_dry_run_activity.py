"""Dry Run activity journal and projection tests.

Split from ``tests/services/test_bot_runner.py`` (issue #1737).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_registry_projection import read_dry_run_activity
from tests._helpers.bot_runner.custody import _SID, _T0, _registry
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.bot_runner.ema_parity import _ema_parity_bars_through_first_exit

from ._support import _install_fake_clerk, _wait_for


@pytest.fixture
def _isolated_synthetic_authority() -> None:
    """Keep process-scoped synthetic Clerk state out of neighbouring tests."""
    from app.broker.alpaca.clerk.active_authority import reset_alpaca_clerk_for_testing

    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


def test_dry_run_activity_projection_excludes_prior_run_rows(tmp_path: Path) -> None:
    journal = DryRunActivityJournal(tmp_path)
    for run_id, seq in (("run-prior", 1), ("run-current", 2)):
        journal.append(
            DryRunActivity(
                seq=seq,
                strategy_instance_id=_SID,
                run_id=run_id,
                authority_account_id=f"sim:{_SID}",
                authority_kind="synthetic",
                recorded_at_ms=seq * 1_000,
                bar_ref=f"SPY@{seq * 1_000}",
                intent="ENTER",
                order_ref=f"simulated:{run_id}",
                symbol="SPY",
                side="buy",
                quantity=1,
                fill_price=400,
            )
        )
    binding = BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="dry_run",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )

    activity = read_dry_run_activity(binding, tmp_path, limit=8)

    assert [row.run_id for row in activity] == ["run-current"]


@pytest.mark.asyncio
async def test_dry_run_records_simulated_round_trip_with_zero_broker_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_synthetic_authority: None,
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    bars = _ema_parity_bars_through_first_exit()
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="dry_run",
        quantity=3,
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: len(registry.dry_run_activity("alpaca", _SID)) >= 2)

    activity = registry.dry_run_activity("alpaca", _SID)
    assert deployed.mode == "dry_run"
    assert clerk.calls == []
    assert [(row.intent, row.side, row.quantity, row.simulated) for row in activity[:2]] == [
        ("ENTER", "buy", 3.0, True),
        ("EXIT", "sell", 3.0, True),
    ]
    assert {(row.authority_account_id, row.authority_kind) for row in activity} == {
        (f"sim:{_SID}", "synthetic")
    }
    # The panel suffix is derived from the synthetic Clerk's real custody
    # operations, not a runner-minted simulated order namespace.
    assert all(not row.order_ref.startswith("simulated:") for row in activity)
    from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
    from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
    from app.services.source_bar_ledger import SourceBarLedger

    account_id = synthetic_account_id_for_strategy(_SID)
    runtime = get_clerk_runtime(account_id)
    assert runtime is not None
    assert runtime.authority_kind == "synthetic"
    assert runtime.selected_account_id == account_id
    retained = SourceBarLedger(artifacts_root=tmp_path, account_id=account_id)
    assert len(retained.bars(provider="lean-golden", symbol="SPY")) == len(bars)
    assert all(row.bar_ref.startswith(f"source-bar:{account_id}:") for row in activity)
    # The EMA's 15-minute decision arrives when the following raw minute
    # flushes its consolidator. A latest-bar fill would therefore price the
    # following minute, not the decision close. Each journal receipt must
    # name and price the unique durable bar at the intent's clock.
    for row in activity:
        decision_bar = retained.find_by_closed_end(
            provider="lean-golden",
            symbol="SPY",
            end_ms=row.recorded_at_ms,
        )
        assert decision_bar is not None
        assert (row.bar_ref, row.fill_price) == (decision_bar.bar_ref, float(decision_bar.close))
    await registry.stop("alpaca", _SID)


def test_dry_run_activity_journal_lifts_legacy_authority_fields(tmp_path: Path) -> None:
    """Pre-authority journal rows remain readable after the schema extension."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    (instance_dir / "dry_run_activity.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "strategy_instance_id": _SID,
                "run_id": "run-legacy",
                "recorded_at_ms": _T0,
                "bar_ref": "SPY@1700000000000",
                "intent": "ENTER",
                "order_ref": "simulated:legacy",
                "symbol": "SPY",
                "side": "buy",
                "quantity": 1.0,
                "fill_price": 400.0,
                "simulated": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = DryRunActivityJournal(instance_dir).tail(1)

    assert rows[0].authority_account_id == f"sim:{_SID}"
    assert rows[0].authority_kind == "synthetic"

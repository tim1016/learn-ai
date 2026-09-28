"""#2543: limits change explicitly; standing loss facts survive that change."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, RiskRevisionConflict
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.contract.models import BrokerAccountSnapshot
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _Read

RiskContext = tuple[ClerkSqliteRepository, LiveEnvelopeSync, _Read, _TestClock]


def _policy(revision: int = 1, cap: float = 100.0) -> AccountRiskPolicy:
    return AccountRiskPolicy(revision, .1, cap, "profile", 1, "owner", NOON)


def _hold(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None)


@pytest.fixture
async def risk_context(tmp_path: Path) -> AsyncIterator[RiskContext]:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-RISK", artifacts_root=tmp_path, clock=clock)
    read = _Read(unrealized=-150)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    sync = LiveEnvelopeSync(repo=repo, read=read, envelope=gate)
    try:
        yield repo, sync, read, clock
    finally:
        await sync.stop()
        repo.close()


async def test_paper_unknown_until_explicit_apply_tightening_raises_hold(risk_context: RiskContext) -> None:
    repo, sync, _read, _ = risk_context
    assert await sync.tick() == "unknown"
    assert sync.envelope.latest_observation() is None
    sync.apply_risk_policy(_policy(), expected_revision=0)
    assert _hold(repo) is not None
    assert sync.envelope.latest_observation() is None
    cause = json.loads(_hold(repo)["facts_json"])["cause_facts"]
    assert cause["policy_revision"] == 1
    assert cause["loss_limit_usd"] == 100
    assert cause["last_equity_usd"] == 100_000


async def test_loosen_does_not_clear_or_rewrite_original_hold(risk_context: RiskContext) -> None:
    repo, sync, read, _ = risk_context
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    original = _hold(repo)
    sync.apply_risk_policy(_policy(2, 200), expected_revision=1)
    assert _hold(repo) == original
    reading = await sync.observe()
    outcome, _ = sync.clear_observed_loss_hold(reading)
    assert outcome == "held"
    read.unrealized = -99
    reading = await sync.observe()
    assert sync.clear_observed_loss_hold(reading)[0] == "cleared"
    assert _hold(repo) is None


async def test_stale_apply_and_clear_cannot_overwrite_current_policy(risk_context: RiskContext) -> None:
    repo, sync, read, _ = risk_context
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    read.unrealized = -50
    reviewed = await sync.observe()
    sync.apply_risk_policy(_policy(2, 200), expected_revision=1)
    with pytest.raises(RiskRevisionConflict):
        sync.apply_risk_policy(_policy(2, 300), expected_revision=1)
    assert repo.account_risk_policy().loss_usd == 200
    assert sync.clear_observed_loss_hold(reviewed)[0] == "unknown"
    assert _hold(repo) is not None


async def test_unknown_evidence_cannot_clear_and_apply_does_not_claim_ready(risk_context: RiskContext) -> None:
    repo, sync, read, _ = risk_context
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    read.last_equity = None
    reading = await sync.observe()
    sync.apply_risk_policy(_policy(2, 200), expected_revision=1)
    assert sync.envelope.latest_observation() is None
    assert sync.clear_observed_loss_hold(reading)[0] == "unknown"
    assert _hold(repo) is not None


async def test_rollover_keeps_original_window_and_baseline(risk_context: RiskContext) -> None:
    repo, sync, read, clock = risk_context
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    cause = json.loads(_hold(repo)["facts_json"])["cause_facts"]
    clock.advance(86_400_000)
    repo.revive_execution_lease()
    read.last_equity = 2_000_000
    sync.apply_risk_policy(replace(_policy(2, 200), applied_at_ms=clock()), expected_revision=1)
    reading = await sync.observe()
    assert sync.clear_observed_loss_hold(reading)[0] == "unknown"
    assert json.loads(_hold(repo)["facts_json"])["cause_facts"] == cause


async def test_restarted_and_rebuilt_authority_preserves_policy_and_hold(tmp_path: Path) -> None:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-RISK", artifacts_root=tmp_path, clock=clock)
    sync = LiveEnvelopeSync(repo=repo, read=_Read(unrealized=-150), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    original_hold = _hold(repo)
    await sync.stop()
    db = repo.db_path
    repo.close()
    reopened = ClerkSqliteRepository.open(account_id="PA-RISK", artifacts_root=tmp_path, clock=clock)
    assert reopened.account_risk_policy() == _policy()
    assert _hold(reopened) == original_hold
    reopened.close()
    db.rename(db.with_suffix(".preserved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="PA-RISK", artifacts_root=tmp_path, clock=clock)
    try:
        assert rebuilt.account_risk_policy() == _policy()
        assert _hold(rebuilt) == original_hold
    finally:
        rebuilt.close()


async def test_observation_started_before_apply_publishes_only_current_revision(risk_context: RiskContext) -> None:
    import asyncio

    repo, sync, read, _ = risk_context
    read.unrealized = -50
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    started, finish = asyncio.Event(), asyncio.Event()
    original_read = read.get_account

    async def delayed_read() -> BrokerAccountSnapshot:
        started.set()
        await finish.wait()
        return await original_read()

    read.get_account = delayed_read
    task = asyncio.create_task(sync.observe())
    await started.wait()
    sync.apply_risk_policy(_policy(2, 25), expected_revision=1)
    finish.set()
    reading = await task
    assert reading.policy_revision == 2
    assert reading.breached is True
    assert sync.envelope.latest_observation() is None
    assert json.loads(_hold(repo)["facts_json"])["cause_facts"]["policy_revision"] == 2


async def test_stale_published_observation_cannot_admit_after_policy_change(risk_context: RiskContext) -> None:
    from app.broker.alpaca.clerk.sqlite.envelope_admission import require_envelope_admission
    from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
    from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce

    repo, sync, read, _ = risk_context
    read.unrealized = 0
    await sync.observe()
    sync.apply_risk_policy(_policy(), expected_revision=0)
    old = sync.envelope.latest_observation()
    sync.apply_risk_policy(_policy(2, 200), expected_revision=1)
    sync.envelope.publish(old)
    with pytest.raises(AdmissionBlockedError):
        require_envelope_admission(repo, envelope=sync.envelope, reference_price=10, now_ms=NOON,
            leg=BrokerOrderLeg(symbol="SPY", side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=1, time_in_force=TimeInForce.DAY))


async def test_explicit_new_session_clear_releases_realized_loss_only_after_obligations_resolve(
    day_pnl_repo: ClerkSqliteRepository, day_pnl_clock: _TestClock,
) -> None:
    from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
    from app.broker.contract.models import BrokerOrder
    from tests.broker.alpaca.clerk.sqlite.conftest import (
        _accept_day_pnl_enter,
        _append_day_pnl_slice,
        _broker_order_fixture,
    )

    class FlatRead(_Read):
        async def list_orders(self, *, status: str, limit: int) -> list[BrokerOrder]:
            return []

    accepted = _accept_day_pnl_enter(day_pnl_repo, decision_id="loss-before-clear")
    for identity, side, price in (("buy-loss", "BUY", 100), ("sell-loss", "SELL", 80)):
        _append_day_pnl_slice(day_pnl_repo, accepted, execution_id=identity, side=side,
            quantity=10, price=price, occurred_at_ms=NOON, fee=0, fee_fidelity="reported")
    sync = LiveEnvelopeSync(repo=day_pnl_repo, read=FlatRead(), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    try:
        await sync.observe()
        sync.apply_risk_policy(_policy(), expected_revision=0)
        original_hold = _hold(day_pnl_repo)
        assert original_hold is not None
        day_pnl_clock.advance(86_400_000)
        day_pnl_repo.revive_execution_lease()
        reading, quiet = await sync.observe_loss_clearance()
        assert reading.day_pnl.total_usd == 0
        assert sync.clear_observed_loss_hold(reading, quiet=quiet)[0] == "held"
        assert _hold(day_pnl_repo) == original_hold
        fold_order_acknowledgement(day_pnl_repo, effect_operation_id=accepted.effect_operation_id,
            order=_broker_order_fixture(accepted.order_ref, status="filled", filled_quantity=20, quantity=20, filled_avg_price=90))
        reading, quiet = await sync.observe_loss_clearance()
        assert sync.clear_observed_loss_hold(reading, quiet=quiet)[0] == "cleared"
        resolution = day_pnl_repo.custody_transitions()[-1]
        basis = json.loads(resolution["facts_json"])["loss_hold_clear_basis"]
        assert basis["session_reset"] is True
        assert basis["current_session_start_ms"] > basis["original_session_start_ms"]
        assert basis["original_loss_limit_usd"] == 100
        assert basis["original_baseline_usd"] == 100_000
        assert basis["current_day_pnl_usd"] == 0
    finally:
        await sync.stop()

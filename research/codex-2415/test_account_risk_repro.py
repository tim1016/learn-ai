"""Independent review reproductions; synthetic ports and private SQLite only.

The failing assertions state the intended cash-only/daily-loss invariants.
They are intentionally not product fixes. Run with the review guard launcher.
"""
from __future__ import annotations

import asyncio

import pytest

from app.broker.alpaca.clerk.sqlite import repository as repository_module
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_ACCOUNT_ID, ENVELOPE_T0, _clock_at, _register_active, _broker_position_fixture,
)
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice, _observed_order


def _gate(clock, cash):
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False)
    gate.publish(AccountObservation(clock(), cash, cash, cash, 0.0, 0))
    return gate


def _accept(repo, gate, *, decision_id, quantity=10, sid="review-bot", symbol="SPY"):
    return accept_enter(
        repo, account_id=ENVELOPE_ACCOUNT_ID, strategy_instance_id=sid,
        decision_id=decision_id, lifecycle_run_id=f"{sid}-run",
        leg=BrokerOrderLeg(symbol=symbol, side="buy", quantity=quantity),
        envelope=gate, reference_price=100.0,
    )


@pytest.mark.asyncio
async def test_cash_read_must_cover_fills_before_reservations_are_released(tmp_path):
    clock = _clock_at(ENVELOPE_T0)
    repo = repository_module.ClerkSqliteRepository.initialize(
        account_id=ENVELOPE_ACCOUNT_ID, artifacts_root=tmp_path, clock=clock,
    )
    _register_active(repo, clock, strategy_instance_id="review-bot", symbol="SPY", run_id="review-bot-run")
    _register_active(repo, clock, strategy_instance_id="review-two", symbol="QQQ", run_id="review-two-run")
    gate = _gate(clock, 1000.0)
    first = _accept(repo, gate, decision_id="first")

    class SplitRead(_LiveBroker):
        async def get_account(self):
            account = await super().get_account()
            return account.model_copy(update={"account_id": ENVELOPE_ACCOUNT_ID})

        async def list_positions(self):
            # get_account has returned the pre-fill $1000 snapshot. The
            # independent execution callback lands while positions is pending.
            await asyncio.sleep(0)
            clock.advance(1000)
            _append_slice(repo, first, execution_id="exec-review", quantity=10,
                          source_event_at_ms=clock())
            self.unrealized = 0.0
            clock.advance(1000)
            return [_broker_position_fixture("SPY", quantity=10)]

    read = SplitRead(now_ms=clock(), cash=1000.0)
    sync = LiveEnvelopeSync(repo=repo, read=read, envelope=gate)
    try:
        reading = await sync.observe()
        assert reading.observation.observed_at_ms == ENVELOPE_T0 + 2000
        assert repo.position("review-bot", "SPY") == 10.0
        assert repo.reserved_cash_usd(observed_at_ms=ENVELOPE_T0) == 1000.0
        assert repo.reserved_cash_usd(observed_at_ms=reading.observation.observed_at_ms) == 0.0
        # Actual cash is zero after the exact $1000 fill; no new buy is safe.
        with pytest.raises(AdmissionBlockedError) as refused:
            _accept(repo, gate, decision_id="second", sid="review-two", symbol="QQQ")
        assert refused.value.decision.reason_code == "LIVE_ENVELOPE_CASH_EXCEEDED"
    finally:
        await sync.stop()
        repo.close()


def test_cash_only_acceptance_requires_a_bounded_execution_price(tmp_path):
    clock = _clock_at(ENVELOPE_T0)
    repo = repository_module.ClerkSqliteRepository.initialize(
        account_id=ENVELOPE_ACCOUNT_ID, artifacts_root=tmp_path, clock=clock,
    )
    try:
        _register_active(repo, clock, strategy_instance_id="review-bot", symbol="SPY", run_id="review-bot-run")
        accepted = _accept(repo, _gate(clock, 1000.0), decision_id="market-at-all-cash")
        assert accepted.created
        assert repo.reserved_cash_usd(observed_at_ms=clock()) == 1000.0
        # Fold a synthetic permitted market fill at $101 (1% adverse).
        clock.advance(1000)
        fold_order_evidence(
            repo, effect_operation_id=accepted.effect_operation_id,
            order=_observed_order(accepted.order_ref, status="filled", filled_quantity=10,
                                 filled_avg_price=101.0, source_event_at_ms=clock()),
        )
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            spent = reader.account_net_cash_spent_usd()
            assert spent <= 1000.0
        finally:
            reader.close()
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_daily_loss_must_measure_change_from_prior_close(tmp_path):
    clock = _clock_at(ENVELOPE_T0)
    repo = repository_module.ClerkSqliteRepository.initialize(
        account_id=ENVELOPE_ACCOUNT_ID, artifacts_root=tmp_path, clock=clock,
    )
    _register_active(repo, clock, strategy_instance_id="review-bot", symbol="SPY", run_id="review-bot-run")
    gate = _gate(clock, 100_000.0)
    accepted = _accept(repo, gate, decision_id="carried-buy", quantity=100)
    _append_slice(repo, accepted, execution_id="exec-prior-day", quantity=100,
                  source_event_at_ms=clock() - 3 * 86_400_000)
    # 100 shares cost $100; prior close was $200 and today's mark is $150.
    # Lifetime unrealized is +$5000; today's loss is $5000.
    class CarriedPosition(_LiveBroker):
        async def get_account(self):
            account = await super().get_account()
            return account.model_copy(update={
                "account_id": ENVELOPE_ACCOUNT_ID,
                "last_equity": 110_000.0, "equity": 105_000.0,
            })

        async def list_positions(self):
            position = _broker_position_fixture("SPY", quantity=100)
            return [position.model_copy(update={
                "current_price": 150.0, "market_value": 15_000.0, "unrealized_pl": 5_000.0,
            })]

    read = CarriedPosition(now_ms=clock(), cash=90_000.0, unrealized=5_000.0)
    sync = LiveEnvelopeSync(repo=repo, read=read, envelope=gate)
    try:
        reading = await sync.observe()
        assert reading.day_pnl is not None and reading.day_pnl.known
        assert reading.day_pnl.total_usd == -5000.0
    finally:
        await sync.stop()
        repo.close()

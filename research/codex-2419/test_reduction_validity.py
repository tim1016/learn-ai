"""Synthetic reduction validity probes at the real durable EXIT boundary.

No network or service is involved. The program session-boundary invariant
fails; recovery controls characterize the documented session validity policy.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.sqlite import repository as repository_module
from app.broker.alpaca.clerk.program_leg import regular_session_shape
from app.broker.alpaca.clerk.recovery_reduction import ConfirmedRecoveryLimit, recovery_reduction_shape
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, accept_recovery_exit, resolve_exit
from app.broker.contract.models import OrderSide, OrderType
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID, RUN_ID, SID, _FakeTrade, _make_entry,
)
from tests.broker.alpaca.clerk.test_recovery_reduction import _at, _quote, _POLICY


def _repository(tmp_path, now):
    clock = _clock_at(now)
    repo = repository_module.ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000,
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="review")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True], ids=["program", "recovery-control"])
async def test_no_unpriced_market_reduction_is_sent_after_regular_close(tmp_path, recovery):
    repo = _repository(tmp_path, _at(15, 59))
    try:
        entry = await _make_entry(repo, status="filled", filled_quantity=10)
        if recovery:
            accepted = accept_recovery_exit(
                repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
                decision_id="recovery-flatten-review", entry_order_ref=entry,
            )
        else:
            accepted = accept_exit(
                repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
                decision_id="program-review", lifecycle_run_id=RUN_ID, entry_order_ref=entry,
                reducing_shape=regular_session_shape(OrderSide.SELL),
            )
        _walk_clock_to(repo, _at(16, 1))
        trade = _FakeTrade()
        await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
        assert repo.position(SID, "SPY") == 10
        if trade.submit_calls:
            leg, _ = trade.submit_calls[0]
            assert leg.order_type is OrderType.MARKET and not leg.extended_hours
        assert trade.submit_calls == [], "Unpriced reducing market order was submitted after close"
    finally:
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("end_session", [False, True], ids=["accepted-quote-policy", "session-expiry-control"])
async def test_accepted_recovery_limit_keeps_its_price_until_session_expiry(tmp_path, end_session):
    now = _at(17)
    repo = _repository(tmp_path, now)
    try:
        entry = await _make_entry(repo, status="filled", filled_quantity=10)
        shape = recovery_reduction_shape(
            side=OrderSide.SELL, symbol="SPY", quantity=10, now_ms=now,
            policy=_POLICY,
            confirmed=ConfirmedRecoveryLimit(limit_price=Decimal("99.80"), quote_observed_at_ms=now),
            current_quote=_quote(observed_at_ms=now),
        )
        assert shape is not None
        accepted = accept_recovery_exit(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
            decision_id="recovery-flatten-quote-review", entry_order_ref=entry, confirmed_shape=shape,
        )
        # An accepted effect resumes later (e.g. its prior lookup timed out).
        # No new broker quote is available to this state-machine invocation.
        _walk_clock_to(repo, _at(20, 1) if end_session else now + 20_000)
        trade = _FakeTrade()
        await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
        if end_session:
            assert trade.submit_calls == []
        else:
            assert len(trade.submit_calls) == 1
            leg, _ = trade.submit_calls[0]
            assert leg.order_type is OrderType.LIMIT
            assert leg.extended_hours
            assert leg.limit_price == 99.8
    finally:
        repo.close()

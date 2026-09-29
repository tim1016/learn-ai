"""#2596 through the runner: a regular-hours ENTER decided on the session's last bar is refused.

The last bar closes *at* the regular close, so its decision lands a fraction of
a second after it. Alpaca's clock is read once a second, and its last answer
before the close still says OPEN -- inside the 5-second freshness bound for the
first seconds after the close. That answer used to carry the ENTER through the
strategy gate, the Clerk's recheck and the check before broker contact as a
market DAY order, which Alpaca queues for the next open: an overnight entry
nobody decided. The answer names the close it expires at, and every gate now
reads it.

The real ``run_trade_bot`` drives the sealed ``deployment_validation`` program
into a real ``SqliteAlpacaClerkFacade`` over a fake broker. That program stops
entering 15 minutes before the close, so this suite moves its barrier past the
close to let a two-green ENTER land on the last bar -- the shape a 15-minute
program's 15:45-16:00 bucket has in production.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.engine.strategy.algorithms.deployment_validation as deployment_validation
import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeFeed, _SqliteRuntimeBroker
from tests._helpers.bot_runner.market import clock_read_before_the_close, patch_wall_clock_to_the_fed_bar
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _green_bar

_ACCOUNT_ID = "PA-TEST"
# A live decision lands this long after its bar closes (the issue's estimate).
_DECISION_DELAY_MS = 600
# The replayed clock jumps a bar at a time with no lease heartbeat in between;
# the Clerk's execution lease must outlive the span.
_REPLAY_LEASE_TTL_MS = 60 * 60_000

_POLICY = ProgramLegPolicy(
    window=ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60),
    allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
)


class _RecordingBroker(_SqliteRuntimeBroker):
    """Keeps every leg the Clerk put on the wire."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted_legs: list[BrokerOrderLeg] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submitted_legs.append(leg)
        return await super().submit(leg, client_order_id=client_order_id)


def _regular_hours_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id=_ACCOUNT_ID,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )


async def _run_two_greens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enter_bar_end_ms: int,
    close_ms: int,
) -> tuple[_RecordingBroker, list[dict[str, object]]]:
    """Feed two green bars, the second ending at ``enter_bar_end_ms``; return the wire and the receipts."""
    # Let the program's two-green ENTER land on the last bar (see the module note).
    monkeypatch.setattr(deployment_validation, "_STOP_AND_FLATTEN_OFFSET_MS", -60_000)
    first_green_end_ms = enter_bar_end_ms - 60_000
    # Seed before acquiring the lease: no future-dated lease can mask expiry.
    patch_wall_clock_to_the_fed_bar(
        monkeypatch, start_ms=first_green_end_ms, decision_delay_ms=_DECISION_DELAY_MS
    )
    liveness = clock_read_before_the_close(close_ms)
    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", liveness)
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path / "clerk",
        clock=lambda: feed_continuity_policy.now_ms_utc(),
        lease_ttl_ms=_REPLAY_LEASE_TTL_MS,
    )
    broker = _RecordingBroker()
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=broker, trade=broker, account_mode="paper", program_leg_policy=_POLICY
    )
    binding = _regular_hours_binding()
    await facade.register_strategy_run(binding)
    feed = _FakeFeed([_green_bar(first_green_end_ms), _green_bar(enter_bar_end_ms)], mode="finite")
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id=_ACCOUNT_ID)
    set_alpaca_clerk(facade)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger)
        await facade.drain_effects()
        receipts = [
            {"outcome": receipt.outcome, **json.loads(receipt.facts_json)}
            for receipt in SqliteDecisionReceipts(repo, strategy_instance_id=_SID).tail(20)
        ]
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()
    return broker, receipts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day",
    [
        pytest.param(date(2024, 1, 2), id="regular-close"),
        # The canonical calendar closes this day early; nothing here names the hour.
        pytest.param(date(2024, 11, 29), id="early-close"),
    ],
)
async def test_a_regular_hours_enter_decided_on_the_last_bar_is_refused_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: date
) -> None:
    """The clock was read OPEN just before the close; the decision lands after it."""
    close_ms = session_close_ms_utc(day)

    broker, receipts = await _run_two_greens(
        tmp_path, monkeypatch, enter_bar_end_ms=close_ms, close_ms=close_ms
    )

    assert broker.submitted_legs == []
    last = receipts[-1]
    assert (last["outcome"], last["reason_code"], last["decision_bar_close_ms"]) == (
        "blocked",
        "MARKET_CLOSED",
        close_ms,
    )
    liveness = last["market_liveness"]
    assert liveness["state"] == "CLOSED"
    assert liveness["market_clock"]["state"] == "OPEN"
    assert liveness["market_clock"]["next_close_ms"] == close_ms
    assert liveness["reason"] == (
        "The regular session has closed. The broker clock was last read before the close, "
        "so it no longer shows the market open."
    )


@pytest.mark.asyncio
async def test_a_regular_hours_enter_decided_mid_session_still_goes_out_as_a_market_day_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same clock, read inside the session, keeps admitting an ordinary ENTER."""
    day = date(2024, 1, 2)

    broker, receipts = await _run_two_greens(
        tmp_path,
        monkeypatch,
        enter_bar_end_ms=session_open_ms_utc(day) + 31 * 60_000,
        close_ms=session_close_ms_utc(day),
    )

    (leg,) = broker.submitted_legs
    assert (leg.side, leg.order_type, leg.time_in_force, leg.extended_hours) == (
        OrderSide.BUY,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    assert receipts[-1]["outcome"] != "blocked"

"""#2440 through the runner: a regular-hours run's last-bar EXIT leaves as an after-hours limit.

The Clerk's own suites pin the rule on the facade
(``test_runtime_program_leg.py``). This drives it end to end instead: the real
``run_trade_bot`` runs the sealed ``deployment_validation`` program on a
``use_rth=True`` binding, enters, and decides its EXIT on the bar that closes
at the regular close -- so the EXIT reaches a real ``SqliteAlpacaClerkFacade``
after the session has ended. A market DAY leg sent then is one Alpaca queues
for the next open; the Clerk must send the extended-hours DAY limit instead,
priced off the decision bar's close and the exit allowance.

The package harness (``tests/_helpers/bot_runner/market.py``) forces
``recovery_reduction.regular_session_open`` to ``True`` so replayed round
trips pass whatever the host's clock says. That force would let a queued
market leg through here, so this test restores the canonical calendar's
judgement and pins the Clerk's clock to the fed bar instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.recovery_reduction as recovery_reduction
import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import regular_session_open
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition, OrderSide, OrderType, TimeInForce
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeFeed, _SqliteRuntimeBroker

from ._support import _green_bar, _trade_bar

_ACCOUNT_ID = "PA-TEST"
# 2024-01-02: a regular NYSE session, closing at 16:00 ET by the canonical calendar.
_CLOSE_MS = session_close_ms_utc(date(2024, 1, 2))
# Two greens complete the program's entry streak at 15:44 ET. The feed then
# skips to the day's last minute: deployment_validation liquidates a held
# position on the first bar at or past its flatten barrier (close - 15 min),
# so the bar that closes at the regular close is where this EXIT is decided.
_FIRST_GREEN_END_MS = _CLOSE_MS - 17 * 60_000
_ENTER_END_MS = _CLOSE_MS - 16 * 60_000
_LAST_BAR_CLOSE = "400.00"
# A live EXIT reaches the Clerk seconds after its bar closes, never at the close.
_SEND_DELAY_MS = 2_000
# The replayed clock jumps a bar at a time, 16 minutes at the skip, with no
# lease heartbeat in between; the Clerk's execution lease must outlive the span.
_REPLAY_LEASE_TTL_MS = 60 * 60_000

_POLICY = ProgramLegPolicy(
    window=ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60),
    allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
)


class _EntryFillingBroker(_SqliteRuntimeBroker):
    """An ENTER fills on submit; a reducing leg rests. Every leg sent is kept.

    ``submitted_legs`` is what the Clerk put on the wire -- the base double
    echoes every order back as a market order whatever leg it was handed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.submitted_legs: list[BrokerOrderLeg] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submitted_legs.append(leg)
        order = await super().submit(leg, client_order_id=client_order_id)
        if leg.side is OrderSide.BUY:
            order = order.model_copy(
                update={
                    "status": "filled",
                    "filled_quantity": leg.quantity,
                    "filled_avg_price": 401.0,
                    "filled_at_ms": _T0,
                }
            )
            self.orders[client_order_id] = order
        return order

    async def list_positions(self) -> list[BrokerPosition]:
        if not any(order.status == "filled" for order in self.orders.values()):
            return []
        return [BrokerPosition(
            broker="alpaca", symbol="SPY", asset_id=None, asset_class="us_equity",
            quantity=1, side="long", average_entry_price=401, market_value=400,
            cost_basis=401, current_price=400, unrealized_pl=-1, unrealized_plpc=None,
            observed_at_ms=_clerk_clock(),
        )]

    async def cancel(self, order_id: str) -> None:
        """A filled order is not cancelable; the Clerk proves its state by exact lookup."""
        if any(order.order_id == order_id and order.status == "filled" for order in self.orders.values()):
            self.cancellations.append(order_id)
            return
        await super().cancel(order_id)


def _clerk_clock() -> int:
    """The Clerk's clock: the fed bar's close plus the EXIT's transit to the Clerk.

    Read through ``feed_continuity_policy`` so the package's autouse
    ``patch_wall_clock_to_the_fed_bar`` pin is what this sees.
    """
    return feed_continuity_policy.now_ms_utc() + _SEND_DELAY_MS


def _regular_hours_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_snapshot", [False, True])
async def test_a_regular_hours_exit_decided_on_the_last_bar_leaves_as_an_after_hours_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_snapshot: bool
) -> None:
    """The last bar's EXIT is the extended-hours DAY limit, never a market order queued for the open."""
    # Undo the harness's always-open session (module docstring): the send-time
    # rule judges the Clerk's clock by the canonical calendar, as in production.
    monkeypatch.setattr(recovery_reduction, "regular_session_open", regular_session_open)
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path / "clerk",
        clock=_clerk_clock,
        lease_ttl_ms=_REPLAY_LEASE_TTL_MS,
    )
    broker = _EntryFillingBroker()
    if stale_snapshot:
        original_submit = broker.submit

        async def submit_then_lose_snapshot(leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
            order = await original_submit(leg, client_order_id=client_order_id)
            if leg.side is OrderSide.BUY:
                raise_uncertainty(
                    repo, strategy_instance_id=None, reason_code="BROKER_SNAPSHOT_STALE",
                    headline="Broker account truth is unavailable",
                    explanation="The account snapshot failed after the entry filled.",
                    operator_impact="Unproven reductions are paused.",
                    next_step="Reconcile after connectivity recovers.",
                    cause_facts={"snapshot": "open_orders_and_positions"}, severity="error",
                )
            return order

        monkeypatch.setattr(broker, "submit", submit_then_lose_snapshot)
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=broker, trade=broker, account_mode="paper", program_leg_policy=_POLICY
    )
    binding = _regular_hours_binding()
    await facade.register_strategy_run(binding)
    feed = _FakeFeed(
        [
            _green_bar(_FIRST_GREEN_END_MS),
            _green_bar(_ENTER_END_MS),
            _trade_bar(_CLOSE_MS, open_price="401.00", close_price=_LAST_BAR_CLOSE),
        ],
        mode="finite",
    )
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id=_ACCOUNT_ID)
    set_alpaca_clerk(facade)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger)
        await facade.drain_effects()

        if stale_snapshot:
            # The real capability refusal happens after acceptance. The Clerk
            # retains custody and the runner commits the accepted evaluation;
            # there is no unowned EXIT waiting for another strategy bar (#2482).
            assert len(broker.submitted_legs) == 1
            [pending] = repo.reconcilable_effect_operations()
            assert pending.kind == "EXIT"
            receipts = SqliteDecisionReceipts(repo, strategy_instance_id=_SID).tail(20)
            assert receipts[-1].outcome == "exit_intent"
            assert feed.bars_consumed == 3
            await facade.reconcile_once()
            assert feed.bars_consumed == 3

        enter_leg, exit_leg = broker.submitted_legs
        assert (enter_leg.side, enter_leg.order_type, enter_leg.extended_hours) == (
            OrderSide.BUY,
            OrderType.MARKET,
            False,
        )
        assert (
            exit_leg.side,
            exit_leg.quantity,
            exit_leg.order_type,
            exit_leg.time_in_force,
            exit_leg.extended_hours,
            exit_leg.limit_price,
        ) == (
            OrderSide.SELL,
            1.0,
            OrderType.LIMIT,
            TimeInForce.DAY,
            True,
            399.20,  # floor_tick(400.00 × (1 − 20 / 10⁴)): the last bar's close less the exit allowance
        )
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()

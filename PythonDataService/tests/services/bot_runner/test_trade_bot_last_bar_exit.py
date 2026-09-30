"""#2607 through the runner: a regular-hours run's last-bar EXIT is not sent after the close.

The bar that closes at the session close is decided only after the close, so
the runner refuses its decision before the Clerk: the program EXIT settles
DISCARD (it stays due for the next session's first decision) and a
``CLOSING_BAR`` receipt records the refusal. This supersedes, for program
decisions, #2440's after-hours limit for a last-bar EXIT; manual Flatten and
the watchdog's re-drive of a refused exit keep that path, and the Clerk's own
suites (``test_runtime_program_leg.py``, ``test_exit_send_session.py``) still
pin it on the facade.

This drives the real ``run_trade_bot`` over the sealed
``deployment_validation`` program on a ``use_rth=True`` binding: it enters,
then decides its EXIT on the bar that closes at the regular close.

The shared replay clock drives session rules at the fed bar, including the final close.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition, OrderSide, OrderType
from app.lean_sidecar.closing_bar import CLOSING_BAR_REASON_CODE
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeFeed, _SqliteRuntimeBroker
from tests._helpers.bot_runner.market import clock_read_before_the_close, patch_wall_clock_to_the_fed_bar
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _green_bar, _trade_bar

_ACCOUNT_ID = "PA-TEST"
# Two greens enter before the program's close-minus-15-minute barrier.
# Skipping to the final minute then makes the last bar decide EXIT.
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


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [date(2024, 1, 2), date(2024, 11, 29)])
@pytest.mark.parametrize("clock_read_before_close", [False, True])
async def test_a_regular_hours_exit_decided_on_the_last_bar_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: date, clock_read_before_close: bool,
) -> None:
    """The last bar's EXIT never reaches the Clerk, on a regular day or a half-day.

    Only the ENTER is on the wire. The runner refuses the closing-bar EXIT with
    a ``CLOSING_BAR`` receipt before any clock or Clerk is consulted, so it
    holds whichever clock answer the strategy gate would have read, and even
    though this account's policy could have priced an after-hours limit.
    """
    close_ms = session_close_ms_utc(day)
    if clock_read_before_close:
        liveness = clock_read_before_the_close(close_ms)
        monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
        monkeypatch.setattr(clerk_runtime, "market_liveness_fact", liveness)
    first_green_end_ms = close_ms - 17 * 60_000
    enter_end_ms = close_ms - 16 * 60_000
    # Seed before acquiring the lease: no future-dated lease can mask expiry.
    patch_wall_clock_to_the_fed_bar(monkeypatch, start_ms=first_green_end_ms)
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path / "clerk",
        clock=_clerk_clock,
        lease_ttl_ms=_REPLAY_LEASE_TTL_MS,
    )
    broker = _EntryFillingBroker()
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=broker, trade=broker, account_mode="paper", program_leg_policy=_POLICY
    )
    binding = _regular_hours_binding()
    await facade.register_strategy_run(binding)
    feed = _FakeFeed(
        [
            _green_bar(first_green_end_ms),
            _green_bar(enter_end_ms),
            _trade_bar(close_ms, open_price="401.00", close_price=_LAST_BAR_CLOSE),
        ],
        mode="finite",
    )
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id=_ACCOUNT_ID)
    set_alpaca_clerk(facade)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger)
        await facade.drain_effects()

        (enter_leg,) = broker.submitted_legs
        assert (enter_leg.side, enter_leg.order_type, enter_leg.extended_hours) == (
            OrderSide.BUY,
            OrderType.MARKET,
            False,
        )
        last = SqliteDecisionReceipts(repo, strategy_instance_id=_SID).tail(20)[-1]
        facts = json.loads(last.facts_json)
        assert (last.outcome, facts["reason_code"], facts["decision_bar_close_ms"]) == (
            "blocked",
            CLOSING_BAR_REASON_CODE,
            close_ms,
        )
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()

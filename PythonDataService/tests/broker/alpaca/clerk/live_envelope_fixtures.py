"""The live account every composed-shadow test rehearses against (ADR 0059 D4).

One envelope and one live-broker double, shared so a test that asserts an
authority *carries* the envelope, one that asserts a paper authority
*refuses* it, and one that drives an ENTER through the cash bound are all
describing the same account. Two doubles of one live account is exactly the
drift that hides an envelope bug -- the branch shipped two that already
disagreed about buying power, in a slice whose whole subject is cash.

Not a conftest: these are values and a class, imported by name from
``tests/broker/alpaca/clerk/``, ``tests/broker/v2panel/``,
``tests/routers/`` and ``tests/services/``, which share no conftest.
"""

from __future__ import annotations

from typing import Any

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import BrokerAccountSnapshot, BrokerAsset, BrokerPosition

TEST_ENVELOPE_VALUES = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)

LIVE_ACCT = "9LIVE0001"
SHADOW_ACCT = "shadow:9LIVE0001"


class _LiveBroker:
    """The live account: real reads the test steers, writes that must never be reached.

    ``cash``, ``unrealized`` and ``last_equity_known`` are plain attributes so
    a test can move the account between ticks, which is how the cash bound and
    the loss hold are driven. ``buying_power`` is ``cash`` deliberately: this
    slice bounds ENTERs against *cash*, and a double reporting margin buying
    power would let a wrong bound look right.

    ``now_ms`` is the caller's pinned instant -- every stamp this double
    reports comes from it, so nothing here reads a wall clock.
    """

    broker_id = "alpaca"

    def __init__(
        self,
        *,
        now_ms: int,
        cash: float = 100_000.0,
        unrealized: float = 0.0,
        last_equity_known: bool = True,
    ) -> None:
        self.now_ms = now_ms
        self.cash = cash
        self.unrealized = unrealized
        # False models the broker answering with no previous-close equity:
        # there is no loss limit to judge against, so the account is
        # unjudgeable and every ENTER is refused (plan R3).
        self.last_equity_known = last_equity_known

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=LIVE_ACCT,
            account_mode="live",
            account_status="ACTIVE",
            currency="USD",
            cash=self.cash,
            equity=self.cash,
            buying_power=self.cash,
            portfolio_value=self.cash,
            long_market_value=0.0,
            short_market_value=0.0,
            last_equity=self.cash if self.last_equity_known else None,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=self.now_ms - 1_000,
            observed_at_ms=self.now_ms,
        )

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        if self.unrealized == 0.0:
            return []
        return [
            BrokerPosition(
                broker="alpaca",
                symbol="SPY",
                asset_id=None,
                asset_class=None,
                quantity=10,
                side="long",
                average_entry_price=100.0,
                market_value=1_000.0 + self.unrealized,
                cost_basis=1_000.0,
                current_price=None,
                unrealized_pl=self.unrealized,
                unrealized_plpc=None,
                observed_at_ms=self.now_ms,
            )
        ]

    async def list_activities(self, **_kwargs: Any) -> list:
        return []

    async def get_asset(self, symbol: str) -> BrokerAsset:
        return BrokerAsset(
            broker="alpaca",
            symbol=symbol,
            asset_class="us_equity",
            tradable=True,
            fractionable=False,
            shortable=False,
            easy_to_borrow=False,
            observed_at_ms=self.now_ms,
        )

    async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("LIVE TRADE PORT WAS REACHED")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("LIVE CANCEL WAS REACHED")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


__all__ = ["LIVE_ACCT", "SHADOW_ACCT", "TEST_ENVELOPE_VALUES", "_LiveBroker"]

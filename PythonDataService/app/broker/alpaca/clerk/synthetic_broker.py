"""Non-network account and capability facts for a synthetic Clerk authority.

This is deliberately not a fill simulator.  It supplies the read facts needed
to compose and recover an isolated Clerk; the later Dry Run slice owns its
retained-bar price policy and simulated execution behavior.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.utils.timestamps import now_ms_utc

SYNTHETIC_BROKER_ID = "synthetic"
SYNTHETIC_CAPABILITIES = BrokerCapabilities(
    broker=SYNTHETIC_BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    supported_order_types=("market", "limit"),
    data_feed="retained_source_bars",
    bars_may_gap=False,
    max_stream_symbols=0,
    max_concurrent_streams=0,
    rest_rate_limit_per_min=0,
)


class SyntheticBroker:
    """Read-only synthetic broker identity; it never contacts Alpaca."""

    broker_id = SYNTHETIC_BROKER_ID

    def __init__(self, *, account_id: str) -> None:
        self._account_id = require_synthetic_account_id(account_id)

    def capabilities(self) -> BrokerCapabilities:
        return SYNTHETIC_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        observed_at_ms = now_ms_utc()
        return BrokerAccountSnapshot(
            broker=self.broker_id,
            account_id=self._account_id,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=0.0,
            equity=0.0,
            buying_power=0.0,
            portfolio_value=0.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=observed_at_ms,
            observed_at_ms=observed_at_ms,
        )

    async def list_positions(self) -> list[BrokerPosition]:
        return []

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del status, limit, after_ms
        return []

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        del after_ms, limit
        return []

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        del status, limit
        return []

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        del symbol
        return None

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        observed_at_ms = now_ms_utc()
        return BrokerClockEvidence(
            broker=self.broker_id,
            is_open=False,
            vendor_timestamp_ms=observed_at_ms,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=observed_at_ms,
        )

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        del history_range
        return BrokerPortfolioHistory(
            timestamps=[],
            equity=[],
            profit_loss=[],
            base_value=0.0,
            timeframe="synthetic",
        )

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        del leg, client_order_id
        raise BrokerUnavailable(
            "Synthetic execution is not installed; the Dry Run execution slice owns it.",
            broker=self.broker_id,
        )

    async def cancel(self, order_id: str) -> None:
        del order_id
        raise BrokerUnavailable(
            "Synthetic execution is not installed; the Dry Run execution slice owns it.",
            broker=self.broker_id,
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        del client_order_id
        return None


__all__ = ["SYNTHETIC_BROKER_ID", "SYNTHETIC_CAPABILITIES", "SyntheticBroker"]

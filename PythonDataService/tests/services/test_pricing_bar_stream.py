"""The traded instrument's bar stream keeps itself going and tells its failures apart (#2912)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.feed import ContinuityPolicy, MarketDataBar, MarketDataFeedError
from app.services.pricing_bar_stream import PricingBarStream, PricingRefusal
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

_MINUTE_MS = 60_000
_T0 = 1_788_000_000_000


def _qqq(end_ms: int, *, close: str = "40.10") -> MarketDataBar:
    price = Decimal(close)
    return MarketDataBar(
        symbol="QQQ", start_ms=end_ms - _MINUTE_MS, end_ms=end_ms, open=price, high=price, low=price,
        close=price, volume=500, fetched_at_ms=end_ms, feed_id="fake", session_phase="RTH",
    )


class _Subscriptions:
    """A feed whose every subscription is its own script: bars to deliver, then an error or silence."""

    feed_id = "fake"

    def __init__(self, *subscriptions: list[MarketDataBar | Exception]) -> None:
        self._subscriptions = list(subscriptions)
        self.opened = 0

    async def stream_bars(
        self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None
    ) -> AsyncIterator[MarketDataBar]:
        del symbol, use_rth, continuity
        script = self._subscriptions[self.opened] if self.opened < len(self._subscriptions) else []
        self.opened += 1
        for step in script:
            if isinstance(step, Exception):
                raise step
            yield step
        await asyncio.Event().wait()


@pytest.fixture
def ledger(tmp_path: Path) -> SourceBarLedger:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="PA-TEST")
    yield bars
    bars.close()


async def test_a_stream_that_failed_subscribes_again_and_prices_the_next_minute(ledger: SourceBarLedger) -> None:
    feed = _Subscriptions(
        [MarketDataFeedError("minute incomplete", reason="MINUTE_INCOMPLETE")],
        [_qqq(_T0 + _MINUTE_MS)],
    )
    async with PricingBarStream(
        feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000, resubscribe_delay_s=0.05
    ) as stream:
        # 20 ms of validity left: the answer comes while the stream is still down.
        while_down = await stream.bar_for_entry(_T0, time_left_ms=20)
        once_back = await stream.bar_for_entry(_T0 + _MINUTE_MS, time_left_ms=20_000)

    assert isinstance(while_down, PricingRefusal) and while_down.reason_code == "PRICING_FEED_UNHEALTHY"
    assert "MINUTE_INCOMPLETE" in while_down.why
    assert isinstance(once_back, RetainedSourceBar) and once_back.end_ms == _T0 + _MINUTE_MS
    assert feed.opened == 2


async def test_the_wait_never_outlasts_the_decisions_own_time_limit(ledger: SourceBarLedger) -> None:
    """A long wait is cut to the time the decision itself stays valid: here, none."""
    async with PricingBarStream(_Subscriptions([]), ledger, symbol="QQQ", run_id="run-1", wait_ms=60_000) as stream:
        refused = await asyncio.wait_for(stream.bar_for_entry(_T0, time_left_ms=0), timeout=1)

    assert isinstance(refused, PricingRefusal) and refused.reason_code == "PRICING_BAR_LATE"


async def test_a_minute_delivered_out_of_order_is_a_missing_price_not_two_prices(ledger: SourceBarLedger) -> None:
    """The ledger keeps neither a second price for a minute nor a minute older than its newest.

    Only the first is two prices for one minute. An older minute that arrives
    late was simply never retained, and says so.
    """
    feed = _Subscriptions([_qqq(_T0 + _MINUTE_MS), _qqq(_T0)])
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=20) as stream:
        newest = await stream.bar_for_entry(_T0 + _MINUTE_MS, time_left_ms=20_000)
        older = await stream.bar_for_entry(_T0, time_left_ms=20_000)

    assert isinstance(newest, RetainedSourceBar)
    assert isinstance(older, PricingRefusal) and older.reason_code == "PRICING_BAR_LATE"

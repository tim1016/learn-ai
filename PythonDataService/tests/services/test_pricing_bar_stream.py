"""The traded instrument's bar stream keeps itself going and tells its failures apart (#2912)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar, MarketDataFeedError
from app.services.pricing_bar_stream import PricingBarStream, PricingRefusal
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger, SourceBarRetentionLimitError

_MINUTE_MS = 60_000
_T0 = 1_788_000_000_000


def _qqq(end_ms: int, *, close: str = "40.10", volume: int = 500, minutes: int = 1) -> MarketDataBar:
    price = Decimal(close)
    return MarketDataBar(
        symbol="QQQ", start_ms=end_ms - minutes * _MINUTE_MS, end_ms=end_ms, open=price, high=price, low=price,
        close=price, volume=volume, fetched_at_ms=end_ms, feed_id="fake", session_phase="RTH",
    )


class _Subscriptions:
    """A feed whose every subscription is its own script: bars to deliver, then an error or silence."""

    feed_id = "fake"

    def __init__(self, *subscriptions: list[MarketDataBar | Exception]) -> None:
        self._subscriptions = list(subscriptions)
        self.opened = 0
        self.connected = True
        self.stale = False
        # Set by a test to keep every subscription after the first silent until released.
        self.hold_before_delivering: asyncio.Event | None = None

    async def stream_bars(
        self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None
    ) -> AsyncIterator[MarketDataBar]:
        del symbol, use_rth, continuity
        script = self._subscriptions[self.opened] if self.opened < len(self._subscriptions) else []
        self.opened += 1
        if self.opened > 1 and self.hold_before_delivering is not None:
            await self.hold_before_delivering.wait()
        for step in script:
            if isinstance(step, Exception):
                raise step
            yield step
        await asyncio.Event().wait()

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=self.connected, stale=self.stale, last_bar_ms=None, reason="",
            active_subscription_count=0, observed_at_ms=_T0,
        )


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


async def test_a_failure_the_feed_did_not_name_is_ridden_out_like_any_other(ledger: SourceBarLedger) -> None:
    """A socket that drops mid-subscribe raises the transport's error, not the feed's.

    The Signal stream rides such a drop out. A pricing stream that died of it
    would leave every later entry unpriceable until the bot was restarted.
    """
    feed = _Subscriptions([ConnectionError("Socket disconnect")], [_qqq(_T0)])
    async with PricingBarStream(
        feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000, resubscribe_delay_s=0.02
    ) as stream:
        priced = await stream.bar_for_entry(_T0, time_left_ms=20_000)

    assert isinstance(priced, RetainedSourceBar) and priced.end_ms == _T0
    assert feed.opened == 2


async def test_a_stream_that_is_down_waits_for_the_connection_before_it_asks_again(ledger: SourceBarLedger) -> None:
    """Asking a Gateway that is not connected only spends requests and log lines."""
    feed = _Subscriptions([MarketDataFeedError("socket down", reason="socket_down")], [_qqq(_T0)])
    feed.connected = False
    async with PricingBarStream(
        feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000, resubscribe_delay_s=0.01
    ) as stream:
        await asyncio.sleep(0.1)
        asked_while_disconnected = feed.opened
        feed.connected = True
        priced = await stream.bar_for_entry(_T0, time_left_ms=20_000)

    assert asked_while_disconnected == 1
    assert isinstance(priced, RetainedSourceBar)
    assert feed.opened == 2


async def test_a_stream_that_keeps_failing_asks_less_and_less_often_until_a_bar_arrives(
    ledger: SourceBarLedger,
) -> None:
    """Each attempt can spend one of the connection's paced requests, which every bot shares.

    The pause doubles to its ceiling while the stream keeps failing, and is
    back to the first pause once a bar has been retained.
    """
    failure = MarketDataFeedError("no line", reason="stall")
    feed = _Subscriptions([failure], [failure], [failure], [failure], [_qqq(_T0), failure], [])
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)
        await asyncio.sleep(0)

    async with PricingBarStream(
        feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=0,
        resubscribe_delay_s=2.0, resubscribe_max_delay_s=5.0, sleep=pause,
    ):
        while feed.opened < 6:
            await asyncio.sleep(0)

    assert pauses == [2.0, 4.0, 5.0, 5.0, 2.0]


async def test_a_stream_whose_bars_can_no_longer_be_kept_stops_and_says_so(
    ledger: SourceBarLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking the feed again cannot cure a full ledger, and each attempt would spend a request."""

    def full(*_args: object, **_kwargs: object) -> RetainedSourceBar:
        raise SourceBarRetentionLimitError("SOURCE_BAR_RETENTION_LIMIT: fake:QQQ reached its retained bars")

    monkeypatch.setattr(ledger, "append", full)
    feed = _Subscriptions([_qqq(_T0)], [_qqq(_T0)])
    async with PricingBarStream(
        feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=50, resubscribe_delay_s=0.001
    ) as stream:
        refused = await stream.bar_for_entry(_T0, time_left_ms=20_000)

    assert isinstance(refused, PricingRefusal) and refused.reason_code == "PRICING_FEED_UNHEALTHY"
    assert "can no longer be retained" in refused.why
    assert feed.opened == 1


async def test_a_stream_that_failed_says_what_failed_until_a_bar_arrives_again(ledger: SourceBarLedger) -> None:
    """Asked for again is not delivering yet. Once a bar has come, a missing one is merely late."""
    feed = _Subscriptions([MarketDataFeedError("minute incomplete", reason="MINUTE_INCOMPLETE")], [_qqq(_T0)])
    feed.hold_before_delivering = asyncio.Event()
    async with PricingBarStream(
        feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=20, resubscribe_delay_s=0.001
    ) as stream:
        while feed.opened < 2:
            await asyncio.sleep(0)
        asked_again = await stream.bar_for_entry(_T0, time_left_ms=20_000)

        feed.hold_before_delivering.set()
        delivering = await stream.bar_for_entry(_T0, time_left_ms=20_000)
        merely_late = await stream.bar_for_entry(_T0 + _MINUTE_MS, time_left_ms=20_000)

    assert isinstance(asked_again, PricingRefusal)
    assert asked_again.reason_code == "PRICING_FEED_UNHEALTHY" and "MINUTE_INCOMPLETE" in asked_again.why
    assert isinstance(delivering, RetainedSourceBar)
    assert isinstance(merely_late, PricingRefusal) and merely_late.reason_code == "PRICING_BAR_LATE"


@pytest.mark.parametrize(
    ("health_flag", "health_value"),
    [
        pytest.param("connected", False, id="a_feed_that_says_it_is_disconnected"),
        pytest.param("stale", True, id="a_line_that_stalled_without_failing"),
    ],
)
async def test_an_unhealthy_feed_is_a_broken_feed_not_a_late_bar(
    ledger: SourceBarLedger, health_flag: str, health_value: bool
) -> None:
    feed = _Subscriptions([])
    setattr(feed, health_flag, health_value)
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=20) as stream:
        refused = await stream.bar_for_entry(_T0, time_left_ms=20_000)

    assert isinstance(refused, PricingRefusal) and refused.reason_code == "PRICING_FEED_UNHEALTHY"


async def test_a_plan_spelt_in_lower_case_still_finds_its_bars(ledger: SourceBarLedger) -> None:
    """The feed stamps bars with the upper-cased symbol; a plan's own spelling is not normalized."""
    async with PricingBarStream(_Subscriptions([_qqq(_T0)]), ledger, symbol="qqq", run_id="run-1", wait_ms=2_000) as stream:
        assert isinstance(await stream.bar_for_entry(_T0, time_left_ms=20_000), RetainedSourceBar)


async def test_a_minute_with_no_trade_is_not_a_price_for_a_sell_either(ledger: SourceBarLedger) -> None:
    """The sell is handed no bar for that minute, as if none had arrived."""
    feed = _Subscriptions([_qqq(_T0, volume=0), _qqq(_T0 + _MINUTE_MS)])
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000) as stream:
        await stream.bar_for_entry(_T0 + _MINUTE_MS, time_left_ms=20_000)

        assert stream.bar_closing_at(_T0) is None
        traded = stream.bar_closing_at(_T0 + _MINUTE_MS)
        assert traded is not None and traded.volume == 500


async def test_two_bars_closing_together_are_two_prices_whatever_minute_they_opened_in(
    ledger: SourceBarLedger,
) -> None:
    feed = _Subscriptions([_qqq(_T0, close="40.10"), _qqq(_T0, close="41.00", minutes=2), _qqq(_T0 + _MINUTE_MS)])
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000) as stream:
        await stream.bar_for_entry(_T0 + _MINUTE_MS, time_left_ms=20_000)
        refused = await stream.bar_for_entry(_T0, time_left_ms=20_000)

    assert isinstance(refused, PricingRefusal) and refused.reason_code == "PRICING_BAR_CONFLICT"


async def test_a_simulated_sell_fills_at_its_own_minutes_price_when_that_arrives_in_the_wait(
    ledger: SourceBarLedger,
) -> None:
    feed = _Subscriptions([_qqq(_T0), _qqq(_T0 + _MINUTE_MS, close="40.60")])
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000) as stream:
        priced = await stream.bar_for_simulated_exit(_T0 + _MINUTE_MS)

    assert priced is not None and (priced.end_ms, priced.close) == (_T0 + _MINUTE_MS, Decimal("40.60"))


@pytest.mark.parametrize(
    "own_minute",
    [
        pytest.param([], id="no-bar"),
        pytest.param([_qqq(_T0 + 2 * _MINUTE_MS, close="41.00", volume=0)], id="a-bar-with-no-trade"),
    ],
)
async def test_a_simulated_sell_with_no_price_of_its_minute_takes_the_newest_earlier_trade(
    ledger: SourceBarLedger, own_minute: list[MarketDataBar]
) -> None:
    """A minute nothing traded in is not a price, for the sell's own minute or for an earlier one."""
    feed = _Subscriptions(
        [_qqq(_T0, close="40.10"), _qqq(_T0 + _MINUTE_MS, close="40.90", volume=0), *own_minute]
    )
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=30) as stream:
        priced = await stream.bar_for_simulated_exit(_T0 + 2 * _MINUTE_MS)

    assert priced is not None and (priced.end_ms, priced.close) == (_T0, Decimal("40.10"))


async def test_a_simulated_sell_passes_over_an_earlier_minute_delivered_with_two_prices(
    ledger: SourceBarLedger,
) -> None:
    """Neither price of that minute may price an order, so the sale takes the trade before it."""
    feed = _Subscriptions([
        _qqq(_T0, close="40.10"),
        _qqq(_T0 + _MINUTE_MS, close="40.50"),
        _qqq(_T0 + _MINUTE_MS, close="41.00", minutes=2),
        _qqq(_T0 + 2 * _MINUTE_MS, close="40.70", volume=0),
    ])
    async with PricingBarStream(feed, ledger, symbol="QQQ", run_id="run-1", wait_ms=2_000) as stream:
        refused = await stream.bar_for_entry(_T0 + _MINUTE_MS, time_left_ms=20_000)
        await stream.bar_for_entry(_T0 + 2 * _MINUTE_MS, time_left_ms=20_000)
        priced = await stream.bar_for_simulated_exit(_T0 + 3 * _MINUTE_MS)

    assert isinstance(refused, PricingRefusal) and refused.reason_code == "PRICING_BAR_CONFLICT"
    assert priced is not None and (priced.end_ms, priced.close) == (_T0, Decimal("40.10"))


async def test_a_simulated_sell_has_no_price_only_when_the_asset_never_traded(ledger: SourceBarLedger) -> None:
    async with PricingBarStream(_Subscriptions([]), ledger, symbol="QQQ", run_id="run-1", wait_ms=0) as stream:
        assert await stream.bar_for_simulated_exit(_T0) is None

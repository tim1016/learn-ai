"""Tests for app.marketdata — MarketDataFeed port, IbkrMarketDataFeed, diagnostics.

Covers all acceptance criteria from issue #1259:
- AC1: No IBKR types at the port boundary.
- AC2: Two concurrent consumers of the same symbol receive identical bars
       from one underlying subscription.
- AC3: Reference-counted subscribe/unsubscribe.
- AC4: Feed-death raises MarketDataFeedError to all consumers; FeedHealth
       reports unhealthy with a reason; bar gaps are non-fatal.
- AC5: All temporal fields int64 ms UTC.
- AC6: Diagnostic endpoint reports connected/stale/last_bar_ms and active
       subscription count.
- AC7: ruff + focused pytest green (verified externally).

The tests use a fake bar source so no real IBKR connection is needed.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.marketdata.feed import (
    FeedHealth,
    MarketDataBar,
    MarketDataFeedError,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed, set_market_data_feed
from tests._helpers.ibkr_feed_adversarial import NeverFirstBarFeedFixture

# ---------------------------------------------------------------------------
# Helpers and fakes
# ---------------------------------------------------------------------------


def _make_ibkr_bar(
    symbol: str = "SPY",
    start_ms: int = 1_700_000_000_000,
    *,
    open_: str = "400.00",
    high: str = "401.00",
    low: str = "399.00",
    close: str = "400.50",
    volume: int = 1000,
) -> SimpleNamespace:
    """Build a fake IbkrMinuteBar-shaped object (attribute duck-typing)."""
    return SimpleNamespace(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        fetched_at_ms=start_ms + 1_000,
        source="ibkr",
        provenance="ibkr_realtime",
        venue="SMART",
        session_phase="RTH",
        use_rth=True,
    )


def _fake_connected_client(*, connected: bool = True, connection_lost: bool = False) -> MagicMock:
    client = MagicMock()
    client.is_connected.return_value = connected
    client.connection_lost = connection_lost
    return client


def test_ibkr_feed_exposes_its_capability_account_identity() -> None:
    client = _fake_connected_client()
    client.connected_account = "DU1234567"

    assert IbkrMarketDataFeed(client).capability_account_id == "DU1234567"


class _FakeBarSource:
    """Injects a fixed sequence of IbkrMinuteBar-shaped objects into the feed.

    Replaces ``app.broker.ibkr.bars.stream_minute_bars`` in tests so no
    IBKR gateway is needed.

    ``raise_after``: if given, raise ``IBKRBarStreamError`` after this many
    bars have been yielded.  E.g. ``raise_after=1`` yields 1 bar then raises.
    """

    def __init__(
        self,
        bars: list[Any],
        *,
        raise_after: int | None = None,
        error_msg: str = "Simulated IBKR connection loss",
    ) -> None:
        self._bars = bars
        self._raise_after = raise_after
        self._error_msg = error_msg
        self.call_count: int = 0

    async def __call__(
        self,
        client: Any,
        symbol: str,
        *,
        use_rth: bool = True,
        on_source_bar=None,
    ):  # type: ignore[override]
        from app.broker.ibkr.bars import IBKRBarStreamError

        self.call_count += 1
        for idx, bar in enumerate(self._bars):
            if on_source_bar is not None:
                on_source_bar(bar.start_ms)
            yield bar
            if self._raise_after is not None and idx + 1 >= self._raise_after:
                raise IBKRBarStreamError(self._error_msg)


# ---------------------------------------------------------------------------
# AC1 — Port exposes no IBKR types
# ---------------------------------------------------------------------------


def test_market_data_bar_carries_no_ibkr_types() -> None:
    """MarketDataBar imports only from app.marketdata.feed — no IBKR."""
    import app.marketdata.feed as feed_mod

    assert "ibkr" not in feed_mod.__name__
    # The module must not import anything from app.broker.ibkr
    for attr in dir(feed_mod):
        obj = getattr(feed_mod, attr)
        mod = getattr(obj, "__module__", "") or ""
        assert "broker.ibkr" not in mod, f"feed.py exports an IBKR type: {attr} from {mod}"


def test_market_data_feed_error_is_not_ibkr_error() -> None:
    """MarketDataFeedError must not be a subclass of IBKRBarStreamError."""
    from app.broker.ibkr.bars import IBKRBarStreamError

    assert not issubclass(MarketDataFeedError, IBKRBarStreamError)


def test_ibkr_bar_stream_error_is_not_market_data_feed_error() -> None:
    """IBKRBarStreamError must not escape through the port."""
    from app.broker.ibkr.bars import IBKRBarStreamError

    assert not issubclass(IBKRBarStreamError, MarketDataFeedError)


# ---------------------------------------------------------------------------
# AC5 — All temporal fields int64 ms UTC
# ---------------------------------------------------------------------------


def test_market_data_bar_timestamps_are_ints() -> None:
    bar = MarketDataBar(
        symbol="SPY",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_060_000,
        open=Decimal("400.00"),
        high=Decimal("401.00"),
        low=Decimal("399.00"),
        close=Decimal("400.50"),
        volume=1000,
        fetched_at_ms=1_700_000_001_000,
        feed_id="ibkr",
        session_phase="RTH",
    )
    assert isinstance(bar.start_ms, int)
    assert isinstance(bar.end_ms, int)
    assert isinstance(bar.fetched_at_ms, int)
    # end_ms is exactly 60 seconds after start_ms
    assert bar.end_ms - bar.start_ms == 60_000


def test_feed_health_timestamps_are_ints() -> None:
    h = FeedHealth(
        connected=True,
        stale=False,
        last_bar_ms=1_700_000_000_000,
        reason="",
        active_subscription_count=0,
        observed_at_ms=1_700_000_001_000,
    )
    assert isinstance(h.last_bar_ms, int)
    assert isinstance(h.observed_at_ms, int)


# ---------------------------------------------------------------------------
# AC2, AC3 — Fan-out: two consumers share one subscription; reference-counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_consumers_receive_identical_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent consumers of the same symbol receive the same closed bars."""
    bars_data = [
        _make_ibkr_bar("SPY", start_ms=1_700_000_000_000),
        _make_ibkr_bar("SPY", start_ms=1_700_000_060_000),
    ]
    fake_source = _FakeBarSource(bars_data)
    monkeypatch.setattr("app.marketdata.ibkr_feed.stream_minute_bars", fake_source)

    client = _fake_connected_client()
    feed = IbkrMarketDataFeed(client)

    consumer_a: list[MarketDataBar] = []
    consumer_b: list[MarketDataBar] = []

    async def _consume(out: list[MarketDataBar]) -> None:
        async for bar in feed.stream_bars("SPY"):
            out.append(bar)

    await asyncio.gather(_consume(consumer_a), _consume(consumer_b))

    assert len(consumer_a) == 2
    assert len(consumer_b) == 2
    # Both consumers received identical bars
    for a, b in zip(consumer_a, consumer_b, strict=True):
        assert a.start_ms == b.start_ms
        assert a.close == b.close
        assert a.volume == b.volume


@pytest.mark.asyncio
async def test_feed_active_count_decrements_after_consumer_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active subscription count tracks enter/exit correctly."""
    bars_data = [_make_ibkr_bar("SPY", start_ms=1_700_000_000_000)]
    fake_source = _FakeBarSource(bars_data)
    monkeypatch.setattr("app.marketdata.ibkr_feed.stream_minute_bars", fake_source)

    client = _fake_connected_client()
    feed = IbkrMarketDataFeed(client)

    assert feed.health("SPY").active_subscription_count == 0

    bars: list[MarketDataBar] = []
    async for bar in feed.stream_bars("SPY"):
        assert feed.health("SPY").active_subscription_count == 1
        bars.append(bar)

    assert feed.health("SPY").active_subscription_count == 0
    assert len(bars) == 1


# ---------------------------------------------------------------------------
# AC4 — Feed death raises MarketDataFeedError; bar gaps are non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ibkr_stream_error_translates_to_market_data_feed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IBKRBarStreamError raised by bars.py is caught and re-raised as MarketDataFeedError."""
    bars_data = [
        _make_ibkr_bar("SPY", start_ms=1_700_000_000_000),
    ]
    fake_source = _FakeBarSource(bars_data, raise_after=1, error_msg="IBKR connection lost")
    monkeypatch.setattr("app.marketdata.ibkr_feed.stream_minute_bars", fake_source)

    client = _fake_connected_client()
    feed = IbkrMarketDataFeed(client)

    with pytest.raises(MarketDataFeedError, match="IBKR connection lost"):
        async for _ in feed.stream_bars("SPY"):
            pass


@pytest.mark.asyncio
async def test_feed_death_raises_error_to_all_consumers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both concurrent consumers receive MarketDataFeedError on connection death."""
    bars_data = [_make_ibkr_bar("SPY", start_ms=1_700_000_000_000)]
    fake_source = _FakeBarSource(bars_data, raise_after=1)
    monkeypatch.setattr("app.marketdata.ibkr_feed.stream_minute_bars", fake_source)

    client = _fake_connected_client()
    feed = IbkrMarketDataFeed(client)

    errors: list[Exception] = []

    async def _consume_expecting_error() -> None:
        try:
            async for _ in feed.stream_bars("SPY"):
                pass
        except MarketDataFeedError as exc:
            errors.append(exc)

    await asyncio.gather(
        _consume_expecting_error(),
        _consume_expecting_error(),
    )

    assert len(errors) == 2
    for err in errors:
        assert isinstance(err, MarketDataFeedError)


@pytest.mark.asyncio
async def test_bar_gap_is_nonfatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bars arriving with a time gap (missing minutes) do not raise an error.

    The feed simply skips the gap and yields the next bar when it arrives.
    This verifies that gaps are non-fatal — the IBKR subscription just has
    no bar to emit during market silence.
    """
    # Two bars 5 minutes apart — a real gap
    bars_data = [
        _make_ibkr_bar("SPY", start_ms=1_700_000_000_000),
        _make_ibkr_bar("SPY", start_ms=1_700_000_300_000),  # +5 minutes
    ]
    fake_source = _FakeBarSource(bars_data)
    monkeypatch.setattr("app.marketdata.ibkr_feed.stream_minute_bars", fake_source)

    client = _fake_connected_client()
    feed = IbkrMarketDataFeed(client)

    collected: list[MarketDataBar] = []
    async for bar in feed.stream_bars("SPY"):
        collected.append(bar)

    assert len(collected) == 2
    assert collected[1].start_ms - collected[0].start_ms == 300_000  # 5-min gap preserved


@pytest.mark.asyncio
async def test_stalled_subscription_is_replaced_without_ending_the_bot_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411: a dead IBKR request is recoverable, not an unbounded wait."""
    from app.broker.ibkr.bars import IBKRBarSubscriptionStalled

    bar = _make_ibkr_bar()
    call_count = 0

    async def recovering_source(
        _client,
        _symbol,
        *,
        use_rth=True,
        on_source_bar=None,
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise IBKRBarSubscriptionStalled("simulated stalled subscription")
        if on_source_bar is not None:
            on_source_bar(bar.start_ms)
        yield bar

    monkeypatch.setattr(
        "app.marketdata.ibkr_feed.stream_minute_bars",
        recovering_source,
    )
    feed = IbkrMarketDataFeed(_fake_connected_client())

    observed = await anext(feed.stream_bars("SPY"))

    assert observed.start_ms == bar.start_ms
    assert call_count == 2
    assert feed.health().stale is False


@pytest.mark.asyncio
async def test_health_is_scoped_per_symbol_when_one_sibling_never_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411: an advancing symbol cannot mask a dead sibling subscription."""
    spy_bar = _make_ibkr_bar()
    aapl_attached = asyncio.Event()

    async def mixed_source(
        _client,
        symbol,
        *,
        use_rth=True,
        on_source_bar=None,
    ):
        del use_rth
        if symbol == "SPY":
            if on_source_bar is not None:
                on_source_bar(spy_bar.start_ms)
            yield spy_bar
            await asyncio.Event().wait()
        aapl_attached.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keeps this branch an async generator
            yield spy_bar

    monkeypatch.setattr(
        "app.marketdata.ibkr_feed.stream_minute_bars",
        mixed_source,
    )
    feed = IbkrMarketDataFeed(_fake_connected_client())
    spy_stream = feed.stream_bars("SPY")
    aapl_stream = feed.stream_bars("AAPL")
    aapl_next = asyncio.create_task(anext(aapl_stream))

    try:
        await anext(spy_stream)
        await aapl_attached.wait()

        assert feed.health("SPY").stale is False
        assert feed.health("AAPL").stale is True
        assert "AAPL" in feed.health().reason
    finally:
        aapl_next.cancel()
        await asyncio.gather(aapl_next, return_exceptions=True)
        await spy_stream.aclose()
        await aapl_stream.aclose()


# ---------------------------------------------------------------------------
# AC4 (health) — health() reports unhealthy when connection is lost
# ---------------------------------------------------------------------------


def test_health_connected_no_bars() -> None:
    client = _fake_connected_client(connected=True, connection_lost=False)
    feed = IbkrMarketDataFeed(client)
    h = feed.health()

    assert h.connected is True
    assert h.stale is False
    assert h.last_bar_ms is None
    assert h.reason == ""
    assert isinstance(h.observed_at_ms, int)


@pytest.mark.asyncio
async def test_health_active_without_first_closed_bar_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411: an active never-advanced stream fails closed immediately."""
    fixture = NeverFirstBarFeedFixture()
    fixture.install(monkeypatch)
    feed = IbkrMarketDataFeed(fixture.client)
    stream = feed.stream_bars("SPY")
    pending_bar = asyncio.create_task(anext(stream))
    await fixture.wait_until_subscribed()

    health = feed.health("SPY")

    assert health.stale is True
    assert health.last_bar_ms is None
    assert "first closed bar" in health.reason.lower()
    pending_bar.cancel()
    await asyncio.gather(pending_bar, return_exceptions=True)
    await stream.aclose()


def test_health_disconnected_reports_reason() -> None:
    client = _fake_connected_client(connected=False)
    feed = IbkrMarketDataFeed(client)
    h = feed.health()

    assert h.connected is False
    assert "connection lost" in h.reason.lower()


def test_health_connection_lost_flag_reports_unhealthy() -> None:
    client = _fake_connected_client(connected=True, connection_lost=True)
    feed = IbkrMarketDataFeed(client)
    h = feed.health()

    assert h.connected is False


def test_health_stale_after_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed reports stale=True when last_bar_wall_ms is older than the threshold."""
    fixed_now = 1_700_001_000_000
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: fixed_now)

    client = _fake_connected_client(connected=True, connection_lost=False)
    feed = IbkrMarketDataFeed(client, stale_threshold_ms=60_000)
    # Fake a last-bar wall-clock 5 minutes ago
    state = feed._state_for("SPY")
    state.last_bar_wall_ms = fixed_now - 300_000
    state.last_bar_ms = 1_700_000_700_000
    state.active_count = 1
    state.first_bar_seen = True

    h = feed.health()

    assert h.connected is True
    assert h.stale is True
    assert "300s" in h.reason or "300" in h.reason


def test_health_not_stale_within_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed is not stale when last bar arrived within the threshold."""
    fixed_now = 1_700_001_000_000
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: fixed_now)

    client = _fake_connected_client(connected=True, connection_lost=False)
    feed = IbkrMarketDataFeed(client, stale_threshold_ms=180_000)
    state = feed._state_for("SPY")
    state.last_bar_wall_ms = fixed_now - 60_000  # 1 minute ago — within 3-min threshold
    state.last_bar_ms = 1_700_000_940_000
    state.active_count = 1
    state.first_bar_seen = True

    h = feed.health()

    assert h.stale is False


def test_health_ignores_detached_symbol_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = 1_700_001_000_000
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: fixed_now)
    client = _fake_connected_client(connected=True, connection_lost=False)
    feed = IbkrMarketDataFeed(client, stale_threshold_ms=30_000)
    detached = feed._state_for("AAPL")
    detached.last_bar_wall_ms = fixed_now - 300_000
    detached.last_bar_ms = fixed_now - 300_000

    health = feed.health()

    assert health.stale is False
    assert health.active_subscription_count == 0


# ---------------------------------------------------------------------------
# AC6 — Diagnostic endpoint returns FeedHealth with active_subscription_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_returns_feed_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/market-data-feed/health returns FeedHealth when feed is installed."""
    from fastapi import FastAPI

    from app.routers.market_data_feed import router

    client_ibkr = _fake_connected_client(connected=True, connection_lost=False)
    feed = IbkrMarketDataFeed(client_ibkr)
    feed._state_for("SPY").last_bar_ms = 1_700_000_000_000

    set_market_data_feed(feed)
    try:
        test_app = FastAPI()
        test_app.include_router(router, prefix="/api/market-data-feed")

        async with httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as tc:
            resp = await tc.get("/api/market-data-feed/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is True
        assert body["stale"] is False
        assert body["last_bar_ms"] == 1_700_000_000_000
        assert "active_subscription_count" in body
        assert isinstance(body["observed_at_ms"], int)
    finally:
        set_market_data_feed(None)


@pytest.mark.asyncio
async def test_health_endpoint_503_when_feed_not_installed() -> None:
    """GET /api/market-data-feed/health returns 503 when feed is not installed."""
    from fastapi import FastAPI

    from app.routers.market_data_feed import router

    set_market_data_feed(None)
    test_app = FastAPI()
    test_app.include_router(router, prefix="/api/market-data-feed")

    async with httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as tc:
        resp = await tc.get("/api/market-data-feed/health")

    assert resp.status_code == 503


def test_health_active_subscription_count_in_response() -> None:
    """health() includes the active_subscription_count field."""
    client = _fake_connected_client(connected=True)
    feed = IbkrMarketDataFeed(client)
    feed._state_for("SPY").active_count = 3

    h = feed.health()

    assert h.active_subscription_count == 3


# ---------------------------------------------------------------------------
# Protocol conformance — IbkrMarketDataFeed satisfies MarketDataFeed structurally
# ---------------------------------------------------------------------------


def test_ibkr_feed_satisfies_market_data_feed_protocol() -> None:
    """IbkrMarketDataFeed must satisfy the MarketDataFeed Protocol structurally."""
    client = _fake_connected_client()
    feed = IbkrMarketDataFeed(client)

    # Protocol has feed_id, stream_bars, health
    assert hasattr(feed, "feed_id")
    assert hasattr(feed, "stream_bars")
    assert hasattr(feed, "health")
    assert callable(feed.stream_bars)
    assert callable(feed.health)
    assert isinstance(feed.feed_id, str)


# ---------------------------------------------------------------------------
# Translation — _translate maps IbkrMinuteBar fields correctly
# ---------------------------------------------------------------------------


def test_translate_maps_all_fields() -> None:
    """_translate produces a MarketDataBar with correct int64 ms UTC timestamps."""
    ibkr_bar = _make_ibkr_bar(
        "AAPL",
        start_ms=1_700_000_120_000,
        open_="150.00",
        high="151.00",
        low="149.50",
        close="150.75",
        volume=2500,
    )
    bar = IbkrMarketDataFeed._translate(ibkr_bar)

    assert bar.symbol == "AAPL"
    assert bar.start_ms == 1_700_000_120_000
    assert bar.end_ms == 1_700_000_180_000
    assert bar.open == Decimal("150.00")
    assert bar.high == Decimal("151.00")
    assert bar.low == Decimal("149.50")
    assert bar.close == Decimal("150.75")
    assert bar.volume == 2500
    assert bar.feed_id == "ibkr"
    assert isinstance(bar.fetched_at_ms, int)
    assert bar.session_phase == "RTH"


def test_translate_produces_int64_ms_utc_timestamps() -> None:
    """Translated bar timestamps must be plain Python ints (int64-compatible)."""
    ibkr_bar = _make_ibkr_bar("SPY", start_ms=1_700_000_000_000)
    bar = IbkrMarketDataFeed._translate(ibkr_bar)

    assert type(bar.start_ms) is int
    assert type(bar.end_ms) is int
    assert type(bar.fetched_at_ms) is int
    # Not floats, not strings, not datetime objects
    assert bar.end_ms - bar.start_ms == 60_000


@pytest.mark.asyncio
async def test_recent_closed_bars_drops_the_forming_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IBKR history includes the still-forming minute; warmup must drop it.

    Regression for the 2026-08-25 fleet run: the forming bar was sealed into
    the source-bar ledger during warmup, so the live close of the same window
    arrived with a different payload and crashed the bot with
    SOURCE_BAR_IDENTITY_CONFLICT.
    """
    from app.utils.timestamps import now_ms_utc

    now = now_ms_utc()
    closed_start = (now // 60_000 - 2) * 60_000
    forming_start = (now // 60_000) * 60_000  # end_ms lands in the future

    async def _fake_history(*_args: Any, **_kwargs: Any) -> list[SimpleNamespace]:
        return [
            _make_ibkr_bar("SPY", start_ms=closed_start),
            _make_ibkr_bar("SPY", start_ms=forming_start, volume=42),
        ]

    monkeypatch.setattr(
        "app.marketdata.ibkr_feed.fetch_historical_minute_bars", _fake_history
    )

    feed = IbkrMarketDataFeed(_fake_connected_client())
    bars = await feed.recent_closed_bars("SPY", use_rth=False)

    assert [bar.start_ms for bar in bars] == [closed_start]
    assert all(bar.end_ms <= now_ms_utc() for bar in bars)


@pytest.mark.asyncio
async def test_recent_closed_bars_anchors_cutoff_before_history_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minute open when the request starts stays excluded after rollover."""
    request_started_ms = 1_700_000_059_000
    request_finished_ms = 1_700_000_061_000
    clock_ms = request_started_ms

    async def _history_crossing_minute_boundary(
        *_args: Any,
        **_kwargs: Any,
    ) -> list[SimpleNamespace]:
        nonlocal clock_ms
        clock_ms = request_finished_ms
        return [_make_ibkr_bar("SPY", start_ms=1_700_000_000_000, volume=42)]

    monkeypatch.setattr(
        "app.marketdata.ibkr_feed.now_ms_utc",
        lambda: clock_ms,
    )
    monkeypatch.setattr(
        "app.marketdata.ibkr_feed.fetch_historical_minute_bars",
        _history_crossing_minute_boundary,
    )

    feed = IbkrMarketDataFeed(_fake_connected_client())

    assert await feed.recent_closed_bars("SPY", use_rth=False) == []

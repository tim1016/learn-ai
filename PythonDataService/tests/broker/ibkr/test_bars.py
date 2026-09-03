"""Tests for app.broker.ibkr.bars."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker.ibkr import bars as bars_mod
from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarStreamError,
    IBKRBarSubscriptionStalled,
    LiveBarCounters,
    MinuteAssembler,
    aggregate_realtime_bar,
    fetch_historical_minute_bars,
    stream_minute_bars,
    stream_raw_5s_bars,
)


def _bar(second: int, open_: str, high: str, low: str, close: str, volume: int):
    return SimpleNamespace(
        time=datetime(2026, 5, 4, 14, 30, second, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def test_realtime_bars_aggregate_within_one_minute() -> None:
    current = None
    last_ms = None
    emitted = None
    for raw in (
        _bar(0, "100.00", "101.00", "99.00", "100.50", 10),
        _bar(5, "100.50", "102.00", "100.25", "101.50", 20),
        _bar(10, "101.50", "101.75", "98.50", "99.50", 30),
    ):
        current, emitted, last_ms = aggregate_realtime_bar(
            current,
            raw,
            symbol="SPY",
            last_source_ms=last_ms,
        )
        assert emitted is None

    assert current is not None
    minute = current.to_model()
    assert minute.open == Decimal("100.00")
    assert minute.high == Decimal("102.00")
    assert minute.low == Decimal("98.50")
    assert minute.close == Decimal("99.50")
    assert minute.volume == 60


def test_realtime_bar_provenance_stamped_on_emitted_minute() -> None:
    current = None
    last_ms = None
    current, _, last_ms = aggregate_realtime_bar(
        current,
        _bar(55, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=last_ms,
        venue="SMART",
        use_rth=False,
    )
    _current, emitted, _last_ms = aggregate_realtime_bar(
        current,
        SimpleNamespace(
            time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("101.5"),
            volume=20,
        ),
        symbol="SPY",
        last_source_ms=last_ms,
        venue="SMART",
        use_rth=False,
    )

    assert emitted is not None
    assert emitted.provenance == "ibkr_realtime"
    assert emitted.venue == "SMART"
    assert emitted.session_phase == "RTH"
    assert emitted.use_rth is False


def test_extended_hours_liveness_respects_weekend_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sunday_overnight_ms = int(
        datetime(2026, 8, 9, 5, 0, tzinfo=UTC).timestamp() * 1000
    )
    monkeypatch.setattr(bars_mod, "now_ms_utc", lambda: sunday_overnight_ms)

    assert bars_mod._session_phase_for_ms(sunday_overnight_ms) == "CLOSED"
    assert bars_mod._bars_expected_now(use_rth=False) is False


def test_new_minute_fires_previous_closed_bar() -> None:
    current = None
    last_ms = None
    current, emitted, last_ms = aggregate_realtime_bar(
        current,
        _bar(55, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=last_ms,
    )
    current, emitted, last_ms = aggregate_realtime_bar(
        current,
        SimpleNamespace(
            time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("101.5"),
            volume=20,
        ),
        symbol="SPY",
        last_source_ms=last_ms,
    )

    assert emitted is not None
    assert emitted.start_ms == int(datetime(2026, 5, 4, 14, 30, tzinfo=UTC).timestamp() * 1000)
    assert emitted.end_ms == int(datetime(2026, 5, 4, 14, 31, tzinfo=UTC).timestamp() * 1000)
    assert emitted.close == Decimal("100.5")
    assert current.start_ms == emitted.end_ms


def test_duplicate_source_timestamp_raises() -> None:
    current, _, last_ms = aggregate_realtime_bar(None, _bar(0, "1", "1", "1", "1", 1), symbol="SPY", last_source_ms=None)
    with pytest.raises(IBKRBarStreamError, match="Duplicate"):
        aggregate_realtime_bar(current, _bar(0, "1", "1", "1", "1", 1), symbol="SPY", last_source_ms=last_ms)


def test_non_monotonic_source_timestamp_raises() -> None:
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(10, "1", "1", "1", "1", 1),
        symbol="SPY",
        last_source_ms=None,
    )
    with pytest.raises(IBKRBarStreamError, match="Non-monotonic"):
        aggregate_realtime_bar(current, _bar(5, "1", "1", "1", "1", 1), symbol="SPY", last_source_ms=last_ms)


def test_live_exact_duplicate_skips_without_double_counting() -> None:
    counters = LiveBarCounters()
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(0, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=None,
        policy="live_idempotent",
        counters=counters,
    )
    current, emitted, returned_ms = aggregate_realtime_bar(
        current,
        _bar(0, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=last_ms,
        policy="live_idempotent",
        counters=counters,
    )

    assert emitted is None
    # last_source_ms stays anchored to the last distinct timestamp.
    assert returned_ms == last_ms
    assert counters.skipped_duplicate == 1
    assert counters.applied_correction == 0
    minute = current.to_model()
    assert minute.volume == 10
    assert minute.high == Decimal("101")


def test_live_correction_before_close_recomputes_ohlcv() -> None:
    counters = LiveBarCounters()
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(0, "100", "100.5", "99.5", "100.2", 10),
        symbol="SPY",
        last_source_ms=None,
        policy="live_idempotent",
        counters=counters,
    )
    current, _, last_ms = aggregate_realtime_bar(
        current,
        _bar(5, "100.2", "101.0", "100.0", "100.8", 15),
        symbol="SPY",
        last_source_ms=last_ms,
        policy="live_idempotent",
        counters=counters,
    )
    # IBKR redelivers the :05 bar with corrected, higher-range values.
    current, emitted, returned_ms = aggregate_realtime_bar(
        current,
        _bar(5, "100.2", "103.0", "98.0", "102.5", 25),
        symbol="SPY",
        last_source_ms=last_ms,
        policy="live_idempotent",
        counters=counters,
    )

    assert emitted is None
    assert returned_ms == last_ms
    assert counters.applied_correction == 1
    assert counters.skipped_duplicate == 0
    minute = current.to_model()
    # OHLCV recomputed from the corrected :05 contribution, not summed onto it.
    assert minute.open == Decimal("100")
    assert minute.high == Decimal("103.0")
    assert minute.low == Decimal("98.0")
    assert minute.close == Decimal("102.5")
    assert minute.volume == 35  # 10 + corrected 25, original 15 dropped


def test_unknown_duplicate_policy_raises() -> None:
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(0, "1", "1", "1", "1", 1),
        symbol="SPY",
        last_source_ms=None,
        policy="strict",
    )
    with pytest.raises(IBKRBarStreamError, match="Unknown duplicate policy"):
        aggregate_realtime_bar(
            current,
            _bar(0, "1", "1", "1", "1", 1),
            symbol="SPY",
            last_source_ms=last_ms,
            policy="bogus",  # type: ignore[arg-type]
        )


def test_live_regression_into_emitted_minute_still_fatal() -> None:
    """A bar from an already-closed minute is < last_source_ms → fatal even in live mode."""
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(55, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=None,
        policy="live_idempotent",
    )
    # Crossing into the next minute emits the closed bar.
    current, emitted, last_ms = aggregate_realtime_bar(
        current,
        SimpleNamespace(
            time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("101.5"),
            volume=20,
        ),
        symbol="SPY",
        last_source_ms=last_ms,
        policy="live_idempotent",
    )
    assert emitted is not None

    # IBKR redelivers a bar from the already-emitted 14:30 minute.
    with pytest.raises(IBKRBarStreamError, match="Non-monotonic"):
        aggregate_realtime_bar(
            current,
            _bar(55, "100", "101", "99", "100.5", 10),
            symbol="SPY",
            last_source_ms=last_ms,
            policy="live_idempotent",
        )


def test_live_non_monotonic_within_open_minute_still_fatal() -> None:
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(10, "1", "1", "1", "1", 1),
        symbol="SPY",
        last_source_ms=None,
        policy="live_idempotent",
    )
    with pytest.raises(IBKRBarStreamError, match="Non-monotonic"):
        aggregate_realtime_bar(
            current,
            _bar(5, "1", "1", "1", "1", 1),
            symbol="SPY",
            last_source_ms=last_ms,
            policy="live_idempotent",
        )


def test_naive_datetime_raises() -> None:
    raw = SimpleNamespace(
        time=datetime(2026, 5, 4, 14, 30),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )
    with pytest.raises(IBKRBarStreamError, match="naive"):
        aggregate_realtime_bar(None, raw, symbol="SPY", last_source_ms=None)


class _FakeIb:
    def __init__(self) -> None:
        self.bars = [
            _bar(55, "100", "101", "99", "100.5", 10),
            SimpleNamespace(
                time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
                open=Decimal("101"),
                high=Decimal("102"),
                low=Decimal("100"),
                close=Decimal("101.5"),
                volume=20,
            ),
        ]
        self.cancelled = False
        self.realtime_bar_request_count = 0
        self.realtime_bar_cancel_count = 0
        self.use_rth_seen: bool | None = None
        self.historical_bars = []
        self.historical_use_rth_seen: bool | None = None

    def reqRealTimeBars(self, contract, bar_size: int, what_to_show: str, *, useRTH: bool):
        self.realtime_bar_request_count += 1
        self.use_rth_seen = useRTH
        assert contract.symbol == "SPY"
        assert bar_size == 5
        assert what_to_show == "TRADES"
        return self.bars

    def cancelRealTimeBars(self, bars) -> None:
        assert bars is self.bars
        self.cancelled = True
        self.realtime_bar_cancel_count += 1

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        assert contract.symbol == "SPY"
        self.historical_use_rth_seen = kwargs["useRTH"]
        return self.historical_bars

    async def qualifyContractsAsync(self, contract):
        contract.conId = 1
        return [contract]


class _FakeClient:
    def __init__(self, *, connected: bool = True, connection_lost: bool = False) -> None:
        self.ib = _FakeIb()
        self._connected = connected
        self.connection_lost = connection_lost

    def require_connected(self) -> None:
        return

    def is_connected(self) -> bool:
        return self._connected


@pytest.mark.asyncio
async def test_stream_minute_bars_yields_closed_bar_and_cancels() -> None:
    client = _FakeClient()
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    emitted = await stream.__anext__()
    await stream.aclose()

    assert emitted.close == Decimal("100.5")
    assert client.ib.use_rth_seen is True
    assert client.ib.cancelled is True
    assert emitted.provenance == "ibkr_realtime"
    assert emitted.venue == "SMART"
    assert emitted.session_phase == "RTH"
    assert emitted.use_rth is True


@pytest.mark.asyncio
async def test_stream_minute_bars_reports_raw_source_activity() -> None:
    """#1411: source liveness advances before the minute consumer yields."""
    client = _FakeClient()
    client.ib.bars.insert(1, client.ib.bars[0])
    source_ms: list[int] = []
    stream = stream_minute_bars(
        client,
        "SPY",
        use_rth=True,
        on_source_bar=source_ms.append,
    )

    await stream.__anext__()
    await stream.aclose()

    assert source_ms == [
        int(client.ib.bars[0].time.timestamp() * 1000),
        int(client.ib.bars[2].time.timestamp() * 1000),
    ]


@pytest.mark.asyncio
async def test_raw_stream_invalidates_a_bounded_stalled_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411: a connected one-print/zero-print line cannot hang forever."""
    client = _FakeClient()
    client.ib.bars = []
    monkeypatch.setattr(bars_mod, "_bars_expected_now", lambda _use_rth: True)

    stream = stream_raw_5s_bars(
        client,
        "SPY",
        use_rth=True,
        stall_timeout_s=0.01,
    )

    with pytest.raises(IBKRBarSubscriptionStalled, match="stalled"):
        await stream.__anext__()

    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_minute_stream_one_print_then_silence_invalidates_without_a_closed_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1415 H1: one print cannot leave an unadvanced minute open forever."""
    client = _FakeClient()
    client.ib.bars = [_bar(0, "100", "101", "99", "100.5", 10)]
    source_ms: list[int] = []
    monkeypatch.setattr(bars_mod, "_bars_expected_now", lambda _use_rth: True)

    stream = stream_minute_bars(
        client,
        "SPY",
        use_rth=True,
        on_source_bar=source_ms.append,
        stall_timeout_s=0.01,
    )

    with pytest.raises(IBKRBarSubscriptionStalled, match="stalled"):
        await stream.__anext__()

    assert len(source_ms) == 1
    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_fetch_historical_minute_bars_stamps_provenance() -> None:
    client = _FakeClient()
    client.ib.historical_bars = [
        SimpleNamespace(
            date=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=20,
        )
    ]

    bars = await fetch_historical_minute_bars(client, "SPY", use_rth=False)

    assert len(bars) == 1
    assert bars[0].provenance == "ibkr_historical"
    assert bars[0].venue == "SMART"
    assert bars[0].session_phase == "RTH"
    assert bars[0].use_rth is False
    assert client.ib.historical_use_rth_seen is False


@pytest.mark.asyncio
async def test_stream_minute_bars_halts_on_connection_lost() -> None:
    """Regression (B-02): a mid-stream disconnect must surface a fatal error,
    not hang forever on a frozen bar list.

    Before the fix the loop only checked ``index >= len(bars)`` and slept,
    spinning indefinitely while the live engine went silently blind. Now an
    empty/stalled feed with a lost connection raises ``IBKRBarStreamError``.

    ``connection_lost`` is the TWS-1100 soft loss (socket up, market data
    gone); #1921 gave it its own message and ``cause``.
    """
    client = _FakeClient(connection_lost=True)
    client.ib.bars = []  # no bars ever arrive → loop reaches the liveness gate

    stream = stream_minute_bars(client, "SPY", use_rth=True)
    with pytest.raises(IBKRBarStreamError, match="connectivity lost"):
        await stream.__anext__()
    # The cancel still ran in finally despite the raise.
    assert client.ib.cancelled is True


@pytest.mark.asyncio
async def test_stream_minute_bars_cancel_exception_does_not_mask_original() -> None:
    """Regression (B-11): cancelRealTimeBars in ``finally`` must be guarded so
    a cancel that raises on a dead connection does not replace the real error
    propagating out of the generator."""
    client = _FakeClient(connection_lost=True)
    client.ib.bars = []

    def _raising_cancel(bars) -> None:
        raise ConnectionError("socket already closed")

    client.ib.cancelRealTimeBars = _raising_cancel  # type: ignore[assignment]

    stream = stream_minute_bars(client, "SPY", use_rth=True)
    # The connectivity-lost error survives; the cancel's ConnectionError is
    # swallowed (logged at debug) rather than masking it.
    with pytest.raises(IBKRBarStreamError, match="connectivity lost"):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_realtime_bar_request_pacer_waits_at_sliding_window_limit() -> None:
    now = 0.0
    waits: list[float] = []

    async def fake_sleep(delay_s: float) -> None:
        nonlocal now
        waits.append(delay_s)
        now += delay_s

    pacer = bars_mod._RealtimeBarRequestPacer(
        max_requests=2,
        window_s=10.0,
        clock=lambda: now,
        sleep=fake_sleep,
    )

    await pacer.acquire()
    await pacer.acquire()
    await pacer.acquire()

    assert waits == [10.0]


@pytest.mark.asyncio
async def test_same_symbol_5s_and_1m_consumers_share_one_broker_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One public client + contract must consume one shared market-data line."""
    client = _FakeClient()
    client.ib.bars = []
    registry = bars_mod._RealtimeBarSubscriptionRegistry()
    original_acquire = registry.acquire
    both_consumers_attached = asyncio.Event()
    acquire_count = 0

    async def observed_acquire(*args, **kwargs):
        nonlocal acquire_count
        lease = await original_acquire(*args, **kwargs)
        acquire_count += 1
        if acquire_count == 2:
            both_consumers_attached.set()
        return lease

    monkeypatch.setattr(registry, "acquire", observed_acquire)
    monkeypatch.setattr(bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", registry)

    raw_stream = stream_raw_5s_bars(client, "SPY", use_rth=True)
    minute_stream = stream_minute_bars(client, "SPY", use_rth=True)
    raw_next = asyncio.create_task(raw_stream.__anext__())
    minute_next = asyncio.create_task(minute_stream.__anext__())

    await asyncio.wait_for(both_consumers_attached.wait(), timeout=1.0)
    assert client.ib.realtime_bar_request_count == 1

    client.ib.bars.extend(
        [
            _bar(55, "100", "101", "99", "100.5", 10),
            SimpleNamespace(
                time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
                open=Decimal("101"),
                high=Decimal("102"),
                low=Decimal("100"),
                close=Decimal("101.5"),
                volume=20,
            ),
        ]
    )

    raw_bar = await asyncio.wait_for(raw_next, timeout=1.0)
    minute_bar = await asyncio.wait_for(minute_next, timeout=1.0)
    assert raw_bar.end_ms - raw_bar.start_ms == 5_000
    assert minute_bar.end_ms - minute_bar.start_ms == 60_000

    await raw_stream.aclose()
    assert client.ib.realtime_bar_cancel_count == 0
    await minute_stream.aclose()
    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_late_shared_consumer_starts_after_existing_list_tail() -> None:
    """Multiplexing must not turn the mutable IB list into implicit replay."""
    client = _FakeClient()
    registry = bars_mod._RealtimeBarSubscriptionRegistry()
    contract = SimpleNamespace(conId=1, symbol="SPY")

    first = await registry.acquire(
        client,
        contract,
        bar_size=5,
        what_to_show="TRADES",
        use_rth=True,
    )
    second = await registry.acquire(
        client,
        contract,
        bar_size=5,
        what_to_show="TRADES",
        use_rth=True,
    )

    assert first.start_index == 0
    assert second.start_index == len(client.ib.bars)
    assert second.multiplexed is True
    assert client.ib.realtime_bar_request_count == 1

    first.release()
    assert client.ib.realtime_bar_cancel_count == 0
    second.release()
    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_invalidated_generation_cannot_release_its_replacement() -> None:
    """#1411: late cleanup from the dead line must not cancel the new line."""
    client = _FakeClient()
    client.ib.bars = []
    registry = bars_mod._RealtimeBarSubscriptionRegistry()
    contract = SimpleNamespace(conId=1, symbol="SPY")

    stalled = await registry.acquire(
        client, contract, bar_size=5, what_to_show="TRADES", use_rth=True
    )
    stalled_peer = await registry.acquire(
        client, contract, bar_size=5, what_to_show="TRADES", use_rth=True
    )

    assert stalled.invalidate() is True
    assert client.ib.realtime_bar_cancel_count == 1

    client.ib.bars = []
    replacement = await registry.acquire(
        client, contract, bar_size=5, what_to_show="TRADES", use_rth=True
    )

    assert stalled_peer.release() is False
    assert client.ib.realtime_bar_cancel_count == 1
    assert replacement.release() is True
    assert client.ib.realtime_bar_cancel_count == 2


@pytest.mark.asyncio
async def test_realtime_bar_registry_refuses_new_line_at_local_active_cap() -> None:
    client = _FakeClient()
    registry = bars_mod._RealtimeBarSubscriptionRegistry(default_max_active=1)
    first_contract = SimpleNamespace(conId=1, symbol="SPY")
    second_contract = SimpleNamespace(conId=2, symbol="SPY")

    first = await registry.acquire(
        client,
        first_contract,
        bar_size=5,
        what_to_show="TRADES",
        use_rth=True,
    )
    with pytest.raises(IBKRBarStreamError, match="local active-line cap reached"):
        await registry.acquire(
            client,
            second_contract,
            bar_size=5,
            what_to_show="TRADES",
            use_rth=True,
        )

    assert client.ib.realtime_bar_request_count == 1
    first.release()


def test_aggregate_handles_ib_async_open_underscore_attribute() -> None:
    """Regression for the production wire type.

    ``ib_async.RealTimeBar`` declares ``open_: float`` (trailing underscore
    to avoid shadowing the ``open()`` builtin). The test fakes earlier in
    this file use plain ``open`` for readability, which left the production
    path uncovered until ``_decimal_attr`` learned the dual lookup.
    """
    raw = SimpleNamespace(
        time=datetime(2026, 5, 4, 14, 30, 0, tzinfo=UTC),
        open_=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
    )

    current, emitted, last_ms = aggregate_realtime_bar(
        None, raw, symbol="SPY", last_source_ms=None,
    )

    assert current is not None
    assert emitted is None
    assert last_ms == int(raw.time.timestamp() * 1000)
    minute = current.to_model()
    assert minute.open == Decimal("100.00")
    assert minute.high == Decimal("101.00")
    assert minute.low == Decimal("99.00")
    assert minute.close == Decimal("100.50")
    assert minute.volume == 10


# ---------------------------------------------------------------------------
# Log-level demotion (incident taxonomy PR-3, plan §4.2 / codex D4): the
# idempotent-skip log was demoted from WARNING to INFO so per-bar
# redeliveries no longer land in the Recent Incidents panel. The
# ``skipped_duplicate`` counter + the aggregate SUBSCRIPTION_STALE
# WARNING still satisfy the ADR's "surface, never silence" intent.
# ---------------------------------------------------------------------------


def test_live_idempotent_skip_logs_at_info_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sets up the second-feed-of-same-bar duplicate-skip path and asserts
    # the emitted log record is INFO. A regression here (a future change
    # bumping it back to WARNING) re-introduces ~80% of the Incidents
    # panel noise documented in unknown-incident-modes-2026-06-24.md.
    counters = LiveBarCounters()
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(0, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=None,
        policy="live_idempotent",
        counters=counters,
    )

    caplog.clear()
    with caplog.at_level("INFO", logger="app.broker.ibkr.minute_assembler"):
        aggregate_realtime_bar(
            current,
            _bar(0, "100", "101", "99", "100.5", 10),
            symbol="SPY",
            last_source_ms=last_ms,
            policy="live_idempotent",
            counters=counters,
        )

    skips = [r for r in caplog.records if r.message.startswith("Idempotent skip")]
    assert len(skips) == 1
    assert skips[0].levelname == "INFO"
    # The structured `extra` must survive the demotion — the classifier
    # and any downstream telemetry key off `action`.
    assert skips[0].action == "skipped_duplicate"
    assert counters.skipped_duplicate == 1


def test_live_applied_correction_still_logs_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sibling guard: the "Applied correction" emit was deliberately
    # left at WARNING in PR-3 because corrections actually change the
    # bar's value, unlike the idempotent skip path.
    counters = LiveBarCounters()
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(0, "100", "100.5", "99.5", "100.2", 10),
        symbol="SPY",
        last_source_ms=None,
        policy="live_idempotent",
        counters=counters,
    )

    caplog.clear()
    with caplog.at_level("INFO", logger="app.broker.ibkr.minute_assembler"):
        aggregate_realtime_bar(
            current,
            _bar(0, "100", "101", "99", "100.5", 10),
            symbol="SPY",
            last_source_ms=last_ms,
            policy="live_idempotent",
            counters=counters,
        )

    corrections = [r for r in caplog.records if r.message.startswith("Applied correction")]
    assert len(corrections) == 1
    assert corrections[0].levelname == "WARNING"
    assert counters.applied_correction == 1


# ---------------------------------------------------------------------------
# Connection-generation fencing (#1921). ``ib_async`` reuses one ``IB()``
# across reconnects: ``Wrapper.reset()`` orphans the old ``bars`` list and
# ``Client.reset()`` restarts request ids. A lease taken on the previous
# socket must therefore be detected as stale on every loop iteration, and
# its release must never send ``cancelRealTimeBars`` — the reqId it holds
# may already belong to a subscription on the new socket.
# ---------------------------------------------------------------------------


class _GenClient(_FakeClient):
    """Fake client whose generation the test can bump."""

    def __init__(self, *, connected: bool = True, connection_lost: bool = False) -> None:
        super().__init__(connected=connected, connection_lost=connection_lost)
        self.connection_generation = 1


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bars_mod,
        "_REALTIME_BAR_SUBSCRIPTIONS",
        bars_mod._RealtimeBarSubscriptionRegistry(),
    )


@pytest.mark.asyncio
async def test_stale_generation_lease_raises_interrupted_even_when_socket_is_back() -> None:
    client = _GenClient()
    client.ib.bars = []  # nothing to deliver: force the idle branch
    stream = stream_minute_bars(client, "SPY", use_rth=True, stall_timeout_s=60.0)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)  # one idle iteration under generation 1
    client.connection_generation = 2  # reconnect happened; socket reports connected
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)
    assert excinfo.value.cause == "generation_changed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_socket_down_raises_interrupted_with_cause() -> None:
    client = _GenClient()
    client.ib.bars = []
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client._connected = False
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)
    assert excinfo.value.cause == "socket_down"
    assert "IBKR connection lost" in str(excinfo.value)
    await stream.aclose()


@pytest.mark.asyncio
async def test_soft_loss_1100_raises_interrupted_with_cause() -> None:
    client = _GenClient()
    client.ib.bars = []
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.connection_lost = True  # TWS 1100: socket up, market data gone
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)
    assert excinfo.value.cause == "soft_loss_1100"
    await stream.aclose()


@pytest.mark.asyncio
async def test_raw_5s_stream_drains_queued_prints_before_the_interruption() -> None:
    """The raw chart stream drains the same way: a queued print is still a print."""
    client = _GenClient()
    backlog = client.ib.bars
    client.ib.bars = []
    stream = stream_raw_5s_bars(client, "SPY", use_rth=True, stall_timeout_s=60.0)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.ib.bars.extend(backlog)
    client.connection_generation = 2

    drained = [await asyncio.wait_for(first, timeout=2.0)]
    drained.append(await asyncio.wait_for(stream.__anext__(), timeout=2.0))

    assert [bar.close for bar in drained] == [Decimal("100.5"), Decimal("101.5")]
    with pytest.raises(IBKRBarInterrupted):
        await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    await stream.aclose()


@pytest.mark.asyncio
async def test_raw_5s_stream_stale_generation_raises_interrupted() -> None:
    client = _GenClient()
    client.ib.bars = []
    stream = stream_raw_5s_bars(client, "SPY", use_rth=True, stall_timeout_s=60.0)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.connection_generation = 2
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)
    assert excinfo.value.cause == "generation_changed"
    await stream.aclose()
    assert client.ib.realtime_bar_cancel_count == 0


@pytest.mark.asyncio
async def test_queued_prints_are_drained_before_the_interruption_surfaces() -> None:
    """The check runs every iteration, and the queue is emptied before it fires.

    Ruling P10: ``Wrapper.reset()`` orphans this list on reconnect, so nothing
    the new socket produces can ever land on it — every bar still queued is a
    print the old socket really delivered. They are folded first, then the
    interruption surfaces, so the interruption is neither deferred by a 60 s
    stall nor paid for with observations the vendor did send.
    """
    client = _GenClient()
    backlog = client.ib.bars  # two 5-second bars, spanning a minute boundary
    client.ib.bars = []
    stream = stream_minute_bars(client, "SPY", use_rth=True, stall_timeout_s=60.0)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.ib.bars.extend(backlog)
    client.connection_generation = 2

    drained = await asyncio.wait_for(first, timeout=2.0)

    assert drained.close == Decimal("100.5")  # the minute the queued prints closed
    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert excinfo.value.cause == "generation_changed"
    await stream.aclose()


class _AlwaysQueuedBars(list):
    """A bar list that always has one more bar queued behind the reader.

    Reading a bar appends the next, so ``index >= len(bars)`` never holds and
    the loop's idle branch is never reached. A liveness gate that only guarded
    that branch could therefore never fire against this list; the
    every-iteration gate can. The cap keeps a gate that never fires
    *terminating* — it eventually runs out of bars and reaches the idle branch
    — rather than spinning the event loop forever.
    """

    def __init__(self, make_bar, *, cap: int = 200) -> None:
        super().__init__([make_bar(0)])
        self._make_bar = make_bar
        self._cap = cap

    def __getitem__(self, item):
        if len(self) < self._cap:
            self.append(self._make_bar(len(self)))
        return super().__getitem__(item)


@pytest.mark.asyncio
async def test_the_liveness_gate_fires_on_a_stream_that_never_goes_idle() -> None:
    """The gate runs every iteration, not only when the bar list is empty.

    The drain tests above cannot show this: their queues run dry, so an
    idle-branch-only gate would still fire, just later. Here the queue never
    runs dry. ``ib_async`` stops appending on a Gateway disconnect, but a
    reconnect can land while a backlog is still being worked through, and a
    consumer blind until the backlog clears is exactly the silent-blindness
    failure this gate exists to prevent.
    """
    base = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)

    def _make(index: int) -> SimpleNamespace:
        return SimpleNamespace(
            time=base + timedelta(seconds=5 * index),
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100.5"), volume=1,
        )

    client = _GenClient()
    client.ib.bars = _AlwaysQueuedBars(_make)
    stream = stream_minute_bars(client, "SPY", use_rth=True, stall_timeout_s=60.0)

    first = await stream.__anext__()  # closes 14:30 without ever idling
    assert first.start_ms == int(base.timestamp() * 1000)

    client.connection_generation = 2  # a reconnect, with the backlog still flowing

    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert excinfo.value.cause == "generation_changed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_queued_prints_reach_the_shared_assembler_before_the_interruption() -> None:
    """Two prints of one minute, a reconnect, and both contributions survive.

    This is the case the drain exists for: the minute is still open, so nothing
    is yielded, and discarding the queue would leave the caller's assembler
    holding 0/12 when the reconnect could have completed 12/12.
    """
    client = _GenClient()
    minute_start_ms = int(datetime(2026, 5, 4, 14, 30, tzinfo=UTC).timestamp() * 1000)
    queued = [
        _bar(0, "100", "101", "99", "100.5", 10),
        _bar(5, "100.5", "102", "100", "101.5", 20),
    ]
    client.ib.bars = []
    assembler = bars_mod.MinuteAssembler()
    stream = stream_minute_bars(
        client, "SPY", use_rth=True, stall_timeout_s=60.0, assembler=assembler
    )
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.ib.bars.extend(queued)
    client.connection_generation = 2

    with pytest.raises(IBKRBarInterrupted) as excinfo:
        await asyncio.wait_for(first, timeout=2.0)

    assert excinfo.value.cause == "generation_changed"
    assert assembler.open_minute_start_ms == minute_start_ms
    assert assembler.current is not None and len(assembler.current.contributions) == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_stale_lease_release_never_cancels_on_the_new_socket() -> None:
    client = _GenClient()
    client.ib.bars = []
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.15)
    client.connection_generation = 2
    with pytest.raises(IBKRBarInterrupted):
        await asyncio.wait_for(first, timeout=2.0)
    await stream.aclose()  # releases the stale lease
    assert client.ib.realtime_bar_cancel_count == 0


@pytest.mark.asyncio
async def test_acquire_after_generation_change_opens_a_new_line() -> None:
    client = _GenClient()
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")
    registry = bars_mod._REALTIME_BAR_SUBSCRIPTIONS
    lease_old = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    client.connection_generation = 2
    lease_new = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    assert lease_new.multiplexed is False
    assert lease_new.generation == 2
    assert lease_old.generation == 1
    assert client.ib.realtime_bar_request_count == 2
    lease_new.release()
    lease_old.release()
    assert client.ib.realtime_bar_cancel_count == 1  # only the live generation cancelled


@pytest.mark.asyncio
async def test_acquire_restarts_when_generation_moves_during_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _GenClient()
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")
    registry = bars_mod._REALTIME_BAR_SUBSCRIPTIONS

    async def _bump_generation() -> None:
        client.connection_generation = 2

    monkeypatch.setattr(registry._pacer, "acquire", _bump_generation)
    lease = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    assert lease.generation == 2
    lease.release()


@pytest.mark.asyncio
async def test_active_line_cap_ignores_evicted_previous_generations() -> None:
    """A reconnect must not exhaust the cap with lines the old socket owned."""
    client = _GenClient()
    registry = bars_mod._RealtimeBarSubscriptionRegistry(default_max_active=1)
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")

    old = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    client.connection_generation = 2
    new = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)

    assert new.generation == 2
    assert old.release() is False
    assert new.release() is True
    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_active_line_cap_ignores_a_pending_line_from_a_previous_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line still waiting on the pacer when the socket flips must not hold a slot."""
    client = _GenClient()
    registry = bars_mod._RealtimeBarSubscriptionRegistry(default_max_active=1)
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")
    reached_pacer = asyncio.Event()
    unblock_pacer = asyncio.Event()

    async def _gated_acquire() -> None:
        if reached_pacer.is_set():
            return
        reached_pacer.set()
        await unblock_pacer.wait()

    monkeypatch.setattr(registry._pacer, "acquire", _gated_acquire)

    pending = asyncio.ensure_future(
        registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    )
    await asyncio.wait_for(reached_pacer.wait(), timeout=1.0)
    client.connection_generation = 2

    live = await registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    assert live.generation == 2

    unblock_pacer.set()
    restarted = await asyncio.wait_for(pending, timeout=1.0)
    # The stalled acquisition restarted and multiplexed onto the live socket
    # rather than filing a request under the generation it started in.
    assert restarted.generation == 2
    assert restarted.multiplexed is True
    assert client.ib.realtime_bar_request_count == 1

    assert restarted.release() is False
    assert live.release() is True
    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_waiter_woken_after_a_reconnect_does_not_spend_pacing_budget() -> None:
    """A woken waiter must re-read the generation before it can lead an acquisition.

    Uses a real ``_RealtimeBarRequestPacer`` — its budget is the thing under
    test — with only the injected clock and sleep hooks under the test's
    control. A waiter that resumes on the pre-reconnect key becomes the leader
    for a dead line and burns a pacing slot on a ``reqRealTimeBars`` that is
    never issued. That slot is exactly what a fleet-wide reconnect storm (60
    new lines per 600 s, when the pacer actually sleeps) cannot spare.
    """
    now = 0.0
    sleeps: list[float] = []
    leader_is_pacing = asyncio.Event()
    release_pacer = asyncio.Event()

    async def gated_sleep(delay_s: float) -> None:
        nonlocal now
        sleeps.append(delay_s)
        leader_is_pacing.set()
        await release_pacer.wait()
        now += delay_s

    pacer = bars_mod._RealtimeBarRequestPacer(
        max_requests=1,
        window_s=10.0,
        clock=lambda: now,
        sleep=gated_sleep,
    )
    await pacer.acquire()  # window now full: the next new line has to pace
    assert sleeps == []

    registry = bars_mod._RealtimeBarSubscriptionRegistry(pacer)
    client = _GenClient()
    client.ib.bars = []
    contract = SimpleNamespace(conId=1, symbol="SPY", secType="STK")

    leader = asyncio.ensure_future(
        registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    )
    await asyncio.wait_for(leader_is_pacing.wait(), timeout=1.0)
    waiter = asyncio.ensure_future(
        registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=True)
    )
    await asyncio.sleep(0)  # the waiter parks on the leader's pending future

    client.connection_generation = 2  # the reconnect lands while the leader paces
    release_pacer.set()

    leader_lease = await asyncio.wait_for(leader, timeout=1.0)
    waiter_lease = await asyncio.wait_for(waiter, timeout=1.0)

    assert leader_lease.generation == 2
    assert waiter_lease.generation == 2
    assert waiter_lease.multiplexed is True  # it joined the live line
    assert client.ib.realtime_bar_request_count == 1  # one request, live generation only
    # Two pacer waits, both the leader's: the pass that was parked when the
    # socket flipped, and the pass that actually issued the request. A waiter
    # that led under the dead key would add a third — budget spent on a
    # request that is never made.
    assert sleeps == [10.0, 10.0]

    waiter_lease.release()
    leader_lease.release()
    assert client.ib.realtime_bar_cancel_count == 1


@pytest.mark.asyncio
async def test_shared_assembler_stitches_one_minute_across_two_stream_calls() -> None:
    """The consumer-owned assembler is what makes a reconnect survivable.

    The first call takes three 5-second bars on generation 1 and is
    interrupted; the second call, on generation 2, delivers the rest of the
    same minute. The minute must emit once, complete by count, and admit that
    its contributions arrived over two connections.
    """
    assembler = MinuteAssembler()
    client = _GenClient()
    client.ib.bars = [_bar(second, "100", "100", "100", "100", 1) for second in (0, 5, 10)]

    first = stream_minute_bars(client, "SPY", use_rth=True, assembler=assembler, stall_timeout_s=60.0)
    pending = asyncio.ensure_future(first.__anext__())
    await asyncio.sleep(0.15)  # drain the three bars, then idle
    assert assembler.open_minute_start_ms == int(client.ib.bars[0].time.timestamp() * 1000)
    client.connection_generation = 2
    with pytest.raises(IBKRBarInterrupted):
        await asyncio.wait_for(pending, timeout=2.0)
    await first.aclose()

    client.ib.bars = [_bar(second, "100", "100", "100", "100", 1) for second in range(15, 60, 5)]
    client.ib.bars.append(
        SimpleNamespace(
            time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
            open=Decimal("101"),
            high=Decimal("101"),
            low=Decimal("101"),
            close=Decimal("101"),
            volume=1,
        )
    )
    second_stream = stream_minute_bars(client, "SPY", use_rth=True, assembler=assembler, stall_timeout_s=60.0)
    emitted = await asyncio.wait_for(second_stream.__anext__(), timeout=2.0)
    await second_stream.aclose()

    assert emitted.start_ms == int(datetime(2026, 5, 4, 14, 30, 0, tzinfo=UTC).timestamp() * 1000)
    assert emitted.contribution_count == 12
    assert emitted.spans_interruption is True

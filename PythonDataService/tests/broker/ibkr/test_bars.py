"""Tests for app.broker.ibkr.bars."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker.ibkr import bars as bars_mod
from app.broker.ibkr.bars import (
    IBKRBarStreamError,
    LiveBarCounters,
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
    """
    client = _FakeClient(connection_lost=True)
    client.ib.bars = []  # no bars ever arrive → loop reaches the liveness gate

    stream = stream_minute_bars(client, "SPY", use_rth=True)
    with pytest.raises(IBKRBarStreamError, match="connection lost"):
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
    with pytest.raises(IBKRBarStreamError, match="connection lost"):
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
    with caplog.at_level("INFO", logger="app.broker.ibkr.bars"):
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
    with caplog.at_level("INFO", logger="app.broker.ibkr.bars"):
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

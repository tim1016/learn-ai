"""An extended run's line going silent in PRE/POST enters ADR 0053 continuity (#2313).

Everything between the IBKR transport and the continuity sink is production
code: ``IbkrMarketDataFeed``, ``ContinuityLoop``, ``stream_minute_bars``, the
shared-subscription registry, the liveness gate and the canonical calendar.
Only the ``ib_async`` transport and the wall/monotonic clocks are faked.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.ibkr import bars as bars_mod
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    SubstitutionRefusal,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.decision_session import RunDecisionSession
from tests._helpers.ibkr_feed_adversarial import AcceleratedFeedClock

_ET = ZoneInfo("America/New_York")
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_TF = 900_000


def _et_ms(hour: int, minute: int, *, day: tuple[int, int, int] = (2026, 9, 22)) -> int:
    return int(datetime(*day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def _raw_5s(source_ms: int) -> SimpleNamespace:
    price = Decimal("1")
    return SimpleNamespace(
        time=datetime.fromtimestamp(source_ms / 1000, tz=UTC),
        open=price, high=price, low=price, close=price, volume=1,
    )


class _Transport:
    """``ib_async.IB`` stand-in: the first line delivers ``prints``, then nothing ever again."""

    def __init__(self, prints: list[SimpleNamespace]) -> None:
        self.lines: list[list[SimpleNamespace]] = []
        self._prints = prints

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        contract.conId = 756_733
        return [contract]

    def reqRealTimeBars(self, contract: Any, bar_size: int, what_to_show: str, *, useRTH: bool) -> list:
        assert (bar_size, what_to_show, useRTH) == (5, "TRADES", False)
        line = list(self._prints) if not self.lines else []
        self.lines.append(line)
        return line

    def cancelRealTimeBars(self, bars: list) -> None:
        return


class _Client:
    def __init__(self, transport: _Transport) -> None:
        self.ib = transport
        self.connection_lost = False
        self.connection_generation = 1
        # The 1101 data-loss fence is unconditional too (#2393); nothing here sends a 1101.
        self.data_loss_epoch = 0
        self.connected_account = None
        self.settings = SimpleNamespace(feed_continuity_enabled=True)

    def require_connected(self) -> None:
        return

    def is_connected(self) -> bool:
        return True


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[FeedContinuityEvent] = []

    async def __call__(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        self.events.append(event)
        return ContinuityEventRef(run_id="run-1", evidence_seq=len(self.events))


def _next_trigger(last_end: int) -> int:
    candidate = (last_end // _TF) * _TF + 60_000
    return candidate if candidate > last_end else candidate + _TF


def _extended_policy(sink: _RecordingSink) -> ContinuityPolicy:
    return ContinuityPolicy(
        session=RunDecisionSession(kind="extended", window=_WINDOW),
        next_trigger_ms=_next_trigger,
        substitution_grant=lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=sink,
    )


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", bars_mod._RealtimeBarSubscriptionRegistry())
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: None)


def _install(clock: AcceleratedFeedClock, monkeypatch: pytest.MonkeyPatch) -> None:
    for target in (
        "app.broker.ibkr.bars.now_ms_utc",
        "app.broker.ibkr.minute_assembler.now_ms_utc",
        "app.marketdata.ibkr_feed.now_ms_utc",
        "app.marketdata.ibkr_continuity.now_ms_utc",
    ):
        monkeypatch.setattr(target, clock.now_ms)
    # Only ``bars``' own view of the monotonic clock: patching ``time.monotonic``
    # itself would also freeze the event loop's clock, and with it every sleep.
    monkeypatch.setattr(bars_mod, "time", SimpleNamespace(monotonic=clock.monotonic))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "minute_start_ms",
    [pytest.param(_et_ms(7, 29), id="pre-market"), pytest.param(_et_ms(17, 29), id="after-hours")],
)
async def test_extended_run_line_silent_outside_rth_opens_a_stall_interruption(
    monkeypatch: pytest.MonkeyPatch, minute_start_ms: int
) -> None:
    """A connected line that stops printing in PRE/POST is a ``stall`` interruption.

    Before #2313 the stall timer was reset on every iteration outside calendar
    RTH, so this run sat blind with no continuity event, no deadline and no
    refusal until IBKR resumed on its own.
    """
    clock = AcceleratedFeedClock(wall_ms=minute_start_ms + 60_000)
    _install(clock, monkeypatch)
    transport = _Transport([_raw_5s(minute_start_ms + second * 1_000) for second in range(0, 60, 5)])
    sink = _RecordingSink()
    feed = IbkrMarketDataFeed(_Client(transport))  # type: ignore[arg-type]

    async with aclosing(feed.stream_bars("SPY", use_rth=False, continuity=_extended_policy(sink))) as bars:
        first = await asyncio.wait_for(anext(bars), timeout=2)
        pending = asyncio.ensure_future(anext(bars))
        try:
            await asyncio.sleep(0.25)  # the stream is idle-polling the silent line
            clock.advance_ms(61_000)
            for _ in range(40):
                if sink.events:
                    break
                await asyncio.sleep(0.05)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert first.start_ms == minute_start_ms
    assert [(event.kind, event.cause) for event in sink.events[:1]] == [("interruption", "stall")]
    assert sink.events[0].last_delivered_end_ms == minute_start_ms + 60_000
    # The same root cause stamped every extended minute ``CLOSED`` (#2299, P3).
    assert first.session_phase in {"PRE", "POST"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session",
    [
        pytest.param(RunDecisionSession(kind="rth", window=None), id="rth-only-run"),
        pytest.param(RunDecisionSession(kind="extended", window=_WINDOW), id="extended-run"),
    ],
)
async def test_line_ibkr_never_started_in_pre_does_not_churn(
    monkeypatch: pytest.MonkeyPatch, session: RunDecisionSession
) -> None:
    """#2299 review: a line silent since 04:00 is not a stall, so it is not re-requested.

    Every trade run streams ``use_rth=False``, RTH-only runs included. Were the
    PRE timer armed before the line's first print, each such run would open a
    stall interruption and issue a fresh ``reqRealTimeBars`` every 60 s from
    04:00 -- burning the shared 60-per-600 s pacing budget and, for an
    extended run, dying at about 04:01 on a line IBKR had simply not started.
    """
    clock = AcceleratedFeedClock(wall_ms=_et_ms(4, 0) + 30_000)
    _install(clock, monkeypatch)
    transport = _Transport([])
    sink = _RecordingSink()
    feed = IbkrMarketDataFeed(_Client(transport))  # type: ignore[arg-type]
    policy = ContinuityPolicy(
        session=session,
        next_trigger_ms=_next_trigger,
        substitution_grant=lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=sink,
    )

    async with aclosing(feed.stream_bars("SPY", use_rth=False, continuity=policy)) as bars:
        pending = asyncio.ensure_future(anext(bars))
        try:
            for _ in range(5):  # five stall timeouts of silence
                await asyncio.sleep(0.25)
                clock.advance_ms(61_000)
            await asyncio.sleep(0.25)
            assert not pending.done()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert sink.events == []
    assert len(transport.lines) == 1

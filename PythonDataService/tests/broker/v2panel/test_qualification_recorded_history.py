"""The qualification-only recorded history provider (issue #2206).

Covers ``app.services.broker_v2_panel.qualification_recorded_history``: the
deterministic generator's calendar alignment and OHLC invariants, its
determinism/continuity across independent calls, the full walk round-trip
through the real (unmodified) ``fetch_complete_history_batch``, and the
mode toggle (healthy / slow / unavailable) that lets the qualification
ceremony prove success, failure and recovery.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from app.data_lake.polygon_fetcher import PolygonBar
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.services.broker_v2_panel import qualification_recorded_history as recorded
from app.services.broker_v2_panel.qualification_recorded_history import (
    RecordedHistoryInjectedUnavailable,
    build_qualification_recorded_history_batch,
    recorded_bar_source,
    recorded_history_mode,
    reset_recorded_history_mode_for_testing,
    set_recorded_history_mode,
)

_AS_OF_MS = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC, an arbitrary instant
_SYMBOL = "QUALHIST"


@pytest.fixture(autouse=True)
def _reset_mode() -> None:
    reset_recorded_history_mode_for_testing()
    yield
    reset_recorded_history_mode_for_testing()


# ---- the deterministic generator --------------------------------------------


def test_stable_unit_fraction_is_pure_and_bounded() -> None:
    a = recorded._stable_unit_fraction(_SYMBOL, 123_456, salt=0)
    b = recorded._stable_unit_fraction(_SYMBOL, 123_456, salt=0)
    c = recorded._stable_unit_fraction(_SYMBOL, 123_456, salt=1)

    assert a == b
    assert 0.0 <= a < 1.0
    assert a != c  # a different salt must not collide by construction


def test_deterministic_bar_is_reproducible_and_internally_consistent() -> None:
    t_ms = 1_700_000_400_000
    span_ms = 60_000

    first = recorded._deterministic_bar(_SYMBOL, t_ms, span_ms)
    second = recorded._deterministic_bar(_SYMBOL, t_ms, span_ms)

    assert first == second
    assert first.t_ms == t_ms
    assert first.high >= max(first.open, first.close)
    assert first.low <= min(first.open, first.close)
    assert first.volume > 0


def test_deterministic_bar_open_equals_the_previous_slots_close() -> None:
    """Continuity across independently-fetched, non-overlapping windows: the
    same instant always yields the same bar regardless of which call fetched
    it (issue #2206 -- the walk's widening loop fetches disjoint ranges)."""
    t_ms = 1_700_000_400_000
    span_ms = 60_000

    bar = recorded._deterministic_bar(_SYMBOL, t_ms, span_ms)
    previous_close = recorded._deterministic_close(_SYMBOL, t_ms - span_ms)

    assert bar.open == previous_close


def test_recorded_bars_align_to_real_nyse_session_opens_for_day_bars() -> None:
    """No hardcoded session time: every bar starts at the canonical
    calendar's actual session open (temporal-rigor.md)."""
    start = date(2024, 1, 2)
    end = date(2024, 1, 5)

    bars = recorded._recorded_bars(_SYMBOL, start, end, 1, "day")
    windows = session_windows_ms_utc(start, end)

    assert [bar.t_ms for bar in bars] == [window.open_ms_utc for window in windows]
    assert len(bars) == len(windows) == 4  # Tue-Fri, no holiday in range


def test_recorded_bars_skip_weekends_and_respect_a_real_half_day() -> None:
    """2023-11-23 (Thanksgiving) is a holiday; 2023-11-24 is a real NYSE
    half-day; 2023-11-25/26 is the following weekend -- none of those three
    non-trading days may produce a bar."""
    start = date(2023, 11, 23)
    end = date(2023, 11, 26)

    bars = recorded._recorded_bars(_SYMBOL, start, end, 1, "day")

    assert len(bars) == 1  # only Fri 11/24 (the half-day) is a session
    assert bars[0].t_ms == session_windows_ms_utc(start, end)[0].open_ms_utc


def test_recorded_minute_bars_stay_within_each_session_and_step_by_the_span() -> None:
    start = date(2024, 1, 2)
    end = date(2024, 1, 2)
    window = session_windows_ms_utc(start, end)[0]

    bars = recorded._recorded_bars(_SYMBOL, start, end, 1, "minute")

    assert bars[0].t_ms == window.open_ms_utc
    assert all(bar.t_ms + 60_000 <= window.close_ms_utc for bar in bars)
    starts = [bar.t_ms for bar in bars]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)
    # A regular RTH session is 6.5 hours = 390 one-minute bars.
    assert len(bars) == 390


def test_recorded_bars_rejects_an_unsupported_timespan() -> None:
    with pytest.raises(ValueError, match="unsupported timespan"):
        recorded._recorded_bars(_SYMBOL, date(2024, 1, 2), date(2024, 1, 2), 1, "week")


async def test_recorded_bar_source_matches_the_history_bar_source_protocol() -> None:
    bars = await recorded_bar_source(_SYMBOL, date(2024, 1, 2), date(2024, 1, 2), 1, "day")

    assert len(bars) == 1
    assert isinstance(bars[0], PolygonBar)


# ---- the complete-batch seam: reuses the real, unmodified walk --------------


async def test_build_qualification_recorded_history_batch_runs_the_real_walk() -> None:
    """The response must satisfy the strict wire contract (bars strictly
    increasing, source='polygon', echoed effective_as_of_ms) -- exactly the
    same invariant the production coordinator's response must satisfy,
    because this reuses ``fetch_complete_history_batch`` unmodified."""
    batch = await build_qualification_recorded_history_batch(
        symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
    )

    assert batch.source == "polygon"
    assert batch.effective_as_of_ms == _AS_OF_MS
    assert batch.overlay_notices == []
    assert len(batch.bars) >= 5
    starts = [bar.start_ms for bar in batch.bars]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)
    assert all(bar.source == "polygon" for bar in batch.bars)
    assert all(bar.end_ms > bar.start_ms for bar in batch.bars)


async def test_build_qualification_recorded_history_batch_covers_intraday_timeframe() -> None:
    """Exercises the non-day branch (span-stepped, not session-per-bar)."""
    batch = await build_qualification_recorded_history_batch(
        symbol=_SYMBOL, timeframe="1m", required_bar_count=300, as_of_ms=_AS_OF_MS
    )

    assert len(batch.bars) >= 300
    assert batch.overlay_notices == []


async def test_same_as_of_ms_and_symbol_produce_the_same_batch_across_calls() -> None:
    """Determinism end to end (not just at the single-bar level): a golden
    fixture-style pin -- the same request always answers the same batch."""
    first = await build_qualification_recorded_history_batch(
        symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
    )
    second = await build_qualification_recorded_history_batch(
        symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
    )

    assert first == second


_GOLDEN_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "golden"
    / "qualification-recorded-history"
    / "output.json"
)


async def test_golden_batch_matches_the_committed_fixture() -> None:
    """Pins the generator's determinism (not vendor equivalence -- see
    ``tests/fixtures/golden/qualification-recorded-history/attribution.md``)
    to a committed value, so a silent behavior change in the generator is
    caught even though nothing here compares to an external reference."""
    expected = json.loads(_GOLDEN_FIXTURE_PATH.read_text(encoding="utf-8"))

    batch = await build_qualification_recorded_history_batch(
        symbol="QUALHIST", timeframe="1d", required_bar_count=5, as_of_ms=1_700_000_000_000
    )

    assert batch.model_dump(mode="json") == expected


# ---- mode toggle: healthy / slow / unavailable ------------------------------


def test_mode_defaults_to_healthy_and_rejects_an_unknown_mode() -> None:
    assert recorded_history_mode() == "healthy"
    with pytest.raises(ValueError, match="unknown recorded-history mode"):
        set_recorded_history_mode("bogus")  # type: ignore[arg-type]


async def test_unavailable_mode_raises_before_touching_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recorded,
        "fetch_complete_history_batch",
        lambda **_kwargs: pytest.fail("the walk must not run while injected-unavailable"),
    )
    set_recorded_history_mode("unavailable")

    with pytest.raises(RecordedHistoryInjectedUnavailable):
        await build_qualification_recorded_history_batch(
            symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
        )


async def test_recovery_after_clearing_unavailable_mode_returns_bars_again() -> None:
    set_recorded_history_mode("unavailable")
    with pytest.raises(RecordedHistoryInjectedUnavailable):
        await build_qualification_recorded_history_batch(
            symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
        )

    set_recorded_history_mode("healthy")
    batch = await build_qualification_recorded_history_batch(
        symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
    )

    assert len(batch.bars) >= 5


async def test_slow_mode_awaits_the_configured_delay_before_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    set_recorded_history_mode("slow")

    batch = await build_qualification_recorded_history_batch(
        symbol=_SYMBOL, timeframe="1d", required_bar_count=5, as_of_ms=_AS_OF_MS
    )

    assert slept == [recorded.SLOW_MODE_DELAY_S]
    assert len(batch.bars) >= 5

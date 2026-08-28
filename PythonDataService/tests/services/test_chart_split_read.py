"""Chart split-read: lake history, live provider tail, fallback notice.

Every boundary in these tests is derived from the canonical NYSE calendar
(``app.lean_sidecar.trading_calendar``) — no session time is written down here.
The window deliberately spans a regular session, a full holiday (Thanksgiving
2025-11-27), an early-close half-day (2025-11-28, 13:00 ET), a weekend, and the
next regular session, so the stitch is exercised against all four calendar
shapes at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath
from app.lean_sidecar.trading_calendar import (
    session_close_ms_utc,
    session_open_ms_utc,
    session_window_for_date,
    session_windows_ms_utc,
)
from app.services import chart_bar_source, chart_service
from app.services.chart_bar_source import (
    _MAX_PROVIDER_RUNS,
    compose_chart_bars,
    split_sessions_at_boundary,
)
from app.services.dataset_service import CanonicalBarsError

_ET = ZoneInfo("America/New_York")
_SYMBOL = "SPY"

# The five calendar shapes under test.
REGULAR_BEFORE = date(2025, 11, 26)  # regular session
HOLIDAY = date(2025, 11, 27)  # Thanksgiving — not a session
HALF_DAY = date(2025, 11, 28)  # early close, 13:00 ET
LIVE_SESSION = date(2025, 12, 1)  # Monday after the weekend

# A warmup start well before the requested range — the chart service always
# extends the fetch window backwards when indicators need lookback.
WARMUP_FROM = date(2025, 11, 17)

# Two clean weeks, used to build a lake full of holes.
HOLEY_START = date(2025, 11, 3)
HOLEY_END = date(2025, 11, 14)


# ──────────────────────────────────────────────
# Deterministic bar fixtures
# ──────────────────────────────────────────────
def _session_rows(session_date: date) -> list[tuple[int, float, float, float, float, float]]:
    """One row per scheduled minute of ``session_date``.

    Prices are built in ``Decimal`` at two decimal places so they survive the
    LEAN deci-cent round-trip exactly: the same minute read from a lake zip and
    fetched from the provider must be bit-identical, otherwise the flag-on /
    flag-off equality assertions below would be measuring float noise.
    """
    window = session_window_for_date(session_date)
    rows: list[tuple[int, float, float, float, float, float]] = []
    for index, ts in enumerate(range(window.open_ms_utc, window.close_ms_utc, 60_000)):
        base = Decimal("100.00") + Decimal(index) * Decimal("0.01")
        rows.append(
            (
                ts,
                float(base),
                float(base + Decimal("0.02")),
                float(base - Decimal("0.02")),
                float(base + Decimal("0.01")),
                float(1_000 + index),
            )
        )
    return rows


def _expected_minutes(sessions: Sequence[date]) -> list[int]:
    """Every scheduled bar-start in ``sessions``, straight from the calendar."""
    return [
        ts
        for session_date in sessions
        for ts in range(session_open_ms_utc(session_date), session_close_ms_utc(session_date), 60_000)
    ]


def _write_lake_day(lake_root: Path, session_date: date) -> None:
    bars = [
        MinuteTradeBar(
            bar_start_et=datetime.fromtimestamp(ts / 1000, tz=UTC).astimezone(_ET),
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(low)),
            close=Decimal(str(c)),
            volume=int(v),
        )
        for ts, o, h, low, c, v in _session_rows(session_date)
    ]
    payload = build_minute_trade_zip_bytes(_SYMBOL, session_date.strftime("%Y%m%d"), bars)
    relative = LeanMinuteBarPath(
        market="usa",
        symbol=_SYMBOL,
        trading_date=session_date,
        data_type="trade",
    ).relative_path()
    path = lake_root / Path(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _provider_rows(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """The bars the provider would return for an inclusive ISO day range."""
    return [
        {
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "volume": v,
            # The provider carries two columns the lake has no counterpart
            # for; keeping them here proves the mixed frame still resamples
            # to the same response.
            "vwap": c,
            "transactions": 10,
        }
        for window in session_windows_ms_utc(date.fromisoformat(from_date), date.fromisoformat(to_date))
        for ts, o, h, low, c, v in _session_rows(window.session_date)
    ]


class _ProviderSpy:
    """Stands in for the per-request provider fetch and records its ranges."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, from_date: str, to_date: str) -> list[dict[str, Any]]:
        self.calls.append((from_date, to_date))
        return _provider_rows(from_date, to_date)


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


@pytest.fixture(autouse=True)
def _clear_chart_caches() -> None:
    """The chart's two in-process caches outlive a test; start every one cold."""
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()


def _during(session_date: date) -> int:
    """A ``now_ms`` inside ``session_date`` — one minute after its open."""
    return session_open_ms_utc(session_date) + 60_000


def _holey_sessions() -> list[date]:
    """The ten scheduled sessions of the two-week hole-testing window."""
    return [window.session_date for window in session_windows_ms_utc(HOLEY_START, HOLEY_END)]


def _lake_coverage_with_holes(hole_run_count: int) -> list[date]:
    """Sessions to seed so the completed range has ``hole_run_count`` holes.

    Holes go at odd indices so no two are adjacent — each is its own contiguous
    provider run. The last session is live and always provider-served, so the
    total provider-run count is ``hole_run_count + 1``. Derived from
    ``_MAX_PROVIDER_RUNS`` by the callers rather than hardcoded, so raising the
    cap moves both scenarios with it.
    """
    sessions = _holey_sessions()
    completed = sessions[:-1]
    holes = {completed[2 * index + 1] for index in range(hole_run_count)}
    return [session_date for session_date in completed if session_date not in holes]


def _compose_holey(lake_root: Path, held_sessions: Sequence[date]) -> tuple[Any, _ProviderSpy]:
    """Compose the two-week window with only ``held_sessions`` in the lake.

    The final session is live, so it is always provider-served; the holes among
    the nine completed sessions are what drives the provider-run count.
    """
    for session_date in held_sessions:
        _write_lake_day(lake_root, session_date)
    provider = _ProviderSpy()
    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=HOLEY_START.isoformat(),
        to_date=HOLEY_END.isoformat(),
        adjusted=False,
        fetch_provider=provider,
        now_ms=_during(_holey_sessions()[-1]),
        lake_root=lake_root,
    )
    return composed, provider


# ──────────────────────────────────────────────
# The calendar boundary
# ──────────────────────────────────────────────
def test_split_sessions_at_boundary_completes_a_half_day_at_its_early_close() -> None:
    """13:05 ET on the half-day completes it; the same clock does not on a
    regular session. Both facts come from the calendar, not from a literal."""
    five_past_early_close = session_close_ms_utc(HALF_DAY) + 5 * 60_000

    completed, live, boundary_ms = split_sessions_at_boundary(
        REGULAR_BEFORE.isoformat(), LIVE_SESSION.isoformat(), five_past_early_close
    )

    assert [window.session_date for window in completed] == [REGULAR_BEFORE, HALF_DAY]
    assert [window.session_date for window in live] == [LIVE_SESSION]
    assert boundary_ms == session_open_ms_utc(LIVE_SESSION)


def test_split_sessions_at_boundary_keeps_a_regular_session_live_past_the_early_close_clock() -> None:
    same_wall_clock_on_regular_day = session_open_ms_utc(REGULAR_BEFORE) + (
        session_close_ms_utc(HALF_DAY) - session_open_ms_utc(HALF_DAY)
    )

    completed, live, boundary_ms = split_sessions_at_boundary(
        REGULAR_BEFORE.isoformat(), LIVE_SESSION.isoformat(), same_wall_clock_on_regular_day
    )

    assert completed == []
    assert [window.session_date for window in live] == [REGULAR_BEFORE, HALF_DAY, LIVE_SESSION]
    assert boundary_ms == session_open_ms_utc(REGULAR_BEFORE)


def test_split_sessions_at_boundary_has_no_live_tail_once_every_session_closed() -> None:
    completed, live, boundary_ms = split_sessions_at_boundary(
        REGULAR_BEFORE.isoformat(),
        HALF_DAY.isoformat(),
        session_close_ms_utc(HALF_DAY),
    )

    assert [window.session_date for window in completed] == [REGULAR_BEFORE, HALF_DAY]
    assert live == []
    assert boundary_ms is None


# ──────────────────────────────────────────────
# The stitch
# ──────────────────────────────────────────────
def test_compose_chart_bars_stitches_lake_history_to_the_live_tail(lake_root: Path) -> None:
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)
    provider = _ProviderSpy()

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=provider,
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    # Only the current session touched the provider.
    assert provider.calls == [(LIVE_SESSION.isoformat(), LIVE_SESSION.isoformat())]
    assert [(span.source, span.reason) for span in composed.spans] == [
        ("lake", "completed_sessions"),
        ("provider", "current_session"),
    ]
    assert composed.notice_code is None
    assert composed.boundary_ms_utc == session_open_ms_utc(LIVE_SESSION)


def test_compose_chart_bars_produces_no_gap_and_no_duplicate_at_the_boundary(lake_root: Path) -> None:
    """The stitched minute set is exactly the calendar's, over all three
    sessions: a missing minute or a repeated one would break this equality."""
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=_ProviderSpy(),
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    timestamps = [bar["timestamp"] for bar in composed.bars]
    assert timestamps == _expected_minutes([REGULAR_BEFORE, HALF_DAY, LIVE_SESSION])
    assert len(set(timestamps)) == len(timestamps)


def test_compose_chart_bars_holds_the_half_day_to_its_calendar_close(lake_root: Path) -> None:
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=_ProviderSpy(),
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    half_day_open = session_open_ms_utc(HALF_DAY)
    half_day_close = session_close_ms_utc(HALF_DAY)
    half_day_bars = [b for b in composed.bars if half_day_open <= b["timestamp"] < session_open_ms_utc(LIVE_SESSION)]

    assert len(half_day_bars) == (half_day_close - half_day_open) // 60_000
    assert max(b["timestamp"] for b in half_day_bars) == half_day_close - 60_000
    # The holiday between the two lake sessions contributes nothing.
    assert not [b for b in composed.bars if session_open_ms_utc(HOLIDAY) <= b["timestamp"] < half_day_open]


def test_compose_chart_bars_transitions_straight_from_last_lake_bar_to_session_open(lake_root: Path) -> None:
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=_ProviderSpy(),
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    boundary_ms = session_open_ms_utc(LIVE_SESSION)
    history = [b["timestamp"] for b in composed.bars if b["timestamp"] < boundary_ms]
    tail = [b["timestamp"] for b in composed.bars if b["timestamp"] >= boundary_ms]

    assert history[-1] == session_close_ms_utc(HALF_DAY) - 60_000
    assert tail[0] == boundary_ms
    assert len(history) == composed.spans[0].bar_count
    assert len(tail) == composed.spans[1].bar_count


# ──────────────────────────────────────────────
# Fallback and the source indicator
# ──────────────────────────────────────────────
def test_compose_chart_bars_falls_back_to_the_provider_for_a_missing_lake_day(lake_root: Path) -> None:
    _write_lake_day(lake_root, REGULAR_BEFORE)  # HALF_DAY is deliberately absent
    provider = _ProviderSpy()

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=provider,
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    assert [(span.source, span.reason) for span in composed.spans] == [
        ("lake", "completed_sessions"),
        ("provider", "lake_gap"),
        ("provider", "current_session"),
    ]
    assert provider.calls == [
        (HALF_DAY.isoformat(), HALF_DAY.isoformat()),
        (LIVE_SESSION.isoformat(), LIVE_SESSION.isoformat()),
    ]
    assert composed.notice_code == "history_provider_fallback"
    # The fallback is invisible to the series itself: still one clean stream.
    assert [b["timestamp"] for b in composed.bars] == _expected_minutes([REGULAR_BEFORE, HALF_DAY, LIVE_SESSION])


def test_compose_chart_bars_serves_an_empty_lake_entirely_from_the_provider(lake_root: Path) -> None:
    provider = _ProviderSpy()

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=provider,
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    # An empty lake has nothing to stitch, so composing costs exactly what the
    # flag-off path costs: one fetch over the whole window, not one per segment.
    assert provider.calls == [(REGULAR_BEFORE.isoformat(), LIVE_SESSION.isoformat())]
    assert [(span.source, span.reason) for span in composed.spans] == [("provider", "lake_gap")]
    assert composed.notice_code == "history_provider_fallback"


def test_compose_chart_bars_never_reads_the_raw_lake_for_an_adjusted_request(lake_root: Path) -> None:
    """The lake stores unadjusted bytes only; an adjusted chart must not be
    served from it, and must say why."""
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)
    provider = _ProviderSpy()

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=True,
        fetch_provider=provider,
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    # The raw-only lake can serve nothing here, so this is the flag-off path
    # exactly: one fetch, one span, no stitch.
    assert provider.calls == [(REGULAR_BEFORE.isoformat(), LIVE_SESSION.isoformat())]
    assert {span.source for span in composed.spans} == {"provider"}
    assert [span.reason for span in composed.spans] == ["price_adjustment_unsupported"]
    assert composed.notice_code == "adjusted_prices_provider_only"


@pytest.mark.parametrize(
    "unaddressable_ticker",
    [
        pytest.param("I:SPX", id="index_prefix"),
        pytest.param("../../etc/passwd", id="path_traversal"),
    ],
)
def test_compose_chart_bars_serves_a_lake_unaddressable_symbol_from_the_provider(
    lake_root: Path, unaddressable_ticker: str
) -> None:
    """A ticker the lake cannot address never reaches a filesystem join, and
    gets the identical single provider fetch the flag-off path would make."""
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)
    provider = _ProviderSpy()

    composed = compose_chart_bars(
        ticker=unaddressable_ticker,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=provider,
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    # One call over the whole requested range — not the composed split — so the
    # flag-on answer is the flag-off answer for the same input.
    assert provider.calls == [(REGULAR_BEFORE.isoformat(), LIVE_SESSION.isoformat())]
    assert [(span.source, span.reason) for span in composed.spans] == [
        ("provider", "symbol_not_lake_addressable"),
    ]
    assert composed.notice_code == "symbol_provider_only"


# ──────────────────────────────────────────────
# The provider fan-out cap
# ──────────────────────────────────────────────
def test_compose_chart_bars_collapses_a_holey_lake_to_one_provider_fetch(lake_root: Path) -> None:
    """Past the cap, stitching costs more provider calls than not stitching.

    One more hole run than the cap allows (plus the live tail) collapses the
    composition to the single whole-window fetch the flag-off path would have
    made.
    """
    sessions = _holey_sessions()
    composed, provider = _compose_holey(lake_root, _lake_coverage_with_holes(_MAX_PROVIDER_RUNS))

    assert provider.calls == [(HOLEY_START.isoformat(), HOLEY_END.isoformat())]
    assert [(span.source, span.reason) for span in composed.spans] == [("provider", "lake_gap")]
    assert composed.spans[0].session_count == len(sessions)
    assert composed.notice_code == "history_provider_fallback"
    # Collapsing changes where the bars came from, never which bars they are.
    assert [bar["timestamp"] for bar in composed.bars] == _expected_minutes(sessions)


def test_compose_chart_bars_still_stitches_at_the_provider_run_cap(lake_root: Path) -> None:
    """Exactly at the cap the composition still reads the lake — the collapse is
    for windows that exceed it, not windows that reach it."""
    sessions = _holey_sessions()

    composed, provider = _compose_holey(lake_root, _lake_coverage_with_holes(_MAX_PROVIDER_RUNS - 1))

    # One hole run short of the cap, plus the live tail: the cap exactly.
    assert len(provider.calls) == _MAX_PROVIDER_RUNS
    assert [span.source for span in composed.spans] == [
        "lake",
        "provider",
        "lake",
        "provider",
        "lake",
        "provider",
    ]
    assert [bar["timestamp"] for bar in composed.bars] == _expected_minutes(sessions)


# ──────────────────────────────────────────────
# The receipt covers the visible window, not the warmup
# ──────────────────────────────────────────────
def test_compose_chart_bars_reports_only_the_visible_window(lake_root: Path) -> None:
    """Warmup-only provider days must not raise a notice about bars nobody sees.

    At the lake's leading edge this is the common case: warmup reaches back
    before the backfill, so without the cut a fully lake-backed visible chart
    would show the fallback notice on every load.
    """
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)
    provider = _ProviderSpy()

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=WARMUP_FROM.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        visible_from_date=REGULAR_BEFORE.isoformat(),
        adjusted=False,
        fetch_provider=provider,
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    assert composed.notice_code is None
    assert [(span.source, span.reason) for span in composed.spans] == [
        ("lake", "completed_sessions"),
        ("provider", "current_session"),
    ]
    assert composed.spans[0].from_session_open_ms_utc == session_open_ms_utc(REGULAR_BEFORE)
    assert composed.spans[0].session_count == 2
    assert composed.spans[0].bar_count == len(_expected_minutes([REGULAR_BEFORE, HALF_DAY]))
    # The warmup bars are still in the series — only the receipt is cut.
    assert min(bar["timestamp"] for bar in composed.bars) < session_open_ms_utc(REGULAR_BEFORE)


def test_compose_chart_bars_still_reports_a_hole_inside_the_visible_window(lake_root: Path) -> None:
    """The cut hides warmup, not real gaps the operator is looking at."""
    _write_lake_day(lake_root, REGULAR_BEFORE)  # HALF_DAY is visible and missing

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=WARMUP_FROM.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        visible_from_date=REGULAR_BEFORE.isoformat(),
        adjusted=False,
        fetch_provider=_ProviderSpy(),
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    assert composed.notice_code == "history_provider_fallback"
    assert [(span.source, span.reason) for span in composed.spans] == [
        ("lake", "completed_sessions"),
        ("provider", "lake_gap"),
        ("provider", "current_session"),
    ]


def test_compose_chart_bars_rejects_a_provider_range_that_overlaps_the_lake(lake_root: Path) -> None:
    """A stitch that would duplicate a bar must fail loudly, never repair itself."""
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)

    def over_returning_provider(_from_date: str, _to_date: str) -> list[dict[str, Any]]:
        """Re-serves the half-day the lake already covered."""
        return _provider_rows(HALF_DAY.isoformat(), LIVE_SESSION.isoformat())

    with pytest.raises(CanonicalBarsError, match="duplicate timestamp"):
        compose_chart_bars(
            ticker=_SYMBOL,
            from_date=REGULAR_BEFORE.isoformat(),
            to_date=LIVE_SESSION.isoformat(),
            adjusted=False,
            fetch_provider=over_returning_provider,
            now_ms=_during(LIVE_SESSION),
            lake_root=lake_root,
        )


def test_compose_chart_bars_spans_report_session_anchored_ms_boundaries(lake_root: Path) -> None:
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)

    composed = compose_chart_bars(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        adjusted=False,
        fetch_provider=_ProviderSpy(),
        now_ms=_during(LIVE_SESSION),
        lake_root=lake_root,
    )

    wire = composed.as_response_dict()

    assert wire["notice_code"] is None
    assert wire["boundary_ms_utc"] == session_open_ms_utc(LIVE_SESSION)
    assert wire["spans"][0] == {
        "source": "lake",
        "reason": "completed_sessions",
        "from_session_open_ms_utc": session_open_ms_utc(REGULAR_BEFORE),
        "to_session_open_ms_utc": session_open_ms_utc(HALF_DAY),
        "session_count": 2,
        "bar_count": len(_expected_minutes([REGULAR_BEFORE, HALF_DAY])),
    }
    # Every temporal value on the wire is an int64 ms UTC anchor, never a date.
    for span in wire["spans"]:
        assert isinstance(span["from_session_open_ms_utc"], int)
        assert isinstance(span["to_session_open_ms_utc"], int)


# ──────────────────────────────────────────────
# End-to-end through get_chart_data
# ──────────────────────────────────────────────
def _run_chart() -> dict[str, Any]:
    return chart_service.get_chart_data(
        ticker=_SYMBOL,
        from_date=REGULAR_BEFORE.isoformat(),
        to_date=LIVE_SESSION.isoformat(),
        timeframe="15m",
        session="rth",
        indicators=[{"name": "ema", "params": {"length": 5}}],
        adjusted=False,
    )


@pytest.fixture
def chart_provider(monkeypatch: pytest.MonkeyPatch) -> _ProviderSpy:
    """Replace the chart service's provider fetch — both the flag-off path and
    the composed path go through this one seam."""
    provider = _ProviderSpy()
    monkeypatch.setattr(
        chart_service,
        "fetch_bars_chunked",
        lambda _client, _ticker, from_date, to_date, adjusted=True: provider(from_date, to_date),
    )
    return provider


def test_get_chart_data_omits_bar_sources_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch, chart_provider: _ProviderSpy
) -> None:
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)

    result = _run_chart()

    assert "bar_sources" not in result
    assert result["bars"]


def test_get_chart_data_flag_on_matches_flag_off_bars_and_indicators(
    monkeypatch: pytest.MonkeyPatch, chart_provider: _ProviderSpy, lake_root: Path
) -> None:
    """Resampling and indicator outputs are identical for identical inputs.

    The lake holds the same minutes the provider would have served, so the only
    legitimate difference between the two responses is the additive source
    indicator.
    """
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)
    flag_off = _run_chart()

    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    monkeypatch.setattr(chart_bar_source, "resolve_lake_root", lambda: lake_root)
    monkeypatch.setattr(chart_bar_source, "now_ms_utc", lambda: _during(LIVE_SESSION))
    flag_on = _run_chart()

    # Warmup pulled the fetch window back before the requested range and those
    # sessions came from the provider, but the operator cannot see them — so the
    # receipt stays quiet. Every *visible* session is lake-backed.
    assert flag_on.pop("bar_sources")["notice_code"] is None
    assert flag_on["bars"] == flag_off["bars"]
    assert flag_on["indicators"] == flag_off["indicators"]
    assert flag_on == flag_off


def test_get_chart_data_carries_the_source_indicator_when_history_is_lake_backed(
    monkeypatch: pytest.MonkeyPatch, chart_provider: _ProviderSpy, lake_root: Path
) -> None:
    for window in session_windows_ms_utc(date(2025, 10, 1), HALF_DAY):
        _write_lake_day(lake_root, window.session_date)
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    monkeypatch.setattr(chart_bar_source, "resolve_lake_root", lambda: lake_root)
    monkeypatch.setattr(chart_bar_source, "now_ms_utc", lambda: _during(LIVE_SESSION))

    result = _run_chart()

    assert result["bar_sources"]["notice_code"] is None
    assert result["bar_sources"]["boundary_ms_utc"] == session_open_ms_utc(LIVE_SESSION)
    assert [span["source"] for span in result["bar_sources"]["spans"]] == ["lake", "provider"]
    # Warmup pulled the fetch window back before the requested range, and every
    # one of those sessions came out of the lake: the provider saw the current
    # session only.
    assert chart_provider.calls == [(LIVE_SESSION.isoformat(), LIVE_SESSION.isoformat())]

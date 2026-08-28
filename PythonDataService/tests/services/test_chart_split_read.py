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
from app.services.chart_bar_source import compose_chart_bars, split_sessions_at_boundary
from app.services.dataset_service import CanonicalBarsError

_ET = ZoneInfo("America/New_York")
_SYMBOL = "SPY"

# The five calendar shapes under test.
REGULAR_BEFORE = date(2025, 11, 26)  # regular session
HOLIDAY = date(2025, 11, 27)  # Thanksgiving — not a session
HALF_DAY = date(2025, 11, 28)  # early close, 13:00 ET
LIVE_SESSION = date(2025, 12, 1)  # Monday after the weekend


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


class _ProviderSpy:
    """Stands in for the per-request provider fetch and records its ranges."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, from_date: str, to_date: str) -> list[dict[str, Any]]:
        self.calls.append((from_date, to_date))
        out: list[dict[str, Any]] = []
        for window in session_windows_ms_utc(date.fromisoformat(from_date), date.fromisoformat(to_date)):
            for ts, o, h, low, c, v in _session_rows(window.session_date):
                out.append(
                    {
                        "timestamp": ts,
                        "open": o,
                        "high": h,
                        "low": low,
                        "close": c,
                        "volume": v,
                        # The provider carries two columns the lake has no
                        # counterpart for; keeping them here proves the mixed
                        # frame still resamples to the same response.
                        "vwap": c,
                        "transactions": 10,
                    }
                )
        return out


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


def _during(session_date: date) -> int:
    """A ``now_ms`` inside ``session_date`` — one minute after its open."""
    return session_open_ms_utc(session_date) + 60_000


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

    assert provider.calls == [
        (REGULAR_BEFORE.isoformat(), HALF_DAY.isoformat()),
        (LIVE_SESSION.isoformat(), LIVE_SESSION.isoformat()),
    ]
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

    assert {span.source for span in composed.spans} == {"provider"}
    assert [span.reason for span in composed.spans] == ["price_adjustment_unsupported", "current_session"]
    assert composed.notice_code == "adjusted_prices_provider_only"


def test_compose_chart_bars_rejects_a_provider_range_that_overlaps_the_lake(lake_root: Path) -> None:
    """A stitch that would duplicate a bar must fail loudly, never repair itself."""
    _write_lake_day(lake_root, REGULAR_BEFORE)
    _write_lake_day(lake_root, HALF_DAY)

    def over_returning_provider(_from_date: str, _to_date: str) -> list[dict[str, Any]]:
        return _ProviderSpy()(HALF_DAY.isoformat(), LIVE_SESSION.isoformat())

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
@pytest.fixture(autouse=True)
def _clear_chart_caches() -> None:
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()


def _run_chart(**overrides: Any) -> dict[str, Any]:
    request = {
        "ticker": _SYMBOL,
        "from_date": REGULAR_BEFORE.isoformat(),
        "to_date": LIVE_SESSION.isoformat(),
        "timeframe": "15m",
        "session": "rth",
        "indicators": [{"name": "ema", "params": {"length": 5}}],
        "adjusted": False,
    }
    request.update(overrides)
    return chart_service.get_chart_data(**request)


@pytest.fixture
def _provider_backed(monkeypatch: pytest.MonkeyPatch) -> _ProviderSpy:
    provider = _ProviderSpy()
    monkeypatch.setattr(
        chart_service,
        "fetch_bars_chunked",
        lambda _client, _ticker, from_date, to_date, adjusted=True: provider(from_date, to_date),
    )
    return provider


def test_get_chart_data_omits_bar_sources_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch, _provider_backed: _ProviderSpy
) -> None:
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)

    result = _run_chart()

    assert "bar_sources" not in result
    assert result["bars"]


def test_get_chart_data_flag_on_matches_flag_off_bars_and_indicators(
    monkeypatch: pytest.MonkeyPatch, _provider_backed: _ProviderSpy, lake_root: Path
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

    assert flag_on.pop("bar_sources")["notice_code"] == "history_provider_fallback"
    assert flag_on["bars"] == flag_off["bars"]
    assert flag_on["indicators"] == flag_off["indicators"]
    assert flag_on == flag_off


def test_get_chart_data_carries_the_source_indicator_when_history_is_lake_backed(
    monkeypatch: pytest.MonkeyPatch, _provider_backed: _ProviderSpy, lake_root: Path
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
    assert _provider_backed.calls == [(LIVE_SESSION.isoformat(), LIVE_SESSION.isoformat())]

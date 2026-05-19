"""Unit tests for app.lean_sidecar.polygon_canonical."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.polygon_canonical import (
    FixtureMetadataMismatchError,
    RecordedPolygonFixtureProvider,
)


def _write_fixture(
    tmp_path: Path,
    *,
    symbol: str = "SPY",
    from_date: str = "2025-01-06",
    to_date: str = "2025-01-10",
    bars: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a minimal fixture directory and return its path."""
    fixture_dir = tmp_path / f"{symbol.lower()}_minute_{from_date}_{to_date}"
    fixture_dir.mkdir()
    bars = (
        bars
        if bars is not None
        else [
            {"timestamp": 1736175600000, "open": 591.0, "high": 591.5, "low": 590.5, "close": 591.2, "volume": 1000},
        ]
    )
    (fixture_dir / "bars.json").write_text(json.dumps(bars))
    (fixture_dir / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "symbol": symbol,
                "from_date": from_date,
                "to_date": to_date,
                "timespan": "minute",
                "multiplier": 1,
                "adjusted": False,
                "session_prefilter": "none",
                "bar_count": len(bars),
                "fetched_at_ms_utc": 1737432000000,
                "polygon_sdk_version": "1.12.5",
                "bars_sha256": "0" * 64,
                "observed_trade_count": 1,
                "observed_first_entry_ms_utc": 1736178300000,
                "observed_first_exit_ms_utc": 1736182800000,
            }
        )
    )
    return fixture_dir


def test_recorded_provider_returns_bars_when_metadata_matches(tmp_path: Path) -> None:
    fixture_dir = _write_fixture(tmp_path)
    provider = RecordedPolygonFixtureProvider(fixture_dir)

    bars = provider.fetch_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 10),
        adjusted=False,
    )

    assert len(bars) == 1
    assert bars[0]["close"] == 591.2


@pytest.mark.parametrize(
    "field,bad_value,asked",
    [
        ("symbol", "QQQ", {"symbol": "SPY"}),
        ("from_date", "2025-02-01", {"from_date": "2025-01-06"}),
        ("to_date", "2025-02-05", {"to_date": "2025-01-10"}),
        ("adjusted", True, {"adjusted": False}),
    ],
)
def test_recorded_provider_rejects_metadata_mismatch(
    tmp_path: Path, field: str, bad_value: object, asked: dict[str, object]
) -> None:
    """Test does not silently load wrong bars when request drifts from fixture shape."""
    fixture_dir = _write_fixture(tmp_path)
    # Mutate fixture metadata to introduce the mismatch.
    meta = json.loads((fixture_dir / "metadata.json").read_text())
    meta[field] = bad_value
    (fixture_dir / "metadata.json").write_text(json.dumps(meta))

    provider = RecordedPolygonFixtureProvider(fixture_dir)
    with pytest.raises(FixtureMetadataMismatchError, match=field):
        provider.fetch_minute_bars(
            symbol=asked.get("symbol", "SPY"),
            start_date=date.fromisoformat(asked.get("from_date", "2025-01-06")),
            end_date=date.fromisoformat(asked.get("to_date", "2025-01-10")),
            adjusted=asked.get("adjusted", False),
        )


def test_recorded_provider_rejects_unknown_schema_version(tmp_path: Path) -> None:
    fixture_dir = _write_fixture(tmp_path)
    meta = json.loads((fixture_dir / "metadata.json").read_text())
    meta["schema_version"] = 2  # future bump the provider doesn't know
    (fixture_dir / "metadata.json").write_text(json.dumps(meta))

    provider = RecordedPolygonFixtureProvider(fixture_dir)
    with pytest.raises(FixtureMetadataMismatchError, match="schema_version"):
        provider.fetch_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 10),
            adjusted=False,
        )


def test_polygon_provider_delegates_to_fetch_bars_chunked(monkeypatch) -> None:
    from app.lean_sidecar import polygon_canonical

    fake_polygon = MagicMock()
    fake_bars = [{"timestamp": 1736175600000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}]
    called_with: dict[str, object] = {}

    def fake_fetch(polygon, ticker, from_date, to_date, timespan, multiplier, adjusted, **_):
        called_with.update(
            ticker=ticker,
            from_date=from_date,
            to_date=to_date,
            timespan=timespan,
            multiplier=multiplier,
            adjusted=adjusted,
        )
        return fake_bars

    monkeypatch.setattr(polygon_canonical, "fetch_bars_chunked", fake_fetch)

    provider = polygon_canonical.PolygonProvider(polygon=fake_polygon)
    out = provider.fetch_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 10),
        adjusted=False,
    )

    assert out is fake_bars
    assert called_with == {
        "ticker": "SPY",
        "from_date": "2025-01-06",
        "to_date": "2025-01-10",
        "timespan": "minute",
        "multiplier": 1,
        "adjusted": False,
    }


def test_get_default_provider_returns_polygon_provider() -> None:
    from app.lean_sidecar.polygon_canonical import (
        PolygonProvider,
        get_default_provider,
    )

    provider = get_default_provider()
    assert isinstance(provider, PolygonProvider)


# ---------------------------------------------------------------------------
# Task 3: fetch_canonical_minute_bars tests
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")


def _bar(et_dt: datetime, close: float = 100.0) -> dict:
    """Build a Polygon-style dict with a ms-UTC timestamp at start-of-bar."""
    ts_ms = int(et_dt.astimezone(ZoneInfo("UTC")).timestamp() * 1000)
    return {
        "timestamp": ts_ms,
        "open": close - 0.05,
        "high": close + 0.05,
        "low": close - 0.10,
        "close": close,
        "volume": 1000,
    }


class _StubProvider:
    def __init__(self, bars: list[dict]) -> None:
        self._bars = bars

    def fetch_minute_bars(self, *, symbol, start_date, end_date, adjusted) -> list[dict]:
        return self._bars


def test_fetch_canonical_minute_bars_filters_to_rth() -> None:
    from app.lean_sidecar.polygon_canonical import fetch_canonical_minute_bars

    # 09:25 (pre-market), 09:30 (first RTH), 15:59 (last RTH), 16:00 (post-market)
    day = datetime(2025, 1, 6, tzinfo=_ET)
    pre = day.replace(hour=9, minute=25)
    open_min = day.replace(hour=9, minute=30)
    last_min = day.replace(hour=15, minute=59)
    post = day.replace(hour=16, minute=0)
    bars = [
        _bar(pre, 590.0),
        _bar(open_min, 591.0),
        _bar(last_min, 592.0),
        _bar(post, 593.0),
    ]

    out = fetch_canonical_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 6),
        session="regular",
        adjustment="raw",
        provider=_StubProvider(bars),
    )

    # One trading day with two RTH bars (09:30, 15:59).
    assert len(out) == 1
    trading_date, trade_bars = out[0]
    assert trading_date == date(2025, 1, 6)
    assert [float(b.close) for b in trade_bars] == [591.0, 592.0]


def test_fetch_canonical_rejects_duplicate_timestamps() -> None:
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        fetch_canonical_minute_bars,
    )

    day = datetime(2025, 1, 6, 10, 0, tzinfo=_ET)
    bars = [_bar(day, 591.0), _bar(day, 591.5)]  # same timestamp

    with pytest.raises(CanonicalBarsError, match="duplicate"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 6),
            session="regular",
            adjustment="raw",
            provider=_StubProvider(bars),
        )


def test_fetch_canonical_rejects_non_monotonic_timestamps() -> None:
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        fetch_canonical_minute_bars,
    )

    day = datetime(2025, 1, 6, 10, 0, tzinfo=_ET)
    bars = [_bar(day, 591.0), _bar(day - timedelta(minutes=1), 590.5)]  # out of order

    with pytest.raises(CanonicalBarsError, match="non-monotonic"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 6),
            session="regular",
            adjustment="raw",
            provider=_StubProvider(bars),
        )


def test_fetch_canonical_keeps_all_when_extended() -> None:
    from app.lean_sidecar.polygon_canonical import fetch_canonical_minute_bars

    day = datetime(2025, 1, 6, tzinfo=_ET)
    pre = day.replace(hour=9, minute=25)
    open_min = day.replace(hour=9, minute=30)
    post = day.replace(hour=16, minute=0)
    bars = [_bar(pre, 590.0), _bar(open_min, 591.0), _bar(post, 593.0)]

    out = fetch_canonical_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 6),
        session="extended",
        adjustment="raw",
        provider=_StubProvider(bars),
    )

    _trading_date, trade_bars = out[0]
    assert len(trade_bars) == 3

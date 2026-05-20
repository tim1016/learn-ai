"""Unit tests for app.lean_sidecar.polygon_canonical."""

from __future__ import annotations

import hashlib
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
    """Write a minimal fixture directory and return its path.

    bars.json is written first and its sha256 is computed over the
    bytes; metadata.json carries that sha so the provider's
    fetch-time sha check passes by default. Tests that need to
    induce a sha mismatch can flip a byte in bars.json after this
    helper returns.
    """
    fixture_dir = tmp_path / f"{symbol.lower()}_minute_{from_date}_{to_date}"
    fixture_dir.mkdir()
    bars = (
        bars
        if bars is not None
        else [
            {"timestamp": 1736175600000, "open": 591.0, "high": 591.5, "low": 590.5, "close": 591.2, "volume": 1000},
        ]
    )
    bars_bytes = json.dumps(bars).encode("utf-8")
    (fixture_dir / "bars.json").write_bytes(bars_bytes)
    bars_sha = hashlib.sha256(bars_bytes).hexdigest()
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
                "bars_sha256": bars_sha,
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

    # Schema validation runs at construction so an unknown metadata
    # version cannot reach the bar-fetch path.
    with pytest.raises(FixtureMetadataMismatchError, match="schema_version"):
        RecordedPolygonFixtureProvider(fixture_dir)


def test_recorded_provider_exposes_fixture_identity(tmp_path: Path) -> None:
    """provider_kind threading: fixture_id is the dir name and fixture_sha256 is metadata's bars_sha256."""
    fixture_dir = _write_fixture(tmp_path)
    provider = RecordedPolygonFixtureProvider(fixture_dir)
    meta = json.loads((fixture_dir / "metadata.json").read_text())

    assert provider.fixture_id == fixture_dir.name
    assert provider.fixture_sha256 == meta["bars_sha256"]


def test_recorded_provider_rejects_tampered_bars_json(tmp_path: Path) -> None:
    """A single-byte edit to bars.json must fail the post-load sha check."""
    fixture_dir = _write_fixture(tmp_path)
    # Flip one byte to invalidate the sha without changing JSON validity.
    bars_path = fixture_dir / "bars.json"
    raw = bars_path.read_bytes()
    # Replace the first "0" digit we can find with "1"; safe because
    # JSON for OHLCV bars always contains a digit byte.
    idx = raw.find(b"1000")  # the volume value
    assert idx >= 0
    tampered = raw[:idx] + b"2" + raw[idx + 1 :]
    bars_path.write_bytes(tampered)

    provider = RecordedPolygonFixtureProvider(fixture_dir)
    with pytest.raises(FixtureMetadataMismatchError, match=r"bars\.json sha256"):
        provider.fetch_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 10),
            adjusted=False,
        )


def test_polygon_provider_identity_is_live(monkeypatch) -> None:
    """PolygonProvider must report live identity (None fixture fields)."""
    from app.lean_sidecar.polygon_canonical import PolygonProvider

    provider = PolygonProvider(polygon=MagicMock())
    assert provider.fixture_id is None
    assert provider.fixture_sha256 is None


def test_polygon_provider_delegates_to_raw_chunker(monkeypatch) -> None:
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

    monkeypatch.setattr(polygon_canonical, "fetch_bars_chunks_raw", fake_fetch)

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
    def __init__(self, bars: list[dict[str, Any]]) -> None:
        self._bars = bars

    def fetch_minute_bars(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
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


def test_polygon_provider_raw_path_surfaces_duplicate_to_canonical_check(monkeypatch) -> None:
    """Regression: production PolygonProvider must NOT sanitize duplicates.

    Pre-fix path was PolygonProvider → fetch_bars_chunked → silent
    dedupe + re-sort, so ``fetch_canonical_minute_bars`` never saw the
    raw wire bytes and the strict monotonicity rule from
    .claude/rules/numerical-rigor.md ran only in tests. Post-fix
    PolygonProvider → fetch_bars_chunks_raw passes duplicates through
    to the fail-fast loop.
    """
    from app.lean_sidecar import polygon_canonical
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        PolygonProvider,
        fetch_canonical_minute_bars,
    )

    day = datetime(2025, 1, 6, 10, 0, tzinfo=_ET)
    duplicate_ts_ms = int(day.astimezone(ZoneInfo("UTC")).timestamp() * 1000)
    fake_bars = [
        {
            "timestamp": duplicate_ts_ms,
            "open": 591.0,
            "high": 591.0,
            "low": 591.0,
            "close": 591.0,
            "volume": 100,
        },
        {
            "timestamp": duplicate_ts_ms,
            "open": 591.5,
            "high": 591.5,
            "low": 591.5,
            "close": 591.5,
            "volume": 100,
        },
    ]

    def fake_raw(polygon, ticker, from_date, to_date, **_kwargs):
        return list(fake_bars)

    monkeypatch.setattr(polygon_canonical, "fetch_bars_chunks_raw", fake_raw)

    provider = PolygonProvider(polygon=MagicMock())
    with pytest.raises(CanonicalBarsError, match="duplicate"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 6),
            session="extended",
            adjustment="raw",
            provider=provider,
        )


def test_polygon_provider_raw_path_surfaces_non_monotonic(monkeypatch) -> None:
    """Regression: non-monotonic Polygon output surfaces, never silently sorted."""
    from app.lean_sidecar import polygon_canonical
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        PolygonProvider,
        fetch_canonical_minute_bars,
    )

    later = datetime(2025, 1, 6, 10, 1, tzinfo=_ET)
    earlier = datetime(2025, 1, 6, 10, 0, tzinfo=_ET)
    later_ts = int(later.astimezone(ZoneInfo("UTC")).timestamp() * 1000)
    earlier_ts = int(earlier.astimezone(ZoneInfo("UTC")).timestamp() * 1000)
    fake_bars = [
        {"timestamp": later_ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        {"timestamp": earlier_ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
    ]

    def fake_raw(polygon, ticker, from_date, to_date, **_kwargs):
        return list(fake_bars)

    monkeypatch.setattr(polygon_canonical, "fetch_bars_chunks_raw", fake_raw)

    provider = PolygonProvider(polygon=MagicMock())
    with pytest.raises(CanonicalBarsError, match="non-monotonic"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 6),
            session="extended",
            adjustment="raw",
            provider=provider,
        )


def _full_session_bars(d: date, et_zone: ZoneInfo) -> list[dict]:
    """Build a minimal RTH bar list with both 09:30 and 15:59 boundaries present."""
    day_start = datetime(d.year, d.month, d.day, tzinfo=et_zone)
    return [
        _bar(day_start.replace(hour=9, minute=30), 100.0),
        _bar(day_start.replace(hour=15, minute=59), 100.5),
    ]


def test_fetch_canonical_rejects_missing_session() -> None:
    """A dropped session in the middle of the window fails fast."""
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        fetch_canonical_minute_bars,
    )

    # Window Mon-Fri Jan 13–17 2025. Stub returns bars only for 4 of 5 sessions.
    # Skip Jan 15 (Wednesday) — it is a trading day.
    bars: list[dict] = []
    for d in [date(2025, 1, 13), date(2025, 1, 14), date(2025, 1, 16), date(2025, 1, 17)]:
        bars.extend(_full_session_bars(d, _ET))

    with pytest.raises(CanonicalBarsError, match=r"polygon_window_incomplete.*2025-01-15"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 13),
            end_date=date(2025, 1, 17),
            session="regular",
            adjustment="raw",
            provider=_StubProvider(bars),
        )


def test_fetch_canonical_rejects_session_missing_first_bar() -> None:
    """A session with no 09:30 ET bar fails fast."""
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        fetch_canonical_minute_bars,
    )

    # Single trading day, but only the 15:59 bar present.
    day_start = datetime(2025, 1, 6, tzinfo=_ET)
    bars = [_bar(day_start.replace(hour=15, minute=59), 100.5)]

    with pytest.raises(CanonicalBarsError, match=r"polygon_session_incomplete.*09:30"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 6),
            session="regular",
            adjustment="raw",
            provider=_StubProvider(bars),
        )


def test_fetch_canonical_accepts_half_day_with_1259_boundary() -> None:
    """Half-day 2024-11-29 (post-Thanksgiving, 13:00 close) → last bar at 12:59 satisfies boundary."""
    from app.lean_sidecar.polygon_canonical import fetch_canonical_minute_bars

    # 2024-11-29 (Black Friday): NYSE closes at 13:00 ET.
    day_start = datetime(2024, 11, 29, tzinfo=_ET)
    bars = [
        _bar(day_start.replace(hour=9, minute=30), 100.0),
        _bar(day_start.replace(hour=12, minute=59), 100.5),
    ]

    out = fetch_canonical_minute_bars(
        symbol="SPY",
        start_date=date(2024, 11, 29),
        end_date=date(2024, 11, 29),
        session="regular",
        adjustment="raw",
        provider=_StubProvider(bars),
    )
    assert len(out) == 1
    assert out[0][0] == date(2024, 11, 29)


def test_fetch_canonical_rejects_half_day_missing_1259_bar() -> None:
    """Half-day 2024-11-29 with only 09:30 present must fail boundary check at 12:59."""
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError,
        fetch_canonical_minute_bars,
    )

    day_start = datetime(2024, 11, 29, tzinfo=_ET)
    bars = [
        _bar(day_start.replace(hour=9, minute=30), 100.0),
        # 12:00 noon bar, no 12:59 bar
        _bar(day_start.replace(hour=12, minute=0), 100.4),
    ]

    with pytest.raises(CanonicalBarsError, match=r"polygon_session_incomplete.*12:59"):
        fetch_canonical_minute_bars(
            symbol="SPY",
            start_date=date(2024, 11, 29),
            end_date=date(2024, 11, 29),
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

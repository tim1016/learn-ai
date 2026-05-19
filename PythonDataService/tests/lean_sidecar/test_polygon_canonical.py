"""Unit tests for app.lean_sidecar.polygon_canonical."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.lean_sidecar.polygon_canonical import RecordedPolygonFixtureProvider


def _write_fixture(
    tmp_path: Path,
    *,
    symbol: str = "SPY",
    from_date: str = "2025-01-06",
    to_date: str = "2025-01-10",
    bars: list[dict] | None = None,
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
    tmp_path: Path, field: str, bad_value: object, asked: dict
) -> None:
    """Test does not silently load wrong bars when request drifts from fixture shape."""
    from app.lean_sidecar.polygon_canonical import FixtureMetadataMismatchError

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

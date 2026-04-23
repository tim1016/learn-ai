"""Tests for app.research.divergence.ingest.align."""

from __future__ import annotations

import pandas as pd
import pytest

from app.research.divergence.ingest.align import align_tv_polygon


def _bars(timestamps: list[int], label: str) -> pd.DataFrame:
    """Build a minimal TV/Polygon-style frame with tz-aware `time_utc`."""
    return pd.DataFrame(
        {
            "time_utc": pd.to_datetime(timestamps, unit="ms", utc=True),
            "open": [100.0 + i for i in range(len(timestamps))],
            "high": [101.0 + i for i in range(len(timestamps))],
            "low": [99.0 + i for i in range(len(timestamps))],
            "close": [100.5 + i for i in range(len(timestamps))],
            "volume": [1000 + i for i in range(len(timestamps))],
            "source_tag": [label] * len(timestamps),
        }
    )


def test_align_requires_time_utc_on_both_sides():
    tv = pd.DataFrame({"other": [1, 2]})
    pg = _bars([1_704_067_200_000], "pg")

    with pytest.raises(ValueError):
        align_tv_polygon(tv, pg)


def test_align_inner_joins_on_time_utc():
    shared = [1_704_067_200_000, 1_704_067_260_000]
    tv = _bars([*shared, 1_704_067_320_000], "tv")
    pg = _bars([1_704_067_140_000, *shared], "pg")

    merged, summary = align_tv_polygon(tv, pg)

    assert summary["tv_rows"] == 3
    assert summary["pg_rows"] == 3
    assert summary["merged_rows"] == 2
    assert summary["tv_only_dropped"] == 1
    assert summary["pg_only_dropped"] == 1
    assert summary["coverage_pct"] == pytest.approx(66.67, abs=0.01, rel=0)
    assert set(merged["time_utc"]) == set(
        pd.to_datetime(shared, unit="ms", utc=True)
    )


def test_align_suffixes_ohlcv_columns():
    shared = [1_704_067_200_000]
    tv = _bars(shared, "tv")
    pg = _bars(shared, "pg")

    merged, _ = align_tv_polygon(tv, pg)

    for col in ("open", "high", "low", "close", "volume"):
        assert f"{col}_tv" in merged.columns
        assert f"{col}_pg" in merged.columns
        assert col not in merged.columns


def test_align_empty_overlap_returns_empty_merged():
    tv = _bars([1_704_067_200_000], "tv")
    pg = _bars([1_704_070_000_000], "pg")

    merged, summary = align_tv_polygon(tv, pg)

    assert merged.empty
    assert summary["merged_rows"] == 0
    assert summary["coverage_pct"] == pytest.approx(0.0, abs=1e-9, rel=0)
    assert summary["first_merged_utc"] == ""
    assert summary["last_merged_utc"] == ""


def test_align_drops_et_column_but_keeps_others():
    shared = [1_704_067_200_000]
    tv = _bars(shared, "tv")
    pg = _bars(shared, "pg")
    tv["et"] = "2024-01-01 04:30"
    pg["et"] = "2024-01-01 04:30"

    merged, _ = align_tv_polygon(tv, pg)

    assert "et" not in merged.columns
    # Non-OHLCV non-et columns retained (no suffix) — source_tag is such a column.
    assert "source_tag_x" in merged.columns or "source_tag" in merged.columns

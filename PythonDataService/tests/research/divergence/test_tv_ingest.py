"""Tests for app.research.divergence.ingest.tv_ingest."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.research.divergence.ingest.tv_ingest import (
    OHLCV_COLS,
    PINE_INDICATOR_COLS,
    IngestValidationError,
    ingest_tv_csv,
)


def _rth_15m_row(ts_s: int, close: float = 100.0) -> dict:
    row: dict = {
        "time": ts_s,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "Volume": 1000,
    }
    # Pine indicator columns — use benign numeric defaults; match time in ms.
    for col in PINE_INDICATOR_COLS:
        row[col] = 0.0
    row["bar_time_unix_s"] = ts_s * 1000  # Pine emits ms (misnomer); ingest tolerates.
    return row


def _make_csv(tmp_path: Path, rows: list[dict]) -> Path:
    df = pd.DataFrame(rows)
    path = tmp_path / "tv.csv"
    df.to_csv(path, index=False)
    return path


def _rth_timestamps_15m_2024_04_01_s() -> list[int]:
    """A single RTH trading day at 15-min cadence: 26 bars 09:30 ET (13:30 UTC) → 15:45 ET (19:45 UTC)."""
    # 2024-04-01 13:30 UTC = 09:30 ET (DST active).
    start_s = 1_711_978_200
    return [start_s + i * 900 for i in range(26)]


def test_ingest_tv_csv_rejects_missing_indicator_columns(tmp_path: Path):
    ts_s = _rth_timestamps_15m_2024_04_01_s()[0]
    row = {
        "time": ts_s,
        "open": 100.0,
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
        "Volume": 1,
    }
    csv_path = _make_csv(tmp_path, [row])

    with pytest.raises(IngestValidationError, match="Pine indicator columns"):
        ingest_tv_csv(csv_path, timeframe="15m")


def test_ingest_tv_csv_rejects_unsupported_timeframe(tmp_path: Path):
    rows = [_rth_15m_row(ts) for ts in _rth_timestamps_15m_2024_04_01_s()]
    csv_path = _make_csv(tmp_path, rows)

    with pytest.raises(IngestValidationError):
        ingest_tv_csv(csv_path, timeframe="3m")


def test_ingest_tv_csv_rejects_duplicate_timestamps(tmp_path: Path):
    timestamps = _rth_timestamps_15m_2024_04_01_s()
    rows = [_rth_15m_row(ts) for ts in timestamps]
    rows.append(_rth_15m_row(timestamps[0]))  # duplicate
    csv_path = _make_csv(tmp_path, rows)

    with pytest.raises(IngestValidationError, match="duplicate"):
        ingest_tv_csv(csv_path, timeframe="15m")


def test_ingest_tv_csv_rejects_non_rth_bar(tmp_path: Path):
    ts_base = _rth_timestamps_15m_2024_04_01_s()
    # Prepend a pre-market 09:00 ET bar = 13:00 UTC.
    bad_ts = ts_base[0] - 1800
    rows = [_rth_15m_row(bad_ts)] + [_rth_15m_row(ts) for ts in ts_base]
    csv_path = _make_csv(tmp_path, rows)

    with pytest.raises(IngestValidationError, match="non-RTH"):
        ingest_tv_csv(csv_path, timeframe="15m")


def test_ingest_tv_csv_happy_path_produces_manifest(tmp_path: Path):
    rows = [_rth_15m_row(ts) for ts in _rth_timestamps_15m_2024_04_01_s()]
    csv_path = _make_csv(tmp_path, rows)

    out_manifest = tmp_path / "out" / "tv.manifest.json"

    # Note: skip out_parquet — the runtime image may not have pyarrow/fastparquet.
    # The in-memory DataFrame + manifest assertions are the interesting contract;
    # parquet I/O is a trivial pandas wrapper verified separately in the ingest CLI.
    df, manifest = ingest_tv_csv(
        csv_path,
        timeframe="15m",
        out_manifest=out_manifest,
    )

    assert len(df) == 26
    assert "time_utc" in df.columns
    assert "et" in df.columns
    # Volume column normalized to lowercase for Polygon parity.
    assert "volume" in df.columns
    assert "Volume" not in df.columns

    assert manifest.rows == 26
    assert manifest.trading_days == 1
    # Single full RTH day at 15m: 26 bars is the expected count, no half day.
    assert manifest.half_days == 0

    assert out_manifest.exists()
    persisted = json.loads(out_manifest.read_text())
    assert persisted["rows"] == 26
    assert persisted["timeframe"] == "15m"


def test_ingest_tv_csv_all_ohlcv_columns_are_checked(tmp_path: Path):
    # Drop 'open' from the row and confirm the validator reports OHLCV missing.
    rows = [_rth_15m_row(ts) for ts in _rth_timestamps_15m_2024_04_01_s()]
    for r in rows:
        r.pop("open")
    csv_path = _make_csv(tmp_path, rows)

    with pytest.raises(IngestValidationError, match="OHLCV"):
        ingest_tv_csv(csv_path, timeframe="15m")

    # Sanity: OHLCV_COLS still advertises `open`.
    assert "open" in OHLCV_COLS

"""Polygon-side ingestor for the divergence study.

Two ingest paths are supported because they have different latencies:

1. ``ingest_polygon_aggregates`` — live fetch via the existing
   ``PolygonClientService``. Used in production / for the FastAPI endpoint.
   Slow (rate-limited) but always-current.

2. ``ingest_polygon_1min_csv_resampled`` — read a pre-fetched 1-minute
   Polygon CSV from disk and resample to the requested timeframe.
   Used for development and for repeatable runs against a frozen dataset.

Both paths produce DataFrames with the same canonical schema:

    columns = [unix_ts (ms), iso_time, time_utc, et,
               open, high, low, close, volume, vwap, transactions]

with ``unix_ts`` as integer milliseconds (matching the Polygon API).
RTH filtering is applied by the resampler so the resulting parquet is
directly comparable with TradingView's RTH-only chart export.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_PERIOD_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


class PolygonIngestError(ValueError):
    """Raised when Polygon ingest can't proceed."""


@dataclass
class PolygonIngestManifest:
    source: str
    timeframe: str
    rows: int
    trading_days: int
    first_bar_utc: str
    last_bar_utc: str
    rth_only: bool


def resample_ohlcv(
    df_1min: pd.DataFrame,
    minutes: int,
    rth_only: bool = True,
) -> pd.DataFrame:
    """Resample 1-minute bars to ``minutes``-minute bars, RTH-aware.

    Buckets are anchored to the session open (09:30 ET) so that on a 15-min
    timeframe the first bar of each day starts at 09:30, the last starts at
    15:45, etc. Half-days naturally produce fewer buckets.

    Args:
        df_1min: DataFrame containing at minimum the columns
            ``iso_time, open, high, low, close, volume`` (vwap and
            transactions are aggregated when present).
        minutes: Target bar size in minutes. Must divide evenly into 60.
        rth_only: When True, drop any 1-min bar outside 09:30-16:00 ET
            before bucketing (mirrors what TradingView's chart does when
            "Extended Trading Hours" is unchecked).

    Returns:
        New DataFrame with one row per resampled bar.
    """
    if 60 % minutes != 0 and minutes != 60:
        raise PolygonIngestError(f"minutes={minutes} doesn't tile cleanly into 60")

    df = df_1min.copy()
    df["time_utc"] = pd.to_datetime(df["iso_time"], utc=True)
    df["et"] = df["time_utc"].dt.tz_convert("America/New_York")

    if rth_only:
        et_time = df["et"].dt.time
        df = df[(et_time >= _RTH_OPEN) & (et_time < _RTH_CLOSE)].copy()

    # Bucket index = (minutes since 09:30 ET) // window
    df["minutes_from_open"] = (df["et"].dt.hour - 9) * 60 + (df["et"].dt.minute - 30)
    df["bucket"] = df["minutes_from_open"] // minutes
    df["et_date"] = df["et"].dt.date

    grouped = df.groupby(["et_date", "bucket"], sort=True)

    has_vwap = "vwap" in df.columns
    has_tx = "transactions" in df.columns

    agg_dict = {
        "iso_time": "first",
        "time_utc": "first",
        "et": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if has_tx:
        agg_dict["transactions"] = "sum"
    out = grouped.agg(agg_dict).reset_index(drop=True)

    # Volume-weighted VWAP recombination (only meaningful when input has VWAP).
    if has_vwap:

        def _vwap(g: pd.DataFrame) -> float:
            v = g["volume"]
            tot = v.sum()
            return float((g["vwap"] * v).sum() / tot) if tot > 0 else float("nan")

        vwap_series = grouped[["volume", "vwap"]].apply(_vwap)
        out["vwap"] = vwap_series.values

    # Add unix_ts (ms) for parity with the source CSV format
    out["unix_ts"] = (out["time_utc"].astype("int64") // 1_000_000).astype("Int64")

    # Reorder for consistency with downstream code
    cols = ["unix_ts", "iso_time", "time_utc", "et", "open", "high", "low", "close", "volume"]
    if has_vwap:
        cols.append("vwap")
    if has_tx:
        cols.append("transactions")
    return out[cols]


def ingest_polygon_1min_csv_resampled(
    csv_path: Path | str,
    timeframe: str,
    out_parquet: Path | str | None = None,
    rth_only: bool = True,
) -> tuple[pd.DataFrame, PolygonIngestManifest]:
    """Read a 1-min Polygon CSV from disk and resample to ``timeframe``.

    Args:
        csv_path: Path to a CSV with the canonical Polygon 1-min schema.
            See ``SPY_minute_rth_2024-03-28_to_2026-03-28 (1).csv`` for the
            reference shape.
        timeframe: Target bar size, one of ``"5m"``, ``"15m"``, ``"1h"``.
        out_parquet: If provided, write the resampled DataFrame to parquet.
        rth_only: Pass through to :func:`resample_ohlcv`.

    Returns:
        (resampled_dataframe, manifest).
    """
    if timeframe not in _PERIOD_MINUTES:
        raise PolygonIngestError(f"unsupported timeframe {timeframe!r}")

    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    minutes = _PERIOD_MINUTES[timeframe]
    logger.info("[POLYGON INGEST] reading %s, resampling to %d-min bars", csv_path, minutes)

    df_1min = pd.read_csv(
        csv_path,
        usecols=lambda c: (
            c
            in {
                "unix_ts",
                "iso_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "vwap",
                "transactions",
            }
        ),
    )
    out = resample_ohlcv(df_1min, minutes, rth_only=rth_only)

    manifest = PolygonIngestManifest(
        source=str(csv_path),
        timeframe=timeframe,
        rows=len(out),
        trading_days=int(out["et"].dt.date.nunique()),
        first_bar_utc=out["time_utc"].iloc[0].isoformat(),
        last_bar_utc=out["time_utc"].iloc[-1].isoformat(),
        rth_only=rth_only,
    )

    if out_parquet is not None:
        out_parquet = Path(out_parquet)
        out_parquet.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(out_parquet, index=False)
        logger.info("[POLYGON INGEST] wrote %s  (%d rows)", out_parquet, len(out))

    return out, manifest


def ingest_polygon_aggregates(  # pragma: no cover — live API path
    polygon_client,  # type: ignore[no-untyped-def]
    ticker: str,
    timeframe: str,
    from_date: str,
    to_date: str,
    out_parquet: Path | str | None = None,
    rth_only: bool = True,
) -> tuple[pd.DataFrame, PolygonIngestManifest]:
    """Live-fetch aggregates from Polygon and (optionally) RTH-filter."""
    multiplier = _PERIOD_MINUTES[timeframe]
    timespan = "minute" if multiplier < 60 else "hour"
    if timespan == "hour":
        multiplier = multiplier // 60

    bars = polygon_client.fetch_aggregates(
        ticker=ticker,
        multiplier=multiplier,
        timespan=timespan,
        from_date=from_date,
        to_date=to_date,
    )
    df = pd.DataFrame(bars)
    df["unix_ts"] = df["timestamp"].astype("Int64")
    df["iso_time"] = pd.to_datetime(df["unix_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["time_utc"] = pd.to_datetime(df["unix_ts"], unit="ms", utc=True)
    df["et"] = df["time_utc"].dt.tz_convert("America/New_York")

    if rth_only:
        et_time = df["et"].dt.time
        df = df[(et_time >= _RTH_OPEN) & (et_time < _RTH_CLOSE)].reset_index(drop=True)

    cols = ["unix_ts", "iso_time", "time_utc", "et", "open", "high", "low", "close", "volume"]
    if "vwap" in df.columns:
        cols.append("vwap")
    if "transactions" in df.columns:
        cols.append("transactions")
    df = df[cols]

    manifest = PolygonIngestManifest(
        source=f"polygon-live:{ticker}",
        timeframe=timeframe,
        rows=len(df),
        trading_days=int(df["et"].dt.date.nunique()),
        first_bar_utc=df["time_utc"].iloc[0].isoformat() if len(df) else "",
        last_bar_utc=df["time_utc"].iloc[-1].isoformat() if len(df) else "",
        rth_only=rth_only,
    )

    if out_parquet is not None:
        out_parquet = Path(out_parquet)
        out_parquet.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_parquet, index=False)
    return df, manifest

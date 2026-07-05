"""TradingView Pine CSV → parquet ingestor.

Consumes a CSV produced by the ``learn-ai_tv_indicator_dump_v6.pine`` script,
validates it, normalizes column names, and writes a parquet file alongside
a small JSON manifest.

The Pine script emits 25 indicator columns in addition to the chart's own
OHLCV columns. We keep both the indicator columns and the chart's built-in
RSI/MACD/ADX/BB default columns so downstream analysis can cross-check
Pine's ta.* outputs against TradingView's default indicator overlays.

Exceptions raised on validation failure are ``IngestValidationError`` so
callers can distinguish bad-data errors from programming errors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from app.lean_sidecar.trading_calendar import is_regular_session_ms_utc

logger = logging.getLogger(__name__)

# Columns the Pine dump script guarantees. Missing any of these → hard fail.
PINE_INDICATOR_COLS: tuple[str, ...] = (
    "ema_5",
    "ema_10",
    "ema_20",
    "ema_30",
    "ema_40",
    "ema_50",
    "ema_100",
    "ema_200",
    "sma_20",
    "sma_50",
    "sma_200",
    "rsi_14",
    "macd_12_26_9",
    "macds_12_26_9",
    "macdh_12_26_9",
    "bb_mid_20_2",
    "bb_upper_20_2",
    "bb_lower_20_2",
    "adx_14",
    "dmp_14",
    "dmn_14",
    "supert_10_3",
    "supertd_10_3",
    "atr_14",
    "bar_time_unix_s",
)

# Columns the chart export adds automatically on top of what Pine emits.
OHLCV_COLS: tuple[str, ...] = ("time", "open", "high", "low", "close", "Volume")

# The chart's default indicator columns (Bollinger in TradingView default,
# not Pine). Kept for a belt-and-braces cross-check against Pine's values.
TV_DEFAULT_COLS: tuple[str, ...] = (
    "Basis",
    "Upper",
    "Lower",  # default Bollinger
    "Up Trend",
    "Down Trend",  # default SuperTrend
    "RSI",
    "RSI-based MA",  # default RSI
    "Histogram",
    "MACD",
    "Signal line",  # default MACD
    "ADX",  # default ADX
)

_PERIOD_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


class IngestValidationError(ValueError):
    """Raised when an input CSV fails structural validation."""


@dataclass
class TvIngestManifest:
    """Small summary of an ingested TV CSV, written next to the parquet."""

    source_csv: str
    timeframe: str
    rows: int
    trading_days: int
    first_bar_utc: str
    last_bar_utc: str
    first_bar_et: str
    last_bar_et: str
    half_days: int
    duplicate_timestamps: int
    non_rth_bars: int


def _assert_columns(df: pd.DataFrame) -> None:
    missing_pine = [c for c in PINE_INDICATOR_COLS if c not in df.columns]
    if missing_pine:
        raise IngestValidationError(f"CSV is missing {len(missing_pine)} Pine indicator columns: {missing_pine}")
    missing_ohlcv = [c for c in OHLCV_COLS if c not in df.columns]
    if missing_ohlcv:
        raise IngestValidationError(f"CSV is missing OHLCV columns: {missing_ohlcv}")


def _assert_monotonic_unique(df: pd.DataFrame) -> None:
    dup = int(df["time"].duplicated().sum())
    if dup:
        raise IngestValidationError(f"Found {dup} duplicate timestamps")
    if not (df["time"].diff().dropna() > 0).all():
        raise IngestValidationError("Timestamps are not strictly increasing")


def _assert_rth_only(df: pd.DataFrame, et_col: str = "et") -> int:
    ts_ms = (df[et_col].dt.tz_convert("UTC").astype("int64") // 1_000_000).astype("int64")
    return int((~ts_ms.map(lambda ts: is_regular_session_ms_utc(int(ts)))).sum())


def _assert_bar_counts(df: pd.DataFrame, timeframe: str) -> tuple[int, int]:
    """Return (trading_days, half_days) counts."""
    bpd = df.groupby(df["et"].dt.date).size()
    expected = {
        "5m": 78,  # 6.5h / 5min
        "15m": 26,  # 6.5h / 15min
        "1h": 7,  # 9:30 + 6 full hours worth of starts (depends on exchange rounding)
    }.get(timeframe)
    if expected is None:
        return int(bpd.count()), 0
    half_days = int((bpd < expected).sum())
    return int(bpd.count()), half_days


def ingest_tv_csv(
    csv_path: Path | str,
    timeframe: str,
    out_parquet: Path | str | None = None,
    out_manifest: Path | str | None = None,
) -> tuple[pd.DataFrame, TvIngestManifest]:
    """Parse, validate, normalize, and optionally persist a TradingView CSV.

    Args:
        csv_path: Path to the TradingView-exported CSV (Pine + chart OHLCV).
        timeframe: One of ``"5m"``, ``"15m"``, ``"1h"``. Used to sanity-check
            per-day bar counts and to pick the output path prefix.
        out_parquet: Optional target path for the written parquet. If None,
            no file is written and only the DataFrame/manifest are returned.
        out_manifest: Optional target path for the JSON manifest.

    Returns:
        Tuple of (dataframe, manifest). The DataFrame has two added columns:
        ``time_utc`` (tz-aware UTC) and ``et`` (tz-aware America/New_York).

    Raises:
        IngestValidationError: on any structural or RTH/timestamp issue.
    """
    if timeframe not in _PERIOD_SECONDS:
        raise IngestValidationError(f"Unsupported timeframe {timeframe!r}")

    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    logger.info("[TV INGEST] reading %s", csv_path)
    df = pd.read_csv(csv_path)

    _assert_columns(df)
    _assert_monotonic_unique(df)

    # Build time columns.
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["et"] = df["time_utc"].dt.tz_convert("America/New_York")

    non_rth = _assert_rth_only(df)
    if non_rth:
        raise IngestValidationError(
            f"{non_rth} non-RTH bars present. Did the user disable Extended "
            f"Trading Hours in the TV chart settings? (Settings → Symbol tab.)"
        )

    # Cross-check: the Pine script also emits ``bar_time_unix_s``. Pine's
    # ``time`` is in MILLISECONDS while TradingView's chart export uses
    # SECONDS, so the Pine column is 1000× the chart column. The script's
    # name is a misnomer; we tolerate either unit by normalizing.
    if "bar_time_unix_s" in df.columns:
        ratio = df["bar_time_unix_s"].iloc[0] / max(int(df["time"].iloc[0]), 1)
        scale = 1000 if 900 <= ratio <= 1100 else 1
        mismatches = ((df["bar_time_unix_s"].astype("Int64") // scale) != df["time"].astype("Int64")).sum()
        if mismatches:
            raise IngestValidationError(
                f"Pine bar_time_unix_s/{scale} mismatches the chart time column on {mismatches} rows"
            )

    trading_days, half_days = _assert_bar_counts(df, timeframe)

    # Normalize volume column name to lowercase for parity with Polygon.
    df = df.rename(columns={"Volume": "volume"})

    manifest = TvIngestManifest(
        source_csv=str(csv_path),
        timeframe=timeframe,
        rows=len(df),
        trading_days=trading_days,
        first_bar_utc=df["time_utc"].iloc[0].isoformat(),
        last_bar_utc=df["time_utc"].iloc[-1].isoformat(),
        first_bar_et=df["et"].iloc[0].isoformat(),
        last_bar_et=df["et"].iloc[-1].isoformat(),
        half_days=half_days,
        duplicate_timestamps=0,
        non_rth_bars=0,
    )

    if out_parquet is not None:
        out_parquet = Path(out_parquet)
        out_parquet.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_parquet, index=False)
        logger.info("[TV INGEST] wrote %s  (%d rows)", out_parquet, len(df))

    if out_manifest is not None:
        out_manifest = Path(out_manifest)
        out_manifest.parent.mkdir(parents=True, exist_ok=True)
        out_manifest.write_text(json.dumps(asdict(manifest), indent=2))
        logger.info("[TV INGEST] wrote manifest %s", out_manifest)

    return df, manifest

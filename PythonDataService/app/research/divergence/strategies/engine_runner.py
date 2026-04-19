"""V-D runner — exercises learn-ai's streaming engine on full-session bars.

V-D in the research plan = "learn-ai engine, current behavior (full-session
bars in indicators)". To reproduce this faithfully we:

  1. Load full-session 1-minute bars from the LEAN cache (these include
     pre-market and after-hours, exactly what the engine ingests today).
  2. Resample to 15-minute bars without an RTH filter, mirroring how
     ``TradeBarConsolidator`` would emit them.
  3. Stream the resulting bars through the same ``Indicator`` classes the
     engine uses (EMA5, EMA10, RSI14), so the values reflect ETH
     contamination.
  4. Filter the resulting bar-by-bar table to RTH-only rows for trade
     execution (strategies only fire intra-RTH).
  5. Apply the S1 entry/exit rules and emit a TradeList.

The result is the trade list a current-as-of-today learn-ai backtest
would produce, modulo fill-model differences. Used in Day 4 to quantify
the cost of the ETH-contamination bug surfaced in the audit.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import date, time
from pathlib import Path

import pandas as pd

from app.research.divergence.indicators.engine_adapter import (
    compute_engine_ema_batch,
    compute_engine_rsi_batch,
)
from app.research.divergence.ingest.polygon_ingest import resample_ohlcv

logger = logging.getLogger(__name__)

LEAN_CACHE_SPY = Path(
    "/sessions/pensive-loving-brahmagupta/mnt/learn-ai/PythonDataService/lean-cache/equity/usa/minute/spy"
)
PRICE_SCALE = 10000  # LEAN deci-cent encoding
EASTERN = "America/New_York"
RTH_OPEN, RTH_CLOSE = time(9, 30), time(16, 0)


def _load_lean_day(zip_path: Path, trading_date: date) -> pd.DataFrame:
    """Decode one LEAN minute zip into a DataFrame of 1-min OHLCV bars."""
    with zipfile.ZipFile(zip_path) as zf:
        name = zf.namelist()[0]
        raw = zf.open(name).read().decode("ascii")
    rows = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 6:
            continue
        ms, o, h, l, c, v = parts
        rows.append((int(ms), int(o), int(h), int(l), int(c), int(v)))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ms", "open", "high", "low", "close", "volume"])
    # Build tz-aware ET timestamp from midnight + ms
    midnight = pd.Timestamp(trading_date.year, trading_date.month, trading_date.day, tz=EASTERN)
    df["et"] = midnight + pd.to_timedelta(df["ms"], unit="ms")
    df["time_utc"] = df["et"].dt.tz_convert("UTC")
    df["iso_time"] = df["time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float) / PRICE_SCALE
    return df.drop(columns=["ms"])


def load_lean_full_session_1min(start: date, end: date) -> pd.DataFrame:
    """Load all available 1-min full-session bars between ``start`` and ``end``."""
    if not LEAN_CACHE_SPY.exists():
        raise FileNotFoundError(f"LEAN cache not found: {LEAN_CACHE_SPY}")

    frames: list[pd.DataFrame] = []
    current = start
    while current <= end:
        zp = LEAN_CACHE_SPY / f"{current.strftime('%Y%m%d')}_trade.zip"
        if zp.exists():
            day_df = _load_lean_day(zp, current)
            if len(day_df):
                frames.append(day_df)
        current = pd.Timestamp(current).date() + pd.Timedelta(days=1).to_pytimedelta()
    if not frames:
        raise FileNotFoundError(f"No LEAN files in [{start}, {end}]")
    out = pd.concat(frames, ignore_index=True).sort_values("time_utc").reset_index(drop=True)
    logger.info("[VD] loaded %d full-session 1-min bars from %s to %s", len(out), start, end)
    return out


def build_vd_15m_with_engine_indicators(
    start: date,
    end: date,
) -> pd.DataFrame:
    """Build the V-D 15-min DataFrame with engine indicators contaminated by ETH.

    Returns one row per RTH-only 15-min bar; the indicator columns reflect the
    engine's state including all preceding ETH bars. Suitable input for the
    pandas strategy runners with ``variant="V-D"``.
    """
    raw = load_lean_full_session_1min(start, end)
    # Resample to 15-min including every session bar
    bars_all = resample_ohlcv(raw, minutes=15, rth_only=False)
    logger.info("[VD] %d full-session 15-min bars", len(bars_all))

    # Stream ALL session bars through engine indicators
    bars_all["ema_5_engine"] = compute_engine_ema_batch(bars_all, length=5, time_col="time_utc", value_col="close")
    bars_all["ema_10_engine"] = compute_engine_ema_batch(bars_all, length=10, time_col="time_utc", value_col="close")
    bars_all["rsi_14_engine"] = compute_engine_rsi_batch(bars_all, length=14, time_col="time_utc", value_col="close")
    # SMA 50/200 for S3
    from app.research.divergence.indicators.engine_adapter import compute_engine_sma_batch

    bars_all["sma_50_engine"] = compute_engine_sma_batch(bars_all, length=50, time_col="time_utc", value_col="close")
    bars_all["sma_200_engine"] = compute_engine_sma_batch(bars_all, length=200, time_col="time_utc", value_col="close")

    # Now filter to RTH bars for trade execution (indicator state is preserved
    # because we computed sequentially through ALL bars).
    bars_all["et"] = bars_all["time_utc"].dt.tz_convert(EASTERN)
    et_t = bars_all["et"].dt.time
    rth_mask = (et_t >= RTH_OPEN) & (et_t < RTH_CLOSE)
    rth = bars_all[rth_mask].reset_index(drop=True)
    # Rename close so strategy code can use close_pg as the canonical column
    rth = rth.rename(columns={"close": "close_pg"})
    logger.info("[VD] %d RTH 15-min bars retained for execution", len(rth))
    return rth

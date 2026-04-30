"""Dataset generation service: chunked OHLCV fetch + dynamic pandas-ta indicator calculation"""

from __future__ import annotations

import csv
import inspect
import io
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import pandas_ta as ta

from app.services.polygon_client import PolygonClientService


class RunCancelledError(Exception):
    """Raised by the chunker when ``cancel_check`` returns True. Callers
    catch this to emit a cancellation event and stop cleanly."""


logger = logging.getLogger(__name__)

_POLYGON_MAX_BARS = 50_000
_MINUTES_PER_DAY = 450
_DAYS_PER_CHUNK = _POLYGON_MAX_BARS // _MINUTES_PER_DAY
_WARMUP_MULTIPLIER = 5

_ET = ZoneInfo("US/Eastern")
_RTH_START_HOUR, _RTH_START_MIN = 9, 30
_RTH_END_HOUR, _RTH_END_MIN = 16, 0

# Default indicator configurations matching TradingView standard setup
DEFAULT_INDICATORS: list[dict[str, Any]] = [
    {"name": "ema", "params": {"length": 5}},
    {"name": "ema", "params": {"length": 10}},
    {"name": "ema", "params": {"length": 20}},
    {"name": "ema", "params": {"length": 30}},
    {"name": "ema", "params": {"length": 40}},
    {"name": "ema", "params": {"length": 50}},
    {"name": "ema", "params": {"length": 100}},
    {"name": "ema", "params": {"length": 200}},
    {"name": "bbands", "params": {"length": 20, "std": 2.0}},
    {"name": "supertrend", "params": {"length": 10, "multiplier": 3.0}},
    {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
]

# Configurable parameters for key indicators
INDICATOR_CONFIGS: dict[str, list[dict[str, Any]]] = {
    "ema": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "sma": [{"name": "length", "type": "int", "default": 20, "min": 1, "max": 500, "description": "Lookback period"}],
    "dema": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "tema": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "wma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "hma": [{"name": "length", "type": "int", "default": 9, "min": 1, "max": 500, "description": "Lookback period"}],
    "kama": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "zlma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "rma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "alma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "bbands": [
        {"name": "length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "SMA lookback period"},
        {"name": "std", "type": "float", "default": 2.0, "min": 0.1, "max": 5.0, "description": "Standard deviations"},
    ],
    "supertrend": [
        {"name": "length", "type": "int", "default": 10, "min": 1, "max": 100, "description": "ATR period"},
        {
            "name": "multiplier",
            "type": "float",
            "default": 3.0,
            "min": 0.5,
            "max": 10.0,
            "description": "ATR multiplier",
        },
    ],
    "rsi": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "macd": [
        {"name": "fast", "type": "int", "default": 12, "min": 1, "max": 100, "description": "Fast EMA period"},
        {"name": "slow", "type": "int", "default": 26, "min": 1, "max": 200, "description": "Slow EMA period"},
        {"name": "signal", "type": "int", "default": 9, "min": 1, "max": 50, "description": "Signal EMA period"},
    ],
    "adx": [
        {"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "DI/ADX smoothing period"}
    ],
    "atr": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "ATR period"}],
    "stoch": [
        {"name": "k", "type": "int", "default": 14, "min": 1, "max": 100, "description": "%K lookback period"},
        {"name": "d", "type": "int", "default": 3, "min": 1, "max": 50, "description": "%D smoothing period"},
    ],
    "stochrsi": [
        {"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "RSI lookback period"}
    ],
    "cci": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "willr": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "roc": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 100, "description": "Lookback period"}],
    "mom": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 100, "description": "Lookback period"}],
    "donchian": [
        {
            "name": "lower_length",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 200,
            "description": "Lower channel period",
        },
        {
            "name": "upper_length",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 200,
            "description": "Upper channel period",
        },
    ],
    "kc": [
        {"name": "length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "EMA period"},
        {"name": "scalar", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0, "description": "ATR multiplier"},
    ],
    "psar": [
        {
            "name": "af0",
            "type": "float",
            "default": 0.02,
            "min": 0.001,
            "max": 0.1,
            "description": "Initial acceleration factor",
        },
        {
            "name": "af",
            "type": "float",
            "default": 0.02,
            "min": 0.001,
            "max": 0.1,
            "description": "Acceleration factor step",
        },
        {
            "name": "max_af",
            "type": "float",
            "default": 0.2,
            "min": 0.05,
            "max": 1.0,
            "description": "Maximum acceleration factor",
        },
    ],
    "aroon": [{"name": "length", "type": "int", "default": 25, "min": 1, "max": 100, "description": "Lookback period"}],
    "natr": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "ATR period"}],
    "obv": [],
    "ad": [],
    "cmf": [{"name": "length", "type": "int", "default": 20, "min": 1, "max": 100, "description": "Lookback period"}],
    "mfi": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "vwap": [],
    "tsi": [
        {"name": "fast", "type": "int", "default": 13, "min": 1, "max": 100, "description": "Fast period"},
        {"name": "slow", "type": "int", "default": 25, "min": 1, "max": 200, "description": "Slow period"},
    ],
    "fisher": [{"name": "length", "type": "int", "default": 9, "min": 1, "max": 100, "description": "Lookback period"}],
    "squeeze": [
        {
            "name": "bb_length",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 200,
            "description": "Bollinger Bands period",
        },
        {
            "name": "kc_length",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 200,
            "description": "Keltner Channel period",
        },
    ],
}


def get_indicator_configs() -> dict[str, list[dict[str, Any]]]:
    """Return configurable parameters for each indicator."""
    return INDICATOR_CONFIGS


def list_available_indicators() -> dict[str, list[dict[str, str]]]:
    """Return all pandas-ta indicators grouped by category with descriptions."""
    categories: dict[str, list[dict[str, str]]] = {}
    for cat_name, indicator_names in ta.Category.items():
        items = []
        for name in sorted(indicator_names):
            fn = getattr(ta, name, None)
            doc = ""
            if fn is not None:
                raw_doc = inspect.getdoc(fn) or ""
                for line in raw_doc.split("\n"):
                    stripped = line.strip()
                    if stripped:
                        doc = stripped
                        break
            items.append({"name": name, "category": cat_name, "description": doc})
        categories[cat_name] = items
    return categories


def fetch_bars_chunked(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    timespan: str = "minute",
    multiplier: int = 1,
    adjusted: bool = True,
    sort: str = "asc",
    limit: int = 50000,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Fetch OHLCV bars for a long date range by splitting into chunks.

    ``sort`` and ``limit`` map to Polygon's aggregate query params but only
    affect each per-chunk request — the final merged list is always
    timestamp-sorted ascending (dedup-safe for overlapping chunk boundaries).
    To surface ``desc`` output to the caller, we reverse after merge.

    When ``on_event`` is supplied, the chunker emits progress dicts:
        ``{type: "chunk_plan", total: int}``
        ``{type: "chunk_start", index: int, total: int, from: str, to: str}``
        ``{type: "chunk_done", index: int, total: int, bars_returned: int}``
        ``{type: "chunk_paced", wait_seconds: float, label: str}``  (from throttle)

    When ``cancel_check`` returns True between chunks, the chunker stops
    early and raises ``RunCancelledError``. The caller should treat this as
    a cooperative abort, not an error.
    """
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")

    # Dynamic chunk size based on timespan and multiplier. Extended
    # resolutions (second/week/month/quarter/year) get conservative defaults;
    # the chunker is bar-count-bounded, not bar-duration bounded, so picking
    # large values for low-frequency resolutions just means the whole range
    # fits in one chunk.
    _bars_per_day = {
        "second": 27_000,
        "minute": 450,
        "hour": 24,
        "day": 1,
        "week": 1,
        "month": 1,
        "quarter": 1,
        "year": 1,
    }
    effective_bpd = _bars_per_day.get(timespan, 450) // max(1, multiplier)
    days_per_chunk = max(1, _POLYGON_MAX_BARS // max(1, effective_bpd))

    # Pre-compute total chunk count so the UI can render "chunk N of M"
    # before the first request fires.
    total_days = max(1, (end - start).days + 1)
    total_chunks = max(1, (total_days + days_per_chunk - 1) // days_per_chunk)
    if on_event is not None:
        on_event({"type": "chunk_plan", "total": total_chunks})

    all_bars: list[dict[str, Any]] = []
    chunk_start = start
    chunk_idx = 0

    while chunk_start < end:
        if cancel_check is not None and cancel_check():
            raise RunCancelledError(f"cancelled after chunk {chunk_idx} of {total_chunks}")
        chunk_end = min(chunk_start + timedelta(days=days_per_chunk), end)
        chunk_idx += 1
        from_str = chunk_start.strftime("%Y-%m-%d")
        to_str = chunk_end.strftime("%Y-%m-%d")
        logger.info(f"[CHUNK {chunk_idx}] Fetching {multiplier}{timespan} bars for {ticker}: {from_str} to {to_str}")
        if on_event is not None:
            on_event(
                {
                    "type": "chunk_start",
                    "index": chunk_idx,
                    "total": total_chunks,
                    "from": from_str,
                    "to": to_str,
                }
            )
        bars = polygon.fetch_aggregates(
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_date=from_str,
            to_date=to_str,
            adjusted=adjusted,
            sort=sort,
            limit=limit,
            on_event=on_event,
        )
        all_bars.extend(bars)
        logger.info(f"[CHUNK {chunk_idx}] Got {len(bars)} bars (total: {len(all_bars)})")
        if on_event is not None:
            on_event(
                {
                    "type": "chunk_done",
                    "index": chunk_idx,
                    "total": total_chunks,
                    "bars_returned": len(bars),
                }
            )
        chunk_start = chunk_end + timedelta(days=1)

    total_raw = len(all_bars)
    seen: set[int] = set()
    unique_bars: list[dict[str, Any]] = []
    for bar in all_bars:
        ts = bar["timestamp"]
        if ts not in seen:
            seen.add(ts)
            unique_bars.append(bar)
    unique_bars.sort(key=lambda b: b["timestamp"])

    # Chunk boundary verification
    chunk_overlaps = total_raw - len(unique_bars)
    if chunk_overlaps > 0:
        logger.info(f"[CHUNK MERGE] Removed {chunk_overlaps} overlapping bars at chunk boundaries")
    if len(unique_bars) > 1:
        timestamps = [b["timestamp"] for b in unique_bars]
        non_mono = sum(1 for i in range(1, len(timestamps)) if timestamps[i] <= timestamps[i - 1])
        if non_mono > 0:
            logger.warning(f"[CHUNK MERGE] {non_mono} non-monotonic timestamps after dedup — re-sorting")
            unique_bars.sort(key=lambda b: b["timestamp"])

    logger.info(f"Total unique {multiplier}{timespan} bars: {len(unique_bars)}")
    return unique_bars


_BAR_WINDOW_MINUTES_PER_UNIT: dict[str, int] = {
    "second": 1,  # rounded up to one minute for the session-window check
    "minute": 1,
    "hour": 60,
    "day": 1440,
    "week": 1440,
    "month": 1440,
    "quarter": 1440,
    "year": 1440,
}


def filter_session(
    df: pd.DataFrame,
    session: str,
    timespan: str = "minute",
    multiplier: int = 1,
) -> pd.DataFrame:
    """Filter bars to RTH (09:30–16:00 ET) or keep all (extended).

    A bar is kept whenever its trade window OVERLAPS [09:30, 16:00) ET on
    a weekday — not just when its start-of-window timestamp falls inside
    that range. The original implementation only checked the start, which
    silently dropped:

      * The 09:00 ET hourly bar (window 09:00–10:00) that contains the
        09:30 RTH open. RTH hourly datasets came back with 6 bars/day
        instead of 7.
      * Every daily/weekly/monthly bar (timestamp at 00:00 ET, well
        before 09:30). RTH daily datasets came back with zero rows.

    Day-and-above bars are treated as 1440-minute windows so they
    overlap RTH on every weekday — the weekday mask is the only
    practical filter for those resolutions.
    """
    if session != "rth":
        return df

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    time_minutes = dt_et.dt.hour * 60 + dt_et.dt.minute
    bar_window_minutes = max(1, multiplier) * _BAR_WINDOW_MINUTES_PER_UNIT.get(timespan, 1)

    rth_start = _RTH_START_HOUR * 60 + _RTH_START_MIN  # 570
    rth_end = _RTH_END_HOUR * 60 + _RTH_END_MIN  # 960

    overlaps_rth = (time_minutes < rth_end) & ((time_minutes + bar_window_minutes) > rth_start)
    is_weekday = dt_et.dt.dayofweek < 5
    mask = overlaps_rth & is_weekday

    before = len(df)
    df = df[mask].reset_index(drop=True)
    logger.info(f"[SESSION] RTH filter ({multiplier}{timespan}): {before} → {len(df)} bars")
    return df


def fetch_rth_closes(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    adjusted: bool = True,
    buffer_days: int = 14,
) -> dict[str, float]:
    """Map each trading date in [from_date - buffer_days, to_date] to that
    day's RTH close (16:00 ET) from Polygon daily aggregates.

    The 14-day default buffer ensures the first trading day in the requested
    window has a prior session's close available for ``add_previous_close_column``;
    it absorbs weekends and any reasonable single-week market closure.

    Polygon's daily-bar timestamp is the start-of-day in UTC for the trading
    date; converting to ET and taking ``.date`` gives the bar's trading date
    unambiguously.
    """
    buffer_start = (
        datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=buffer_days)
    ).strftime("%Y-%m-%d")

    daily = polygon.fetch_aggregates(
        ticker=ticker,
        multiplier=1,
        timespan="day",
        from_date=buffer_start,
        to_date=to_date,
        adjusted=adjusted,
        sort="asc",
    )
    if not daily:
        logger.warning(f"[PC] No daily bars returned for {ticker} {buffer_start}→{to_date}")
        return {}

    closes: dict[str, float] = {}
    for bar in daily:
        ts_ms = int(bar["timestamp"])
        bar_date = pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(_ET).date()
        closes[bar_date.isoformat()] = float(bar["close"])

    logger.info(f"[PC] Built RTH-close map for {ticker}: {len(closes)} trading days")
    return closes


# Wall-clock minute (ET) at which the regular trading session ends.
_RTH_CLOSE_MINUTE = _RTH_END_HOUR * 60 + _RTH_END_MIN  # 16:00 → 960


def add_previous_close_column(
    df: pd.DataFrame,
    rth_closes: dict[str, float],
    column_name: str = "PC",
) -> pd.DataFrame:
    """Stamp ``column_name`` with the close of the most recently completed
    RTH session at or before each bar's timestamp.

    Rule (ET wall-clock):
      * Bar time < 16:00 → PC = previous trading day's RTH close.
      * Bar time ≥ 16:00 → PC = today's RTH close (the session that just
        ended).

    This is the time-aware reading of "previous close": morning RTH bars on
    day D and pre-market bars on day D both reference D−1's close, while
    after-hours bars on day D reference D's just-completed close — so the
    overnight gap between two adjacent extended-session bars is a single
    subtraction.

    Bars whose lookup target isn't in ``rth_closes`` (e.g. the very first
    trading day in a fresh feed with no prior session) get NaN, never a
    silent zero.
    """
    if df.empty:
        df[column_name] = pd.Series(dtype="float64")
        return df

    if not rth_closes:
        df[column_name] = pd.Series([float("nan")] * len(df), dtype="float64")
        return df

    dt_et = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(_ET)
    bar_dates = dt_et.dt.date.astype(str).reset_index(drop=True)
    bar_minute_of_day = (dt_et.dt.hour * 60 + dt_et.dt.minute).reset_index(drop=True)
    after_close_mask = bar_minute_of_day >= _RTH_CLOSE_MINUTE

    sorted_dates = sorted(rth_closes.keys())
    prev_date_map: dict[str, str] = {sorted_dates[i]: sorted_dates[i - 1] for i in range(1, len(sorted_dates))}

    today_close = bar_dates.map(rth_closes)
    prev_close = bar_dates.map(prev_date_map).map(rth_closes)

    pc = today_close.where(after_close_mask, prev_close).astype("float64")
    df[column_name] = pc.to_numpy()
    return df


def forward_fill_gaps(
    df: pd.DataFrame,
    session: str,
    timespan: str = "minute",
    multiplier: int = 1,
) -> pd.DataFrame:
    """
    Build a continuous bar grid at the requested resolution and forward-fill
    missing bars. Missing bars get: open=high=low=close=prev_close, volume=0,
    transactions=0.

    The grid frequency must match the requested ``(timespan, multiplier)`` —
    otherwise the merge would expand higher-resolution bars onto a finer grid
    and silently reshape the data (e.g. 15-minute bars expanded to 1-minute
    rows via ffill).

    Only minute-resolution bars participate in forward-fill: those are the
    ones where Polygon legitimately omits empty intra-session minutes and a
    continuous grid is useful for indicator math. Hour/day/week/month bars
    come back from Polygon already aligned to their own boundaries (top of
    the hour, top of the day, etc.) which do not match the RTH session
    start of 09:30 ET; building a synthetic grid for them would produce
    timestamps that never line up with Polygon's bars, the merge would
    return NaN for every row, and the resulting CSV looks empty. Return
    the frame unchanged in that case.
    """
    if df.empty:
        return df

    if timespan != "minute":
        logger.info(
            f"[FILL] Skipping forward_fill for timespan={timespan} — Polygon returns "
            f"these bars pre-aligned and an RTH-anchored grid would mis-align with them."
        )
        return df

    freq = f"{max(1, multiplier)}min"
    bar_delta = timedelta(minutes=max(1, multiplier))

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    df["_dt_et"] = dt_et

    # Group by trading date and build continuous bar ranges
    filled_frames = []
    for date, group in df.groupby(dt_et.dt.date):
        day_dt = pd.Timestamp(date)
        if day_dt.dayofweek >= 5:
            continue  # skip weekends

        if session == "rth":
            start = datetime.combine(
                date, datetime.min.time().replace(hour=_RTH_START_HOUR, minute=_RTH_START_MIN, tzinfo=_ET)
            )
            end = datetime.combine(date, datetime.min.time().replace(hour=_RTH_END_HOUR, minute=0, tzinfo=_ET))
        else:
            # Extended: 04:00 - 20:00 ET (typical Polygon range)
            first_bar = group["_dt_et"].iloc[0]
            last_bar = group["_dt_et"].iloc[-1]
            start = first_bar.floor(freq)
            end = last_bar.ceil(freq) + bar_delta

        minute_range = pd.date_range(start=start, end=end - bar_delta, freq=freq, tz=_ET)
        # Convert to milliseconds since epoch (pandas 3.0 uses variable resolution,
        # so .astype("int64") may return µs not ns — use total_seconds() for safety)
        _epoch = pd.Timestamp("1970-01-01", tz="UTC")
        minute_ts = ((minute_range.tz_convert("UTC") - _epoch).total_seconds() * 1000).astype("int64")

        template = pd.DataFrame({"timestamp": minute_ts})
        merged = template.merge(group.drop(columns=["_dt_et"], errors="ignore"), on="timestamp", how="left")

        # Forward-fill OHLC with previous close
        merged["close"] = merged["close"].ffill()
        for col in ["open", "high", "low"]:
            merged[col] = merged[col].fillna(merged["close"])
        merged["volume"] = merged["volume"].fillna(0)
        merged["transactions"] = merged["transactions"].fillna(0)
        if "vwap" in merged.columns:
            merged["vwap"] = merged["vwap"].ffill()

        filled_frames.append(merged)

    if not filled_frames:
        return df.drop(columns=["_dt_et"], errors="ignore")

    result = pd.concat(filled_frames, ignore_index=True)
    result = result.sort_values("timestamp").reset_index(drop=True)

    # Remove any remaining _dt_et column
    result = result.drop(columns=["_dt_et"], errors="ignore")

    logger.info(f"[FILL] Forward-filled: {len(df)} → {len(result)} bars")
    return result


def compute_warmup_start_date(
    from_date: str,
    max_lookback: int,
    timespan: str = "minute",
    multiplier: int = 1,
) -> str:
    """Step back from from_date enough calendar days to warm up indicators."""
    warmup_bars = max_lookback * _WARMUP_MULTIPLIER
    bars_per_day = {"minute": 390, "hour": 7, "day": 1}
    bpd = bars_per_day.get(timespan, 390) * multiplier
    warmup_days = max(1, (warmup_bars // bpd) + 2)
    return (datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=warmup_days)).strftime("%Y-%m-%d")


def estimate_max_lookback(indicator_entries: list[dict[str, Any]]) -> int:
    """Scan indicator_entries for the largest lookback parameter."""
    lookback = 0
    for entry in indicator_entries:
        params = entry.get("params", {})
        for key in ("length", "slow", "k", "bb_length", "kc_length", "lower_length", "upper_length"):
            val = params.get(key, 0)
            if isinstance(val, (int, float)):
                lookback = max(lookback, int(val))
    return max(lookback, 200)


def indicator_table_params_to_entries(
    ema_periods: list[int],
    bb_length: int = 20,
    bb_std: float = 2.0,
    supertrend_length: int = 10,
    supertrend_multiplier: float = 3.0,
    rsi_length: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    adx_length: int = 14,
) -> list[dict[str, Any]]:
    """Convert fixed indicator-table params into dynamic indicator_entries."""
    entries: list[dict[str, Any]] = []
    for period in sorted(ema_periods):
        entries.append({"name": "ema", "params": {"length": period}})
    entries.append({"name": "bbands", "params": {"length": bb_length, "std": bb_std}})
    entries.append({"name": "supertrend", "params": {"length": supertrend_length, "multiplier": supertrend_multiplier}})
    entries.append({"name": "rsi", "params": {"length": rsi_length}})
    entries.append({"name": "macd", "params": {"fast": macd_fast, "slow": macd_slow, "signal": macd_signal}})
    entries.append({"name": "adx", "params": {"length": adx_length}})
    return entries


def _tag_session_column(
    df: pd.DataFrame,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Tag each bar with session type: 'rth', 'pre', or 'post' using NYSE calendar."""
    if df.empty:
        return df
    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.schedule(start_date=from_date, end_date=to_date)
    if schedule.empty:
        df["session"] = "pre"
        return df

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["session"] = "pre"  # default
    for _, row in schedule.iterrows():
        open_t = row["market_open"]
        close_t = row["market_close"]
        rth_mask = (dt_utc >= open_t) & (dt_utc < close_t)
        df.loc[rth_mask, "session"] = "rth"
        close_et = close_t.tz_convert(_ET)
        post_end = close_et.replace(hour=20, minute=0, second=0)
        post_end_utc = post_end.tz_convert("UTC")
        post_mask = (dt_utc >= close_t) & (dt_utc < post_end_utc)
        df.loc[post_mask, "session"] = "post"
    return df


def preprocess_and_calculate(
    bars: list[dict[str, Any]],
    indicator_entries: list[dict[str, Any]],
    session: str = "extended",
    forward_fill: bool = False,
    trim_from_ts: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    timespan: str = "minute",
    multiplier: int = 1,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """
    Shared preprocessing pipeline:
      1. Sort and deduplicate bars
      2. Session filter (RTH or extended)
      3. Tag session column (rth/pre/post) for CSV export
      4. Forward-fill gaps (optional)
      5. Calculate dynamic indicators
      6. Trim warm-up rows (optional, by timestamp)
    """
    df = pd.DataFrame(bars)
    df = df.sort_values("timestamp").reset_index(drop=True)

    df = filter_session(df, session, timespan=timespan, multiplier=multiplier)

    # Tag session column for CSV export (uses NYSE calendar)
    if from_date and to_date:
        df = _tag_session_column(df, from_date, to_date)

    if forward_fill:
        df = forward_fill_gaps(df, session, timespan=timespan, multiplier=multiplier)

    column_meta: list[dict[str, Any]] = []
    if indicator_entries:
        df, column_meta = calculate_dynamic_indicators(df, indicator_entries)

    if trim_from_ts is not None:
        before = len(df)
        df = df[df["timestamp"] >= trim_from_ts].reset_index(drop=True)
        logger.info(f"[TRIM] Warm-up trimmed: {before} → {len(df)} rows")

    return df, column_meta


def rename_to_indicator_table_columns(
    df: pd.DataFrame,
    column_meta: list[dict[str, Any]],
) -> pd.DataFrame:
    """Rename pandas-ta raw column names to the indicator-table API contract."""
    rename_map: dict[str, str] = {}
    for m in column_meta:
        col = m["column"]
        ind = m["indicator"]
        if ind == "bbands":
            if col.startswith("bbl"):
                rename_map[col] = "bb_lower"
            elif col.startswith("bbm"):
                rename_map[col] = "bb_basis"
            elif col.startswith("bbu"):
                rename_map[col] = "bb_upper"
        elif ind == "supertrend":
            if col.startswith("supertl"):
                rename_map[col] = "supertrend_up"
            elif col.startswith("superts"):
                rename_map[col] = "supertrend_down"
        elif ind == "rsi":
            rename_map[col] = "rsi"
        elif ind == "macd":
            if col.startswith("macdh"):
                rename_map[col] = "macd_histogram"
            elif col.startswith("macds"):
                rename_map[col] = "macd_signal"
            else:
                rename_map[col] = "macd"
        elif ind == "adx":
            if "dmp" not in col and "dmn" not in col:
                rename_map[col] = "adx"
        elif ind == "ema":
            # calculate_dynamic_indicators produces "ema_length5" → rename to "ema_5"
            length = m.get("params", "").replace("length=", "")
            if length:
                rename_map[col] = f"ema_{length}"

    # Drop columns not in the indicator-table contract (bbb, bbp, supert, supertd, dmp, dmn)
    keep_cols = set(rename_map.values()) | {"timestamp", "open", "high", "low", "close", "volume"}
    df = df.rename(columns=rename_map)
    drop_cols = [c for c in df.columns if c not in keep_cols]
    df = df.drop(columns=drop_cols, errors="ignore")
    return df


def calculate_dynamic_indicators(
    df: pd.DataFrame,
    indicator_entries: list[dict[str, Any]],
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """
    Calculate selected pandas-ta indicators on a DataFrame with OHLCV columns.
    indicator_entries: list of {"name": "ema", "params": {"length": 20}}
    Returns the enriched DataFrame and column metadata list.
    """
    column_meta: list[dict[str, Any]] = []

    for entry in indicator_entries:
        ind_name = entry.get("name", "")
        params = entry.get("params", {})

        fn = getattr(ta, ind_name, None)
        if fn is None:
            logger.warning(f"Unknown indicator: {ind_name}, skipping")
            continue

        try:
            sig = inspect.signature(fn)
            param_names = list(sig.parameters.keys())

            args: list[Any] = []
            if _needs_param(param_names, "high", "low", "close"):
                args = [df["high"], df["low"], df["close"]]
            elif _needs_param(param_names, "high", "low"):
                args = [df["high"], df["low"]]
            elif _needs_param(param_names, "close"):
                args = [df["close"]]
            elif _needs_param(param_names, "open_"):
                args = [df["open"], df["high"], df["low"], df["close"]]
            elif _needs_param(param_names, "volume"):
                args = [df["volume"]]

            kwargs: dict[str, Any] = {**params}
            if (
                "volume" in param_names
                and "volume" not in [p for p in param_names[: len(args)]]
                and len(args) > 0
                and "volume" not in kwargs
            ):
                kwargs["volume"] = df["volume"]

            result = fn(*args, **kwargs)
            if result is None:
                logger.warning(f"Indicator {ind_name} returned None")
                continue

            param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "default"

            if isinstance(result, pd.DataFrame):
                for col in result.columns:
                    clean = col.lower().replace(" ", "_")
                    df[clean] = result[col]
                    column_meta.append(
                        {
                            "column": clean,
                            "indicator": ind_name,
                            "params": param_str,
                            "library": "pandas-ta",
                        }
                    )
            elif isinstance(result, pd.Series):
                col_name = f"{ind_name}_{param_str.replace(', ', '_').replace('=', '')}" if params else ind_name
                col_name = col_name.lower().replace(" ", "_")
                df[col_name] = result
                column_meta.append(
                    {
                        "column": col_name,
                        "indicator": ind_name,
                        "params": param_str,
                        "library": "pandas-ta",
                    }
                )

            logger.info(
                f"Calculated {ind_name}({param_str}): +{len(result.columns) if isinstance(result, pd.DataFrame) else 1} columns"
            )

        except Exception as e:
            logger.error(f"Error calculating {ind_name}: {e}", exc_info=True)
            continue

    return df, column_meta


def build_csv_bytes(df: pd.DataFrame, columns: list[str]) -> bytes:
    """Serialize a DataFrame to CSV bytes with unix_ts and iso_time columns first."""
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["unix_ts", "iso_time", *columns]
    writer.writerow(header)

    for _, row in df.iterrows():
        ts = int(row["timestamp"])
        iso = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
        values = [ts, iso] + [_fmt(row.get(col)) for col in columns]
        writer.writerow(values)
    return output.getvalue().encode("utf-8")


def build_metadata_json(
    ticker: str,
    from_date: str,
    to_date: str,
    bar_count: int,
    column_meta: list[dict[str, Any]],
    ohlcv_cols: list[str],
    session: str = "extended",
    forward_fill: bool = True,
    raw_bar_count: int = 0,
    filled_bar_count: int = 0,
) -> bytes:
    """Generate CSV metadata JSON describing every column and its calculation."""
    base_columns = [
        {
            "column": "unix_ts",
            "type": "int",
            "description": "Unix timestamp in milliseconds (UTC)",
            "source": "Polygon.io",
        },
        {
            "column": "iso_time",
            "type": "string",
            "description": "ISO 8601 datetime (UTC)",
            "source": "Derived from unix_ts",
        },
    ]
    for col in ohlcv_cols:
        desc_map = {
            "PC": "Previous trading day's RTH close for the underlying ticker",
            "open": "Opening price of the minute bar",
            "high": "Highest price during the minute bar",
            "low": "Lowest price during the minute bar",
            "close": "Closing price of the minute bar",
            "volume": "Number of shares traded during the minute bar",
            "vwap": "Volume-weighted average price for the minute bar",
            "transactions": "Number of transactions in the minute bar",
        }
        source_map = {
            "PC": "Polygon.io REST API (list_aggs, daily timespan)",
        }
        base_columns.append(
            {
                "column": col,
                "type": "float" if col != "transactions" else "int",
                "description": desc_map.get(col, col),
                "source": source_map.get(col, "Polygon.io REST API (list_aggs)"),
            }
        )

    # Session column (added by _tag_session_column)
    base_columns.append(
        {
            "column": "session",
            "type": "string",
            "description": "Trading session: rth (regular 09:30-16:00 ET), pre (pre-market), or post (after-hours)",
            "source": "Derived from NYSE calendar",
        }
    )

    indicator_columns = []
    for meta in column_meta:
        desc = _describe_indicator_column(meta["indicator"], meta["column"], meta["params"])
        indicator_columns.append(
            {
                "column": meta["column"],
                "type": "float",
                "indicator": meta["indicator"],
                "parameters": meta["params"],
                "library": meta["library"],
                "description": desc,
            }
        )

    metadata = {
        "dataset": {
            "ticker": ticker,
            "from_date": from_date,
            "to_date": to_date,
            "timespan": "minute",
            "multiplier": 1,
            "bar_count": bar_count,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "data_source": {
            "provider": "Polygon.io",
            "plan": "Starter (2-year history, 15-min delayed)",
            "api": "REST v2 list_aggs with auto-pagination",
            "chunking": f"Date range split into ~{_DAYS_PER_CHUNK}-day windows to stay within {_POLYGON_MAX_BARS} bar API limit",
        },
        "calculation_engine": {
            "library": "pandas-ta",
            "version": getattr(ta, "version", "unknown"),
            "description": "Technical Analysis library for Python built on pandas, providing 150+ indicators",
            "url": "https://github.com/twopirllc/pandas-ta",
        },
        "columns": base_columns + indicator_columns,
        "processing": {
            "session_filter": session,
            "session_description": "Regular Trading Hours 09:30-16:00 ET"
            if session == "rth"
            else "Extended hours (pre-market + RTH + after-hours)",
            "forward_fill": forward_fill,
            "forward_fill_description": "Missing minute bars filled with previous close (volume=0)"
            if forward_fill
            else "No fill — raw Polygon data with gaps",
            "raw_bars_from_polygon": raw_bar_count,
            "bars_after_processing": filled_bar_count or bar_count,
            "bars_added_by_fill": (filled_bar_count - raw_bar_count) if forward_fill and filled_bar_count else 0,
        },
        "known_behaviors": {
            "vwap": "Polygon VWAP is a daily rolling VWAP, not per-bar. It accumulates across the session and routinely falls outside a single bar's H/L range. This is correct behavior.",
            "supertrend_nans": "supertl (long/support) is NaN during downtrends; superts (short/resistance) is NaN during uptrends. This is by design — use supert for the main line and supertd for direction.",
            "polygon_0700_contamination": "Polygon includes late-reported settlement trades in minute aggregates around 07:00-07:02 ET, inflating close prices by $4-6 on some bars. TradingView filters these out. This causes EMA/indicator divergence vs TradingView — longer-period EMAs recover more slowly from the contaminated bar.",
            "flat_bars": "Bars where High=Low=Open=Close occur in pre/post market when only 1 trade happens in the minute. Expected in extended hours.",
            "saturday_data": "Some bars may appear on Saturday UTC — these are Friday after-hours trades past midnight UTC. Filtered out when session='rth'.",
        },
        "notes": [
            "All float values are rounded to 6 decimal places",
            "Empty cells indicate NaN (indicator warm-up period or insufficient data)",
            "Timestamps represent the start of each minute bar (bar-open convention)",
            "Data is de-duplicated by timestamp and sorted chronologically",
            "VWAP is a daily rolling accumulation — not bounded by individual bar H/L",
        ],
    }
    return json.dumps(metadata, indent=2).encode("utf-8")


_INDICATOR_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "ema": {
        "": "Exponential Moving Average — weighted moving average giving more weight to recent prices",
    },
    "bbands": {
        "bbl": "Bollinger Lower Band — lower envelope at N standard deviations below the SMA",
        "bbm": "Bollinger Middle Band — simple moving average center line",
        "bbu": "Bollinger Upper Band — upper envelope at N standard deviations above the SMA",
        "bbb": "Bollinger Band Width — (upper - lower) / middle, measures volatility expansion/contraction",
        "bbp": "Bollinger %B — (close - lower) / (upper - lower), shows where price sits within the bands (0=lower, 1=upper)",
    },
    "supertrend": {
        "supert": "Supertrend line — trailing stop that flips between support and resistance based on ATR",
        "supertd": "Supertrend direction — +1 (bullish/uptrend) or -1 (bearish/downtrend)",
        "supertl": "Supertrend long/support level — NaN during downtrends (by design)",
        "superts": "Supertrend short/resistance level — NaN during uptrends (by design)",
    },
    "macd": {
        "macd": "MACD line — difference between fast and slow EMA (momentum direction)",
        "macdh": "MACD histogram — MACD line minus signal line (momentum acceleration)",
        "macds": "MACD signal line — EMA of the MACD line (smoothed trend signal)",
    },
    "rsi": {
        "": "Relative Strength Index — oscillator (0-100) measuring speed of price changes; >70 overbought, <30 oversold",
    },
    "adx": {
        "adx": "Average Directional Index — trend strength (0-100); >25 trending, <20 ranging",
        "dmp": "Plus Directional Indicator (+DI) — bullish pressure strength",
        "dmn": "Minus Directional Indicator (-DI) — bearish pressure strength",
    },
    "atr": {
        "": "Average True Range — volatility measure in price units (not directional)",
    },
    "stoch": {
        "stochk": "Stochastic %K — raw oscillator showing close relative to high-low range",
        "stochd": "Stochastic %D — smoothed %K (signal line)",
    },
    "cci": {
        "": "Commodity Channel Index — deviation from statistical mean; >100 overbought, <-100 oversold",
    },
    "obv": {
        "": "On-Balance Volume — cumulative volume flow (+ on up closes, - on down closes)",
    },
    "vwap": {
        "": "Volume Weighted Average Price — session anchored average price weighted by volume",
    },
}


def _describe_indicator_column(indicator: str, column: str, params: str) -> str:
    """Get a rich description for an indicator column."""
    descs = _INDICATOR_DESCRIPTIONS.get(indicator, {})
    # Try prefix match (e.g. "bbl" matches "bbl_20_2.0_2.0")
    for prefix, desc in descs.items():
        if prefix and column.startswith(prefix):
            return desc
    # Fallback to generic indicator description or default
    if "" in descs:
        return descs[""]
    return f"{indicator} indicator output ({params})"


def build_metadata_csv(
    column_meta: list[dict[str, Any]],
    ohlcv_cols: list[str],
) -> bytes:
    """Generate a CSV describing every column in the dataset."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["column", "type", "source", "indicator", "parameters", "description"])

    base = [
        ("unix_ts", "int", "Polygon.io", "", "", "Unix timestamp in milliseconds (UTC)"),
        ("iso_time", "string", "Derived", "", "", "ISO 8601 datetime (UTC)"),
    ]
    desc_map = {
        "PC": "Previous close — the RTH close (16:00 ET) of the most recently completed "
        "regular trading session at or before this bar. Bars before 16:00 ET reference the "
        "prior trading day's close; bars at or after 16:00 ET reference the same day's "
        "close. Sourced from Polygon daily aggregates.",
        "open": "Opening price of the minute bar",
        "high": "Highest price during the minute bar",
        "low": "Lowest price during the minute bar",
        "close": "Closing price of the minute bar",
        "volume": "Number of shares traded during the minute bar",
        "vwap": "Polygon daily rolling VWAP — accumulates across the session (not per-bar)",
        "transactions": "Number of transactions in the minute bar",
    }
    for col in ohlcv_cols:
        col_type = "int" if col == "transactions" else "float"
        base.append((col, col_type, "Polygon.io", "", "", desc_map.get(col, col)))

    for row in base:
        writer.writerow(row)

    for meta in column_meta:
        desc = _describe_indicator_column(meta["indicator"], meta["column"], meta["params"])
        writer.writerow(
            [
                meta["column"],
                "float",
                "pandas-ta",
                meta["indicator"],
                meta["params"],
                desc,
            ]
        )

    return output.getvalue().encode("utf-8")


def _needs_param(param_names: list[str], *required: str) -> bool:
    return all(any(req in p for p in param_names[:5]) for req in required)


def _fmt(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if isinstance(val, float):
        return f"{val:.6f}"
    return str(val)


def build_metadata_kv_csv(
    ticker: str,
    from_date: str,
    to_date: str,
    bar_count: int,
    session: str = "rth",
    forward_fill: bool = True,
    timespan: str = "minute",
    multiplier: int = 1,
    raw_bar_count: int = 0,
    filled_bar_count: int = 0,
    column_meta: list[dict[str, Any]] | None = None,
) -> bytes:
    """Generate a simple key-value CSV with dataset metadata."""
    import zipfile as _zf  # noqa: F401 — just to verify import

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["key", "value"])

    rows = [
        ("ticker", ticker),
        ("from_date", from_date),
        ("to_date", to_date),
        ("timespan", timespan),
        ("multiplier", str(multiplier)),
        ("session_filter", session),
        ("forward_fill", str(forward_fill).lower()),
        ("bar_count", str(bar_count)),
        ("raw_bars_from_polygon", str(raw_bar_count)),
        ("bars_after_processing", str(filled_bar_count or bar_count)),
        ("generated_at", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("data_source", "Polygon.io (Starter plan)"),
        ("calculation_engine", f"pandas-ta {getattr(ta, 'version', 'unknown')}"),
    ]

    if column_meta:
        for i, meta in enumerate(column_meta, 1):
            rows.append((f"indicator_{i}_name", meta["indicator"]))
            rows.append((f"indicator_{i}_column", meta["column"]))
            rows.append((f"indicator_{i}_params", meta["params"]))

    for key, value in rows:
        writer.writerow([key, value])

    return output.getvalue().encode("utf-8")


def build_zip_bytes(
    df: pd.DataFrame,
    columns: list[str],
    column_meta: list[dict[str, Any]],
    ohlcv_cols: list[str],
    ticker: str,
    from_date: str,
    to_date: str,
    session: str = "rth",
    forward_fill: bool = True,
    timespan: str = "minute",
    multiplier: int = 1,
    raw_bar_count: int = 0,
    filled_bar_count: int = 0,
    trades_csv_bytes: bytes | None = None,
    options_slot_files: dict[str, bytes] | None = None,
    options_companion_report: dict[str, Any] | None = None,
    quality_report_md_bytes: bytes | None = None,
    splits_csv_bytes: bytes | None = None,
    dividends_csv_bytes: bytes | None = None,
    ticker_overview_json_bytes: bytes | None = None,
    news_csv_bytes: bytes | None = None,
    financials_csv_bytes: bytes | None = None,
    stock_trades_csv_bytes: bytes | None = None,
    stock_quotes_csv_bytes: bytes | None = None,
) -> bytes:
    """Pack the dataset bundle into a ZIP.

    Always includes ``dataset.csv``, ``metadata.csv``, ``columns.csv``.
    Optional members (from Data Lab toggles):
      * ``trades.csv`` — legacy raw trade data.
      * ``calls/<slot>.csv`` / ``puts/<slot>.csv`` — per-slot options companion files.
      * ``options_companion_report.json`` — per-day contract selection summary.
      * ``quality_report.md`` — rendered data-quality report.
      * ``splits.csv`` / ``dividends.csv`` — Polygon reference endpoints.
      * ``ticker_overview.json`` — single-object Polygon reference.
      * ``news.csv`` / ``financials.csv`` — Polygon reference endpoints.
      * ``stock_trades.csv`` / ``stock_quotes.csv`` — tick-level reference data.
    """
    import json
    import zipfile

    dataset_csv = build_csv_bytes(df, columns)
    metadata_csv = build_metadata_kv_csv(
        ticker=ticker,
        from_date=from_date,
        to_date=to_date,
        bar_count=len(df),
        session=session,
        forward_fill=forward_fill,
        timespan=timespan,
        multiplier=multiplier,
        raw_bar_count=raw_bar_count,
        filled_bar_count=filled_bar_count,
        column_meta=column_meta,
    )
    columns_csv = build_metadata_csv(column_meta, ohlcv_cols)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dataset.csv", dataset_csv)
        zf.writestr("metadata.csv", metadata_csv)
        zf.writestr("columns.csv", columns_csv)
        if trades_csv_bytes is not None:
            zf.writestr("trades.csv", trades_csv_bytes)
        if options_slot_files:
            for path, payload in options_slot_files.items():
                zf.writestr(path, payload)
        if options_companion_report is not None:
            zf.writestr(
                "options_companion_report.json",
                json.dumps(options_companion_report, indent=2, default=str).encode("utf-8"),
            )
        if quality_report_md_bytes is not None:
            zf.writestr("quality_report.md", quality_report_md_bytes)
        if splits_csv_bytes is not None:
            zf.writestr("splits.csv", splits_csv_bytes)
        if dividends_csv_bytes is not None:
            zf.writestr("dividends.csv", dividends_csv_bytes)
        if ticker_overview_json_bytes is not None:
            zf.writestr("ticker_overview.json", ticker_overview_json_bytes)
        if news_csv_bytes is not None:
            zf.writestr("news.csv", news_csv_bytes)
        if financials_csv_bytes is not None:
            zf.writestr("financials.csv", financials_csv_bytes)
        if stock_trades_csv_bytes is not None:
            zf.writestr("stock_trades.csv", stock_trades_csv_bytes)
        if stock_quotes_csv_bytes is not None:
            zf.writestr("stock_quotes.csv", stock_quotes_csv_bytes)

    return buf.getvalue()

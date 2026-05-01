"""Forward log-return target computation.

The "forward log return" used by the Feature Runner and Signal Engine
is the headline target every IC, quantile, and stage-ladder claim is
built on. Earlier versions of this module conflated three subtly
different concepts:

* "15-minute forward return" (time-based, what the UI label promises)
* "horizon=15 bar offset" (bar-count-based, what the loop actually did)
* "no cross-day contamination" (UTC-date-masked, not session-aware)

When bars are exactly 1-minute and gap-free, the three coincide. Outside
that envelope they diverge silently, which is the worst possible failure
mode for a research target — every downstream metric (IC, NW t-stat,
quantiles, walk-forward Sharpe) inherits the misalignment.

This module makes the contract explicit:

* The horizon is **minutes**. The number of bar offsets to apply is
  derived from the inferred or user-supplied bar spacing.
* Each row's forward window is **timestamp-validated**: the bar at
  ``t + horizon_bars`` must be exactly ``horizon_minutes * 60_000`` ms
  after ``t``. Missing bars, halts, and irregular feeds produce NaN
  rather than a silently-wrong return.
* Trading-day boundaries are **session-aware**: timestamps are
  converted to ``America/New_York`` before extracting the date, so
  bars that cross UTC midnight inside the same US session are not
  artificially separated.
* Inputs are **schema-validated**: required columns, numeric close,
  no inf, no duplicate timestamps. Bad input fails fast with a
  descriptive error.

The result is a ``TargetResult`` that carries the values *plus* the
metadata needed to audit what was actually computed (horizon in
minutes, horizon in bars, bar spacing, timezone, valid ratio,
breakdown of why each invalid row was dropped). The UI/disclosure
surfaces these, so the reader can spot a "wrong target" mismatch
before reading the verdict.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SESSION_TIMEZONE = "America/New_York"
"""US-equity convention. Non-US assets need their own session tz; the
spec disclosure exposes this so a mismatch is obvious."""

DEFAULT_NON_NAN_RATIO_THRESHOLD = 0.70
"""Minimum non-NaN ratio for ``validate_return_series`` to pass.

Tightened from 0.30 in v2 (review feedback): a research target with
30 % valid coverage admits regimes where two thirds of the sample is
unobservable, which makes any IC claim fragile. 70 % is still permissive
for intraday data with end-of-session masking; tighten further per
study where the bar coverage is known."""


@dataclass(frozen=True)
class TargetResult:
    """Forward-return values plus the metadata that authorizes them.

    The metadata is the audit trail: a Stage 1+ verdict that disagrees
    with what the user thought the target was should be obvious from
    the disclosure rather than buried in code. The dataclass is frozen
    so callers can pass it through the pipeline without worrying about
    mid-flight mutation.
    """

    values: pd.Series
    """Forward log returns, **positionally indexed** (0..n-1) on a
    DataFrame sorted by timestamp ascending. This matches the index
    convention used by :class:`TechnicalFeatures.compute_feature`, so
    feature/target Series can be aligned positionally without a
    timestamp join. The accompanying ``timestamps`` Series carries the
    timestamp at each position for any caller that wants to verify
    alignment explicitly."""

    timestamps: pd.Series
    """``timestamp`` (int64 ms UTC) at each position of ``values``.

    Use this to merge target/feature with any other timestamp-indexed
    series instead of trusting positional alignment when the upstream
    ordering might differ."""

    target_name: str = "forward_log_return_15m"
    horizon_minutes: int = 15
    horizon_bars: int = 15
    bar_minutes: int = 1
    timezone: str = SESSION_TIMEZONE

    valid_count: int = 0
    total_count: int = 0
    invalid_reason_counts: dict[str, int] = field(default_factory=dict)
    """Per-reason breakdown of why a row's forward window was rejected.

    Keys are stable identifiers (``cross_session``, ``window_gap``,
    ``non_positive_close``, ``window_runs_off_end``). The UI surfaces
    the dominant reason so a near-zero IC against a feature with 60 %
    NaN forward returns is visibly attributed to data, not signal."""

    @property
    def valid_ratio(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.valid_count / self.total_count


# ─── Public API ───────────────────────────────────────────────────────────


def compute_forward_log_return(
    bars: list[dict],
    horizon_minutes: int = 15,
    bar_minutes: int | None = None,
    timezone: str = SESSION_TIMEZONE,
) -> TargetResult:
    """Time-aware, session-aware forward log return.

    Parameters
    ----------
    bars : list[dict]
        OHLCV bars with at least ``timestamp`` (int64 ms UTC) and
        ``close``. Other columns are passed through.
    horizon_minutes : int
        Forward horizon in **wall-clock minutes**. The number of bar
        offsets to apply is derived from ``bar_minutes``.
    bar_minutes : int | None
        Bar spacing in minutes. When None, inferred from the median
        timestamp delta. Required to be a positive integer divisor of
        ``horizon_minutes``.
    timezone : str
        IANA timezone for session-date masking. Default
        ``America/New_York``; pass another tz when validating non-US
        instruments.

    Returns
    -------
    TargetResult
        Values + audit metadata. Values are NaN where any of:

        * the forward window crosses a session boundary,
        * the bar at ``t + horizon_bars`` is not exactly
          ``horizon_minutes * 60_000`` ms after ``t``,
        * either close is non-positive,
        * the window would run off the end of the series.

    Raises
    ------
    ValueError
        On schema violations (missing columns, duplicate timestamps,
        non-numeric close), or when ``horizon_minutes`` is not a
        positive integer multiple of the inferred ``bar_minutes``.
    """
    df = _validate_and_normalise(bars)

    inferred_bar_minutes = _infer_bar_minutes(df)
    if bar_minutes is None:
        bar_minutes = inferred_bar_minutes
    elif inferred_bar_minutes != bar_minutes:
        logger.warning(
            "[Research] Caller-supplied bar_minutes=%d disagrees with "
            "inferred=%d; using caller value.",
            bar_minutes,
            inferred_bar_minutes,
        )

    if bar_minutes <= 0 or horizon_minutes <= 0:
        raise ValueError(
            f"horizon_minutes ({horizon_minutes}) and bar_minutes "
            f"({bar_minutes}) must both be positive."
        )
    if horizon_minutes % bar_minutes != 0:
        raise ValueError(
            f"horizon_minutes ({horizon_minutes}) is not an integer "
            f"multiple of bar_minutes ({bar_minutes}); refusing to "
            "round silently."
        )

    horizon_bars = horizon_minutes // bar_minutes
    expected_delta_ms = horizon_minutes * 60_000

    # Session date in caller-supplied tz (default NY) — UTC-date masking
    # would mis-classify the last few minutes of the US session.
    ts_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.assign(session_date=ts_utc.dt.tz_convert(timezone).dt.date)

    n = len(df)
    timestamps = df["timestamp"].to_numpy()
    closes = df["close"].to_numpy()
    sessions = df["session_date"].to_numpy()

    forward_returns = np.full(n, np.nan)
    reasons: Counter[str] = Counter()

    for i in range(n):
        j = i + horizon_bars
        if j >= n:
            reasons["window_runs_off_end"] += 1
            continue
        if sessions[i] != sessions[j]:
            reasons["cross_session"] += 1
            continue
        if int(timestamps[j] - timestamps[i]) != expected_delta_ms:
            reasons["window_gap"] += 1
            continue
        if closes[i] <= 0 or closes[j] <= 0:
            reasons["non_positive_close"] += 1
            continue
        forward_returns[i] = float(np.log(closes[j] / closes[i]))

    series = pd.Series(forward_returns, name="forward_log_return")
    timestamps = df["timestamp"].reset_index(drop=True).astype("int64")
    timestamps.name = "timestamp"
    valid_count = int(series.notna().sum())

    target_name = f"forward_log_return_{horizon_minutes}m"

    logger.info(
        "[Research] Forward returns: %d valid / %d total (%.1f%%); "
        "horizon=%dm (%d bars × %dm), tz=%s, drops=%s",
        valid_count,
        n,
        100.0 * valid_count / max(n, 1),
        horizon_minutes,
        horizon_bars,
        bar_minutes,
        timezone,
        dict(reasons),
    )

    return TargetResult(
        values=series,
        timestamps=timestamps,
        target_name=target_name,
        horizon_minutes=horizon_minutes,
        horizon_bars=horizon_bars,
        bar_minutes=bar_minutes,
        timezone=timezone,
        valid_count=valid_count,
        total_count=n,
        invalid_reason_counts=dict(reasons),
    )


def validate_return_series(
    returns: pd.Series,
    min_non_nan_ratio: float = DEFAULT_NON_NAN_RATIO_THRESHOLD,
) -> bool:
    """Sanity-check a return series.

    Fails when:

    * the series is empty,
    * the non-NaN ratio is below ``min_non_nan_ratio`` (default 0.70 —
      tightened from 0.30 in v2 because a 30 %-coverage target makes
      any IC claim regime-dependent and hard to interpret),
    * the dropna'd series has zero or non-finite std (constant or
      degenerate returns).
    """
    if len(returns) == 0:
        logger.warning("[Research] Empty return series.")
        return False

    non_nan_ratio = returns.notna().sum() / len(returns)
    if non_nan_ratio < min_non_nan_ratio:
        logger.warning(
            "[Research] Only %.1f%% non-NaN returns (need ≥ %.0f%%).",
            non_nan_ratio * 100,
            min_non_nan_ratio * 100,
        )
        return False

    clean = returns.dropna()
    if len(clean) == 0:
        logger.warning("[Research] Return series has no valid values.")
        return False

    std = float(clean.std())
    if not np.isfinite(std) or std < 1e-10:
        logger.warning(
            "[Research] Return series has near-zero or non-finite std (%.3e).",
            std,
        )
        return False

    return True


# ─── Internal helpers ─────────────────────────────────────────────────────


def _validate_and_normalise(bars: list[dict]) -> pd.DataFrame:
    """Convert raw ``list[dict]`` bars to a sorted, schema-validated DataFrame.

    Fails fast on:

    * missing required columns,
    * non-numeric ``close`` (after coercion),
    * inf/-inf in ``close``,
    * duplicate timestamps.

    Returns a sorted-by-timestamp DataFrame with no duplicates.
    """
    if not bars:
        raise ValueError("bars is empty; cannot compute forward returns.")

    df = pd.DataFrame(bars)

    required = {"timestamp", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"bars is missing required columns: {sorted(missing)}; "
            f"got {sorted(df.columns)}."
        )

    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="raise").astype("int64")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    if df["close"].replace([np.inf, -np.inf], np.nan).isna().any():
        n_bad = int(df["close"].replace([np.inf, -np.inf], np.nan).isna().sum())
        raise ValueError(
            f"bars contains {n_bad} non-finite/non-numeric close values; "
            "the target pipeline does not silently coerce these."
        )

    if df["timestamp"].duplicated().any():
        n_dups = int(df["timestamp"].duplicated().sum())
        raise ValueError(
            f"bars contains {n_dups} duplicate timestamps; "
            "the target pipeline does not silently dedupe — fix upstream."
        )

    return df.sort_values("timestamp").reset_index(drop=True)


def _infer_bar_minutes(df: pd.DataFrame) -> int:
    """Infer bar spacing in minutes from the median consecutive delta.

    The median is robust to gaps (lunch breaks, halts, weekends) that
    would skew the mean. Uses minute granularity; sub-minute or
    fractional spacings raise.
    """
    if len(df) < 2:
        raise ValueError("Need at least 2 bars to infer bar spacing.")

    deltas_ms = np.diff(df["timestamp"].to_numpy())
    median_delta_ms = float(np.median(deltas_ms))

    if median_delta_ms <= 0 or median_delta_ms % 60_000 != 0:
        raise ValueError(
            f"Median bar delta {median_delta_ms} ms is not a whole-minute "
            "spacing; cannot infer bar_minutes. Pass bar_minutes explicitly."
        )

    return int(median_delta_ms // 60_000)


# ─── Legacy wrapper ───────────────────────────────────────────────────────


def compute_15min_forward_return(
    bars: list[dict],
    horizon: int = 15,
) -> pd.Series:
    """Legacy: bar-offset-based 15-minute forward log return.

    .. deprecated::
        Use :func:`compute_forward_log_return` instead. This wrapper
        treats ``horizon`` as a number of **bar offsets**, which only
        equals 15 minutes when the input is 1-minute bars and gap-free.
        It is kept for back-compat with existing Signal Engine call
        sites and tests; new code must use the time-based API.

    Returns
    -------
    pd.Series
        Forward log returns positionally indexed (0..n-1) on bars
        sorted by timestamp ascending. Length equals the sorted bar
        count.
    """
    df = _validate_and_normalise(bars)
    bar_minutes = _infer_bar_minutes(df)
    horizon_minutes = horizon * bar_minutes
    result = compute_forward_log_return(
        bars=bars,
        horizon_minutes=horizon_minutes,
        bar_minutes=bar_minutes,
    )
    return result.values

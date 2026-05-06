"""IV basis conversion: ACT/365 (calendar) ↔ TRD/252 (trading-day).

Formula: σ_TRD252 = σ_ACT365 · √((D · 252) / (365 · N)) where D = calendar tenor days, N = NYSE trading sessions in [asof, asof+D).
Reference: Practitioner convention — variance accrues only on trading days. NYSE calendar via pandas_market_calendars. docs/references/iv-rv-basis-alignment.md.
Canonical implementation: app/volatility/basis.py
Validated against: NONE — pending (no golden fixture; pending-fixture per registry)

Our IV solver returns ``σ`` on ACT/365 basis (``TTM = calendar_days / 365``,
the market-screen / QuantLib convention). Our realized-vol pipeline annualizes
per-bar variance with √252 — TRD/252 basis. ``vrp.compute_vrp`` subtracts
``iv² − rv²`` directly, so the inputs must share a basis. We convert IV to
TRD/252 at the boundary using the practitioner assumption that variance
accrues only on trading days.

Conversion (per-tenor, per-asof):

    σ²_TRD252 · (N / 252) = σ²_ACT365 · (D / 365)        ← equate total variance
    σ_TRD252 = σ_ACT365 · √((D · 252) / (365 · N))

where ``D`` is the IV's tenor in calendar days and ``N`` is the count of NYSE
trading sessions in the half-open window ``[asof_date, asof_date + D)``.

For SPY IV30 with typical N=21 the factor is ≈0.993 (small downward
correction); in dense-holiday windows N can drop to 19, giving a factor
≈1.044 (upward correction). The sign flips with N — a static √(365/252)
constant would be wrong in both directions.
"""

from __future__ import annotations

import math
from typing import Final

import pandas as pd
import pandas_market_calendars as mcal

from app.volatility.conventions import (
    CALENDAR_DAYS_PER_YEAR,
    TRADING_DAYS_PER_YEAR,
)

_NYSE: Final = mcal.get_calendar("NYSE")


def _et_date(ts: pd.Timestamp) -> pd.Timestamp:
    """Floor a timestamp to its NYSE-relevant calendar date (America/New_York).

    Naive timestamps are taken as **date-like in ET** (caller already knows the
    date they mean — no tz juggling). Tz-aware timestamps are converted to ET
    before flooring; this is the correct path for int64-ms-UTC wire input,
    which enters this module through the int branch and is built as
    ``pd.Timestamp(ms, unit="ms", tz="UTC")``.
    Returns a tz-naive Timestamp at midnight, suitable for ``mcal.schedule``.
    """
    if ts.tzinfo is None:
        return ts.normalize()
    return ts.tz_convert("America/New_York").normalize().tz_localize(None)


def nyse_trading_days_in_window(
    asof: pd.Timestamp | int,
    calendar_days: int,
) -> int:
    """Count NYSE trading sessions in ``[asof_date, asof_date + calendar_days)``.

    Window is half-open on the right: a 30-day option from 2024-03-04 covers
    sessions on 03-04, 03-05, …, up to but not including 04-03. The expiry
    day itself does not contribute forward variance.

    ``asof`` may be a ``pd.Timestamp`` (naive treated as UTC) or an int64
    millisecond-since-epoch UTC integer (the repo's wire format).
    """
    if isinstance(asof, int):
        ts = pd.Timestamp(asof, unit="ms", tz="UTC")
    else:
        ts = asof
    start_date = _et_date(ts)
    end_date_inclusive = start_date + pd.Timedelta(days=calendar_days - 1)
    schedule = _NYSE.schedule(start_date=start_date, end_date=end_date_inclusive)
    return len(schedule)


def convert_iv_act365_to_trading252(
    sigma_act365: float,
    asof: pd.Timestamp | int,
    tenor_calendar_days: int,
) -> float:
    """Convert IV from ACT/365 to TRD/252 basis using NYSE trading-day count."""
    if tenor_calendar_days <= 0:
        raise ValueError(f"tenor_calendar_days must be positive: {tenor_calendar_days}")
    if sigma_act365 < 0:
        raise ValueError(f"sigma_act365 must be non-negative: {sigma_act365}")
    n_trading = nyse_trading_days_in_window(asof, tenor_calendar_days)
    if n_trading <= 0:
        raise ValueError(
            f"no NYSE trading days in [{asof}, +{tenor_calendar_days}d) — empty window"
        )
    factor_sq = (tenor_calendar_days * TRADING_DAYS_PER_YEAR) / (
        CALENDAR_DAYS_PER_YEAR * n_trading
    )
    return sigma_act365 * math.sqrt(factor_sq)


def convert_iv_trading252_to_act365(
    sigma_trd252: float,
    asof: pd.Timestamp | int,
    tenor_calendar_days: int,
) -> float:
    """Inverse of :func:`convert_iv_act365_to_trading252`."""
    if tenor_calendar_days <= 0:
        raise ValueError(f"tenor_calendar_days must be positive: {tenor_calendar_days}")
    if sigma_trd252 < 0:
        raise ValueError(f"sigma_trd252 must be non-negative: {sigma_trd252}")
    n_trading = nyse_trading_days_in_window(asof, tenor_calendar_days)
    if n_trading <= 0:
        raise ValueError(
            f"no NYSE trading days in [{asof}, +{tenor_calendar_days}d) — empty window"
        )
    factor_sq = (CALENDAR_DAYS_PER_YEAR * n_trading) / (
        tenor_calendar_days * TRADING_DAYS_PER_YEAR
    )
    return sigma_trd252 * math.sqrt(factor_sq)

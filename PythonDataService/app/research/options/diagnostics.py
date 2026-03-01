from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MAX_MISSING_PCT = 15.0
MAX_DAY_OVER_DAY_IV_CHANGE = 0.50  # 50% day-over-day change flags discontinuity


@dataclass
class IvDiagnosticsReport:
    valid: bool = False
    missing_pct: float = 0.0
    total_trading_days: int = 0
    valid_iv_days: int = 0
    first_date: str | None = None
    last_date: str | None = None
    gaps: int = 0
    dte_spikes: int = 0
    iv_mean: float | None = None
    iv_std: float | None = None
    iv_min: float | None = None
    iv_max: float | None = None
    iv_skewness: float | None = None
    discontinuities: int = 0
    warnings: list[str] = field(default_factory=list)


def run_iv_diagnostics(iv_data_df: pd.DataFrame) -> IvDiagnosticsReport:
    """Validate IV time series before research.

    Checks:
    - Missing data % (reject if > 15%)
    - DTE time series (check for large DTE spikes)
    - IV distribution (mean, std, min, max, skewness)
    - Day-over-day IV change > 50% (likely data error)
    - Date coverage (total trading days, gaps)
    """
    report = IvDiagnosticsReport()

    if iv_data_df is None or iv_data_df.empty:
        report.warnings.append("Empty IV data — no data to validate")
        return report

    # Date coverage
    report.total_trading_days = len(iv_data_df)
    if "date" not in iv_data_df.columns:
        report.warnings.append("Column 'date' not found in IV data")
        return report

    dates = pd.to_datetime(iv_data_df["date"], errors="coerce")
    valid_dates = dates.dropna()
    if len(valid_dates) == 0:
        report.warnings.append("No valid dates in IV data")
        return report

    report.first_date = valid_dates.min().strftime("%Y-%m-%d")
    report.last_date = valid_dates.max().strftime("%Y-%m-%d")

    # Missing data
    iv_col = "iv_30d_atm"
    if iv_col not in iv_data_df.columns:
        report.warnings.append(f"Column '{iv_col}' not found in IV data")
        return report

    iv_series = iv_data_df[iv_col].astype(float)
    valid_mask = iv_series.notna() & (iv_series > 0)
    report.valid_iv_days = int(valid_mask.sum())
    report.missing_pct = round(
        (1 - report.valid_iv_days / report.total_trading_days) * 100, 2
    ) if report.total_trading_days > 0 else 100.0

    if report.missing_pct > MAX_MISSING_PCT:
        report.warnings.append(
            f"Missing data too high: {report.missing_pct:.1f}% (max {MAX_MISSING_PCT}%)"
        )

    # Gaps (consecutive missing days)
    missing_runs = (~valid_mask).astype(int)
    gap_starts = missing_runs.diff().fillna(0) == 1
    report.gaps = int(gap_starts.sum())

    # IV distribution
    valid_iv = iv_series[valid_mask]
    if len(valid_iv) > 0:
        report.iv_mean = round(float(valid_iv.mean()), 6)
        report.iv_std = round(float(valid_iv.std()), 6)
        report.iv_min = round(float(valid_iv.min()), 6)
        report.iv_max = round(float(valid_iv.max()), 6)
        report.iv_skewness = round(float(valid_iv.skew()), 4)

        # Sanity checks on distribution
        if report.iv_mean < 0.05:
            report.warnings.append(f"IV mean suspiciously low: {report.iv_mean:.4f}")
        if report.iv_mean > 1.5:
            report.warnings.append(f"IV mean suspiciously high: {report.iv_mean:.4f}")

    # Day-over-day discontinuities
    if len(valid_iv) > 1:
        iv_changes = valid_iv.pct_change().abs()
        discontinuity_mask = iv_changes > MAX_DAY_OVER_DAY_IV_CHANGE
        report.discontinuities = int(discontinuity_mask.sum())
        if report.discontinuities > 0:
            report.warnings.append(
                f"{report.discontinuities} day(s) with >50% IV change (possible data errors)"
            )

    # DTE spikes (check for large DTE jumps indicating interpolation instability)
    if "dte_low" in iv_data_df.columns and "dte_high" in iv_data_df.columns:
        dte_low = iv_data_df["dte_low"].dropna()
        dte_high = iv_data_df["dte_high"].dropna()

        if len(dte_low) > 1:
            dte_low_changes = dte_low.diff().abs()
            dte_spikes_low = int((dte_low_changes > 15).sum())
        else:
            dte_spikes_low = 0

        if len(dte_high) > 1:
            dte_high_changes = dte_high.diff().abs()
            dte_spikes_high = int((dte_high_changes > 15).sum())
        else:
            dte_spikes_high = 0

        report.dte_spikes = dte_spikes_low + dte_spikes_high
        if report.dte_spikes > 5:
            report.warnings.append(
                f"{report.dte_spikes} DTE spikes detected (interpolation bracket instability)"
            )

    # Final validity determination
    report.valid = (
        report.missing_pct <= MAX_MISSING_PCT
        and report.valid_iv_days >= 30  # Minimum 30 valid days
        and report.discontinuities <= report.total_trading_days * 0.05  # Max 5% discontinuities
    )

    if report.valid:
        logger.info(
            f"[IV DIAGNOSTICS] PASSED — {report.valid_iv_days} valid days, "
            f"{report.missing_pct:.1f}% missing, mean IV={report.iv_mean:.4f}"
        )
    else:
        logger.warning(
            f"[IV DIAGNOSTICS] FAILED — {len(report.warnings)} warnings: "
            + "; ".join(report.warnings)
        )

    return report

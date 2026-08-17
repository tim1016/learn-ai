"""Metrics unique to the full-data Exhaustive Run study.

Formula: recent_trade_share = recent_completed_trades / all_completed_trades;
  recent_vs_prior_rate_ratio = (recent_count / recent_duration) /
  (prior_count / prior_duration).
Reference: repository-internal protocol contract documented in
  docs/references/spy-ema-exhaustive-run.md.
Canonical implementation: this file.
Validated against: tests/research/exhaustive_run/test_metrics.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.research.exhaustive_run.result import TradeRecencySummary
from app.research.runs.result import RunTrade


def summarize_trade_recency(
    trades: Sequence[RunTrade],
    *,
    study_start_ms: int,
    recent_start_ms: int,
    study_end_ms: int,
) -> TradeRecencySummary:
    """Summarize completed trades by exit time inside one full-data run."""
    if not study_start_ms < recent_start_ms < study_end_ms:
        raise ValueError("recency boundary must be strictly inside the study window")

    in_window = [
        trade
        for trade in trades
        if study_start_ms <= trade.exit_time_ms < study_end_ms
    ]
    recent_count = sum(trade.exit_time_ms >= recent_start_ms for trade in in_window)
    prior_count = len(in_window) - recent_count
    total_count = len(in_window)
    recent_share = recent_count / total_count if total_count else None

    recent_duration = study_end_ms - recent_start_ms
    prior_duration = recent_start_ms - study_start_ms
    prior_rate = prior_count / prior_duration
    rate_ratio = (recent_count / recent_duration) / prior_rate if prior_rate > 0 else None

    return TradeRecencySummary(
        recent_window_start_ms=recent_start_ms,
        recent_window_end_ms=study_end_ms,
        recent_trade_count=recent_count,
        prior_trade_count=prior_count,
        total_trade_count=total_count,
        recent_trade_share=recent_share,
        recent_vs_prior_rate_ratio=rate_ratio,
        last_trade_exit_ms=max((trade.exit_time_ms for trade in in_window), default=None),
    )

"""Pinned tests for Exhaustive Run trade-recency statistics."""

from __future__ import annotations

import pytest

from app.research.exhaustive_run.metrics import summarize_trade_recency
from app.research.runs.result import RunTrade


def _trade(exit_ms: int) -> RunTrade:
    return RunTrade(
        trade_number=exit_ms,
        entry_time_ms=exit_ms - 10,
        entry_price=100.0,
        exit_time_ms=exit_ms,
        exit_price=101.0,
        pnl_pts=1.0,
        pnl_pct=0.01,
        result="WIN",
        bars_held=1,
    )


def test_trade_recency_reports_share_rate_ratio_and_last_exit() -> None:
    summary = summarize_trade_recency(
        [_trade(100), _trade(200), _trade(350), _trade(390)],
        study_start_ms=0,
        recent_start_ms=300,
        study_end_ms=400,
    )

    assert summary.recent_trade_count == 2
    assert summary.prior_trade_count == 2
    assert summary.total_trade_count == 4
    assert summary.recent_trade_share == pytest.approx(0.5, abs=1e-12, rel=0)
    assert summary.recent_vs_prior_rate_ratio == pytest.approx(3.0, abs=1e-12, rel=0)
    assert summary.last_trade_exit_ms == 390


def test_trade_recency_is_explicit_for_an_empty_run() -> None:
    summary = summarize_trade_recency(
        [],
        study_start_ms=0,
        recent_start_ms=300,
        study_end_ms=400,
    )

    assert summary.total_trade_count == 0
    assert summary.recent_trade_share is None
    assert summary.recent_vs_prior_rate_ratio is None
    assert summary.last_trade_exit_ms is None


def test_trade_recency_rejects_a_boundary_outside_the_study() -> None:
    with pytest.raises(ValueError, match="strictly inside"):
        summarize_trade_recency(
            [],
            study_start_ms=0,
            recent_start_ms=400,
            study_end_ms=400,
        )

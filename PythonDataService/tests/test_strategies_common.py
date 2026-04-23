"""Tests for app.services.strategies.common shared strategy helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from app.services.strategies.common import (
    TradeRecord,
    _compute_max_drawdown,
    compute_metrics,
    format_timestamp,
    make_trade,
)


def _trade(num: int, trade_type: str = "Buy", pnl_pct: float = 0.01, cum: float | None = None) -> TradeRecord:
    return TradeRecord(
        trade_number=num,
        trade_type=trade_type,
        entry_timestamp="2024-01-01T14:30:00Z",
        exit_timestamp="2024-01-01T14:45:00Z",
        entry_price=100.0,
        exit_price=100.0 * (1.0 + pnl_pct),
        pnl=pnl_pct * 100.0,
        pnl_pct=pnl_pct,
        cumulative_pnl_pct=cum if cum is not None else pnl_pct,
        signal_reason="test",
    )


def test_compute_metrics_empty_returns_empty_dict():
    assert compute_metrics([]) == {}


def test_compute_metrics_win_rate_profit_factor_expectancy():
    trades = [
        _trade(1, pnl_pct=0.02, cum=0.02),
        _trade(2, pnl_pct=-0.01, cum=0.01),
        _trade(3, pnl_pct=0.03, cum=0.04),
        _trade(4, pnl_pct=-0.02, cum=0.02),
    ]

    metrics = compute_metrics(trades)

    assert metrics["total_trades"] == 4
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5, abs=1e-12, rel=0)
    assert metrics["avg_win_pct"] == pytest.approx(0.025, abs=1e-12, rel=0)
    assert metrics["avg_loss_pct"] == pytest.approx(-0.015, abs=1e-12, rel=0)
    # profit factor = total_win / total_loss = 0.05 / 0.03 = 1.666…
    assert metrics["profit_factor"] == pytest.approx(0.05 / 0.03, abs=1e-9, rel=0)
    # expectancy = final cumulative / number of trades = 0.02 / 4.
    assert metrics["expectancy_per_trade"] == pytest.approx(0.02 / 4, abs=1e-12, rel=0)


def test_compute_metrics_all_wins_gives_zero_win_loss_ratio():
    trades = [_trade(i, pnl_pct=0.01, cum=i * 0.01) for i in (1, 2, 3)]

    metrics = compute_metrics(trades)

    # avg_loss_pct is 0 when there are no losers; win_loss_ratio then 0.
    assert metrics["win_loss_ratio"] == pytest.approx(0.0, abs=1e-12, rel=0)
    # profit_factor is 0 when total_loss is zero (current behavior: no infinity).
    assert metrics["profit_factor"] == pytest.approx(0.0, abs=1e-12, rel=0)


def test_compute_max_drawdown_tracks_peak_trough():
    cum_pnl = [0.0, 0.05, 0.03, 0.08, 0.01, 0.02]  # peak 0.08, trough 0.01 → dd = 0.07

    dd = _compute_max_drawdown(cum_pnl)

    assert dd == pytest.approx(0.07, abs=1e-12, rel=0)


def test_compute_max_drawdown_empty_is_zero():
    assert _compute_max_drawdown([]) == 0.0


def test_format_timestamp_epoch_ms_produces_iso_z_suffix():
    # 2024-01-01 00:00:00 UTC = 1_704_067_200_000 ms.
    iso = format_timestamp(1_704_067_200_000)

    assert iso == "2024-01-01T00:00:00Z"


def test_format_timestamp_naive_datetime_interpreted_as_utc():
    iso = format_timestamp(datetime(2024, 1, 1, 14, 30, 0))

    assert iso == "2024-01-01T14:30:00Z"


def test_format_timestamp_aware_datetime_converted_to_utc():
    from datetime import timedelta, timezone

    et = timezone(timedelta(hours=-4))
    iso = format_timestamp(datetime(2024, 4, 1, 10, 30, 0, tzinfo=et))

    # 10:30 EDT = 14:30 UTC.
    assert iso == "2024-04-01T14:30:00Z"


def test_format_timestamp_string_pass_through():
    assert format_timestamp("2024-01-01T00:00:00Z") == "2024-01-01T00:00:00Z"


def test_make_trade_buy_records_correct_pnl_and_cum():
    entry = pd.Series(
        {
            "timestamp": 1_704_067_200_000,
            "close": 100.0,
        }
    )
    exit_ = pd.Series(
        {
            "timestamp": 1_704_067_800_000,
            "close": 102.0,
        }
    )

    trade = make_trade(
        trade_num=7,
        trade_type="Buy",
        entry_row=entry,
        exit_row=exit_,
        cum_pnl_pct=0.01,
        signal_reason="testing",
    )

    assert trade.trade_number == 7
    assert trade.pnl == pytest.approx(2.0, abs=1e-12, rel=0)
    assert trade.pnl_pct == pytest.approx(0.02, abs=1e-12, rel=0)
    assert trade.cumulative_pnl_pct == pytest.approx(0.03, abs=1e-12, rel=0)
    assert trade.entry_timestamp.endswith("Z")


def test_make_trade_sell_inverts_pnl_direction():
    entry = pd.Series(
        {
            "timestamp": datetime(2024, 1, 1, 14, 30, tzinfo=UTC),
            "close": 102.0,
        }
    )
    exit_ = pd.Series(
        {
            "timestamp": datetime(2024, 1, 1, 14, 45, tzinfo=UTC),
            "close": 100.0,
        }
    )

    trade = make_trade(
        trade_num=1,
        trade_type="Sell",
        entry_row=entry,
        exit_row=exit_,
        cum_pnl_pct=0.0,
        signal_reason="testing",
    )

    # Short: pnl = entry - exit = 2.
    assert trade.pnl == pytest.approx(2.0, abs=1e-12, rel=0)
    assert trade.pnl_pct == pytest.approx(2.0 / 102.0, abs=1e-12, rel=0)

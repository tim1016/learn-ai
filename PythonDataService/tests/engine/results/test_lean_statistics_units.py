"""The Strategy Lab "LEAN native statistics" panel reads dollars, not per-share points (E-1, ruling 8 of #3159).

Reproduction before the fix: a 100-share winner of +$2.00 per share (a real
$200) showed ``trade.total_profit_loss`` 2.0, ``runtime.net_profit`` 2000.0
(``pnl_pct`` x start capital) and ``average_win_rate`` 2e-05. The cases below
are closed-form: every expected value is an exact decimal of the inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from app.engine.results.lean_statistics import compute_lean_statistics
from app.engine.strategy.base import LoggedTrade
from app.services.engine_backtest_service import _format_trade_record

ATOL = 1e-9
START_CAPITAL = 100_000.0


def _ms(day: int, hour: int = 14, minute: int = 30) -> int:
    return int(datetime(2026, 9, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def _daily_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {"timestamp": [_ms(day, 20, 0) for day in range(8, 13)], "close": [100.0, 100.5, 102.0, 101.0, 101.5]}
    )


def _winner(quantity: int = 100) -> LoggedTrade:
    return LoggedTrade(
        entry_time_ms=_ms(9), entry_price=Decimal("100"), exit_time_ms=_ms(10, 19, 55), exit_price=Decimal("102"),
        quantity=quantity, pnl_pts=Decimal("2"), pnl_pct=Decimal("0.02"), result="WIN", signal_reason="exit",
    )


def test_a_trade_record_carries_dollars_not_per_share_points() -> None:
    record = _format_trade_record(1, _winner(quantity=100))

    assert record.pnl == pytest.approx(200.0, abs=ATOL, rel=0)
    assert record.pnl_pct == pytest.approx(0.02, abs=ATOL, rel=0)


def test_the_native_statistics_of_a_100_share_winner_are_in_dollars() -> None:
    trades = [_format_trade_record(1, _winner(quantity=100))]

    stats = compute_lean_statistics(_daily_bars(), trades, start_capital=START_CAPITAL, total_fees=2.0, total_orders=2)

    assert stats.trade.total_profit_loss == pytest.approx(200.0, abs=ATOL, rel=0)
    assert stats.trade.average_profit == pytest.approx(200.0, abs=ATOL, rel=0)
    assert stats.portfolio.average_win_rate == pytest.approx(200.0 / START_CAPITAL, abs=ATOL, rel=0)
    assert stats.fees == pytest.approx(2.0, abs=ATOL, rel=0)
    assert stats.net_profit == pytest.approx(198.0, abs=ATOL, rel=0)  # $200 less $2 of commission
    assert stats.equity == pytest.approx(START_CAPITAL + 198.0, abs=ATOL, rel=0)
    assert stats.total_return == pytest.approx(198.0 / START_CAPITAL, abs=ATOL, rel=0)
    assert stats.total_orders == 2


def test_orders_default_to_an_entry_and_an_exit_per_closed_trade() -> None:
    trades = [_format_trade_record(1, _winner())]

    stats = compute_lean_statistics(_daily_bars(), trades, start_capital=START_CAPITAL)

    assert stats.total_orders == 2
    assert stats.fees == 0.0

"""Behavioral tests for app.engine.edge.trade_simulator."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.engine.edge.trade_simulator import TradeSimConfig, simulate


def _make_bars(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    ts_ms = pd.Index(np.arange(n, dtype=np.int64) * 86_400_000, name="ts")
    arr = np.array(closes, dtype=np.float64)
    return pd.DataFrame({
        "open": arr,
        "high": arr * 1.01,
        "low": arr * 0.99,
        "close": arr,
    }, index=ts_ms)


def test_simulate_emits_no_trades_on_zero_signals():
    bars = _make_bars([100.0, 101.0, 102.0, 103.0])
    sig = pd.Series(0, index=bars.index)
    res = simulate(bars=bars, signals=sig)
    assert res.stats["n_trades"] == 0
    assert res.equity_curve is not None
    assert res.equity_curve["equity"].iloc[-1] == 100_000.0


def test_simulate_long_signal_then_time_stop():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    bars = _make_bars(closes)
    signals = pd.Series([1, 0, 0, 0, 0, 0, 0], index=bars.index)
    cfg = TradeSimConfig(time_stop_bars=3, slippage_pct=0.0,
                         commission_per_unit=0.0, spread_bps_stock=0.0)
    res = simulate(bars=bars, signals=signals, config=cfg)
    assert res.stats["n_trades"] == 1
    trade = res.trades[0]
    assert trade.side == 1
    assert trade.exit_reason == "time_stop"
    # entry at next bar open = 101, exit at bar 4 close = 104; pnl = 3
    assert trade.entry_px == 101.0
    assert trade.exit_px == 104.0
    np.testing.assert_allclose(trade.gross_pnl, 3.0, atol=1e-9)


def test_simulate_opposite_signal_exits_position():
    bars = _make_bars([100.0, 101.0, 102.0, 103.0, 104.0])
    signals = pd.Series([1, 0, -1, 0, 0], index=bars.index)
    cfg = TradeSimConfig(time_stop_bars=10, slippage_pct=0.0,
                         commission_per_unit=0.0, spread_bps_stock=0.0)
    res = simulate(bars=bars, signals=signals, config=cfg)
    assert res.stats["n_trades"] == 1
    assert res.trades[0].exit_reason == "opposite_signal"


def test_simulate_costs_reduce_pnl():
    bars = _make_bars([100.0, 101.0, 102.0, 103.0, 104.0])
    signals = pd.Series([1, 0, 0, 0, 0], index=bars.index)
    cfg_no_cost = TradeSimConfig(time_stop_bars=2, slippage_pct=0.0,
                                 commission_per_unit=0.0, spread_bps_stock=0.0)
    cfg_with_cost = TradeSimConfig(time_stop_bars=2, slippage_pct=0.001,
                                    commission_per_unit=0.005, spread_bps_stock=2.0)
    res_no = simulate(bars=bars, signals=signals, config=cfg_no_cost)
    res_yes = simulate(bars=bars, signals=signals, config=cfg_with_cost)
    assert res_yes.trades[0].net_pnl < res_no.trades[0].net_pnl
    assert res_yes.trades[0].costs > 0


def test_simulate_cost_attribution_sums_correctly():
    bars = _make_bars([100.0, 101.0, 102.0, 103.0])
    signals = pd.Series([1, 0, 0, 0], index=bars.index)
    res = simulate(bars=bars, signals=signals,
                   config=TradeSimConfig(time_stop_bars=2))
    attr = res.cost_attribution
    np.testing.assert_allclose(
        attr["gross_pnl"] - attr["total_costs"], attr["net_pnl"], atol=1e-9
    )

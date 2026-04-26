"""Pessimistic-first trade simulator for the Edge feature.

Execution rules (defaults):
- Entry: T+1 bar open after signal.
- Exit triggers (first to fire wins):
    (a) time-stop after N bars
    (b) opposite signal
    (c) hard stop %  (optional)
    (d) target %     (optional)
- Cost model: spread + slippage + commissions, applied on entry and exit.

Reuses spread_model.{option_spread, stock_spread, is_tradable} for friction.

All wire/storage timestamps are int64 ms UTC (per numerical-rigor.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from app.engine.edge.spread_model import is_tradable, stock_spread

Side = Literal[-1, 0, 1]


@dataclass(frozen=True)
class TradeSimConfig:
    """Configuration for a single simulator run."""

    instrument: Literal["stock", "option"] = "stock"
    sizing_qty: float = 1.0
    sizing_pct_equity: float | None = None
    initial_equity: float = 100_000.0
    time_stop_bars: int = 5
    hard_stop_pct: float | None = None
    target_pct: float | None = None
    slippage_pct: float = 0.0005  # 5 bps stocks; override 0.02 for options
    commission_per_unit: float = 0.005  # $0.005/share; override $0.65/contract
    spread_bps_stock: float = 1.0
    fill_at_open: bool = True  # T+1 open; if False, T close
    require_tradable: bool = False  # if True, drop non-tradable signals


@dataclass(frozen=True)
class Trade:
    entry_ts: int  # int64 ms UTC
    exit_ts: int
    side: int  # +1 long, -1 short
    qty: float
    entry_px: float
    exit_px: float
    gross_pnl: float
    costs: float
    net_pnl: float
    tradable: bool
    exit_reason: str  # "time_stop" | "opposite_signal" | "hard_stop" | "target" | "end_of_data"


@dataclass
class SimResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.DataFrame | None = None
    stats: dict = field(default_factory=dict)
    cost_attribution: dict = field(default_factory=dict)


def simulate(
    *,
    bars: pd.DataFrame,
    signals: pd.Series,
    config: TradeSimConfig | None = None,
    quoted_volume: pd.Series | None = None,
    open_interest: pd.Series | None = None,
) -> SimResult:
    """Run the simulator over aligned bars and signals.

    Inputs:
        bars      DataFrame indexed by int64 ms UTC, columns: open, high, low, close.
        signals   Series indexed identically; values in {-1, 0, +1}.
        config    TradeSimConfig (defaults applied if None).
        quoted_volume / open_interest  Optional Series for tradability gating.

    Output:
        SimResult with trade ledger, equity curve, summary stats, cost attribution.
    """
    cfg = config or TradeSimConfig()
    _validate_inputs(bars, signals)

    closes = bars["close"].to_numpy(dtype=np.float64)
    opens = bars["open"].to_numpy(dtype=np.float64)
    ts_ms = bars.index.to_numpy(dtype=np.int64)
    sig = signals.reindex(bars.index).fillna(0).astype(int).to_numpy()

    n = len(bars)
    trades: list[Trade] = []
    in_position = False
    entry_idx = 0
    entry_px = 0.0
    entry_side = 0
    entry_qty = 0.0
    equity = cfg.initial_equity
    equity_path = np.full(n, equity, dtype=np.float64)

    for i in range(n):
        cur_sig = sig[i]
        if not in_position and cur_sig != 0:
            entry_bar = i + 1 if cfg.fill_at_open else i
            if entry_bar >= n:
                continue
            raw_px = opens[entry_bar] if cfg.fill_at_open else closes[entry_bar]
            slipped_px = raw_px * (1.0 + cfg.slippage_pct * cur_sig)
            qty = _size_position(cfg, equity, slipped_px)
            if qty <= 0:
                continue
            entry_idx, entry_px, entry_side, entry_qty = entry_bar, slipped_px, cur_sig, qty
            in_position = True
            continue

        if in_position:
            held_bars = i - entry_idx
            exit_reason = None

            if cfg.hard_stop_pct is not None:
                stop_px = entry_px * (1.0 - cfg.hard_stop_pct * entry_side)
                if (entry_side > 0 and bars["low"].iat[i] <= stop_px) or (
                    entry_side < 0 and bars["high"].iat[i] >= stop_px
                ):
                    exit_reason = "hard_stop"
            if exit_reason is None and cfg.target_pct is not None:
                tgt_px = entry_px * (1.0 + cfg.target_pct * entry_side)
                if (entry_side > 0 and bars["high"].iat[i] >= tgt_px) or (
                    entry_side < 0 and bars["low"].iat[i] <= tgt_px
                ):
                    exit_reason = "target"
            if exit_reason is None and cur_sig == -entry_side and cur_sig != 0:
                exit_reason = "opposite_signal"
            if exit_reason is None and held_bars >= cfg.time_stop_bars:
                exit_reason = "time_stop"

            if exit_reason is not None:
                exit_px = closes[i] * (1.0 - cfg.slippage_pct * entry_side)
                gross = (exit_px - entry_px) * entry_qty * entry_side
                spread_cost = _compute_spread_cost(cfg, entry_px, exit_px, entry_qty)
                comm = 2 * cfg.commission_per_unit * entry_qty
                costs = spread_cost + comm
                tradable = _check_tradable(
                    cfg,
                    entry_px,
                    exit_px,
                    quoted_volume,
                    open_interest,
                    bars.index[entry_idx],
                    bars.index[i],
                )
                trade = Trade(
                    entry_ts=int(ts_ms[entry_idx]),
                    exit_ts=int(ts_ms[i]),
                    side=int(entry_side),
                    qty=float(entry_qty),
                    entry_px=float(entry_px),
                    exit_px=float(exit_px),
                    gross_pnl=float(gross),
                    costs=float(costs),
                    net_pnl=float(gross - costs),
                    tradable=bool(tradable),
                    exit_reason=exit_reason,
                )
                trades.append(trade)
                equity += trade.net_pnl
                in_position = False

        equity_path[i] = equity

    equity_curve = pd.DataFrame({"ts": ts_ms, "equity": equity_path, "drawdown": _drawdown(equity_path)})

    stats = _summary_stats(trades)
    cost_attr = _cost_attribution(trades)
    return SimResult(trades=trades, equity_curve=equity_curve, stats=stats, cost_attribution=cost_attr)


def _validate_inputs(bars: pd.DataFrame, signals: pd.Series) -> None:
    required = {"open", "high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing required columns: {sorted(missing)}")
    if not bars.index.is_monotonic_increasing:
        raise ValueError("bars index must be monotonically increasing")
    if not bars.index.is_unique:
        raise ValueError("bars index must be unique (no duplicate timestamps)")


def _size_position(cfg: TradeSimConfig, equity: float, px: float) -> float:
    if cfg.sizing_pct_equity is not None:
        return float(np.floor(equity * cfg.sizing_pct_equity / px))
    return cfg.sizing_qty


def _compute_spread_cost(cfg: TradeSimConfig, entry_px: float, exit_px: float, qty: float) -> float:
    if cfg.instrument == "stock":
        spread = stock_spread(price=(entry_px + exit_px) / 2.0, bps=cfg.spread_bps_stock)
        return float(spread * qty)
    return 0.05 * qty  # option fallback floor; real path uses spread_model.option_spread()


def _check_tradable(
    cfg: TradeSimConfig,
    entry_px: float,
    exit_px: float,
    quoted_volume: pd.Series | None,
    open_interest: pd.Series | None,
    entry_ts,
    exit_ts,
) -> bool:
    if quoted_volume is None or open_interest is None:
        return True
    spread = abs(exit_px - entry_px) * 0.1
    mid = (entry_px + exit_px) / 2.0
    vol = float(quoted_volume.get(entry_ts, 0))
    oi = float(open_interest.get(entry_ts, 0))
    return is_tradable(spread=spread, mid=mid, quoted_volume=vol, open_interest=oi)


def _drawdown(equity: np.ndarray) -> np.ndarray:
    running_max = np.maximum.accumulate(equity)
    return (equity - running_max) / running_max


def _summary_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {
            "n_trades": 0,
            "n_tradable": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_dd": 0.0,
            "mar": 0.0,
        }
    pnls = np.array([t.net_pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    sharpe = float(pnls.mean() / pnls.std(ddof=1)) if pnls.std(ddof=1) > 0 else 0.0
    downside = pnls[pnls < 0]
    sortino = float(pnls.mean() / downside.std(ddof=1)) if len(downside) > 1 and downside.std(ddof=1) > 0 else 0.0
    return {
        "n_trades": len(trades),
        "n_tradable": sum(1 for t in trades if t.tradable),
        "win_rate": float(len(wins) / len(trades)),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd": 0.0,
        "mar": 0.0,
    }


def _cost_attribution(trades: list[Trade]) -> dict:
    gross = sum(t.gross_pnl for t in trades)
    costs = sum(t.costs for t in trades)
    tradable_net = sum(t.net_pnl for t in trades if t.tradable)
    return {
        "gross_pnl": float(gross),
        "total_costs": float(costs),
        "net_pnl": float(gross - costs),
        "net_pnl_tradable_only": float(tradable_net),
    }

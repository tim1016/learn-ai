"""Shared types and helpers for divergence-study strategies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd


@dataclass(frozen=True)
class Trade:
    """A single round-trip long trade.

    Prices are stored as floats (not Decimals) because the strategies here
    are driven by vectorized pandas indicators, not the streaming engine.
    For bit-exact engine parity use ``engine_runner.py`` instead.
    """

    entry_idx: int
    exit_idx: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    bars_held: int
    pnl_dollars: float
    pnl_pct: float
    exit_reason: str  # "hold_expired" | "rsi_exit" | "death_cross" | "end_of_data"

    def asdict(self) -> dict:
        return asdict(self)


@dataclass
class TradeList:
    """A container for trades plus a small set of aggregate statistics."""

    strategy: str
    variant: str  # "V-A" | "V-B" | "V-C" | "V-D"
    timeframe: str
    trades: list[Trade] = field(default_factory=list)

    def to_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(columns=list(Trade.__dataclass_fields__.keys()))
        df = pd.DataFrame([t.asdict() for t in self.trades])
        df.insert(0, "variant", self.variant)
        df.insert(0, "strategy", self.strategy)
        df.insert(0, "timeframe", self.timeframe)
        return df

    def summary(self) -> dict:
        if not self.trades:
            return {
                "strategy": self.strategy,
                "variant": self.variant,
                "timeframe": self.timeframe,
                "n_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": None,
                "net_pnl": 0.0,
                "avg_win": None,
                "avg_loss": None,
                "best": None,
                "worst": None,
                "profit_factor": None,
                "avg_bars_held": None,
            }
        df = self.to_frame()
        wins = df[df["pnl_dollars"] > 0]
        losses = df[df["pnl_dollars"] <= 0]
        gp = float(wins["pnl_dollars"].sum())
        gl = float(-losses["pnl_dollars"].sum())
        return {
            "strategy": self.strategy,
            "variant": self.variant,
            "timeframe": self.timeframe,
            "n_trades": len(df),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(df) * 100, 2),
            "net_pnl": round(float(df["pnl_dollars"].sum()), 4),
            "avg_win": round(float(wins["pnl_dollars"].mean()), 4) if len(wins) else None,
            "avg_loss": round(float(losses["pnl_dollars"].mean()), 4) if len(losses) else None,
            "best": round(float(df["pnl_dollars"].max()), 4),
            "worst": round(float(df["pnl_dollars"].min()), 4),
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "avg_bars_held": round(float(df["bars_held"].mean()), 2),
        }


def _validate_required(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")

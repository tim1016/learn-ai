"""Trade-list comparison: signal-flip + timing-shift categorization.

Given two :class:`TradeList`s (e.g., V-A vs V-D), classify every trade
into one of four buckets:

  * ``matched_aligned`` — same entry bar (within ±0).
  * ``matched_shifted`` — same entry within ±N bars but different bar.
  * ``a_only_flip``    — fired in A, no near-match in B.
  * ``b_only_flip``    — fired in B, no near-match in A.

Per-bucket aggregates: count, win rate, mean P&L, total P&L.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

from app.research.divergence.strategies.common import Trade, TradeList


@dataclass
class TradeMatch:
    a: Trade | None
    b: Trade | None
    category: str  # "matched_aligned" | "matched_shifted" | "a_only_flip" | "b_only_flip"
    bar_offset: int = 0


def categorize_trade_lists(
    a: TradeList,
    b: TradeList,
    tolerance_bars: int = 5,
) -> tuple[list[TradeMatch], dict]:
    """Match A's trades against B's by entry-bar proximity.

    Each B trade can be matched at most once. Greedy: for each A trade in
    chronological order, find the closest unmatched B entry within
    ``tolerance_bars``; if found, classify as matched (aligned vs shifted);
    otherwise classify A as ``a_only_flip``. After processing A, any
    remaining unmatched B trades become ``b_only_flip``.
    """
    matches: list[TradeMatch] = []
    used_b: set[int] = set()
    a_trades = sorted(a.trades, key=lambda t: t.entry_idx)
    b_trades = sorted(b.trades, key=lambda t: t.entry_idx)

    for at in a_trades:
        best_b_pos: int | None = None
        best_offset = tolerance_bars + 1
        for j, bt in enumerate(b_trades):
            if j in used_b:
                continue
            off = bt.entry_idx - at.entry_idx
            if abs(off) > tolerance_bars:
                # b_trades is sorted; we can break once we're past tolerance
                # in the increasing direction.
                if off > tolerance_bars:
                    break
                continue
            if abs(off) < abs(best_offset):
                best_offset = off
                best_b_pos = j
        if best_b_pos is None:
            matches.append(TradeMatch(a=at, b=None, category="a_only_flip"))
        else:
            used_b.add(best_b_pos)
            bt = b_trades[best_b_pos]
            cat = "matched_aligned" if best_offset == 0 else "matched_shifted"
            matches.append(TradeMatch(a=at, b=bt, category=cat, bar_offset=best_offset))

    for j, bt in enumerate(b_trades):
        if j not in used_b:
            matches.append(TradeMatch(a=None, b=bt, category="b_only_flip"))

    summary = _summarize(matches, a.variant, b.variant)
    return matches, summary


def _summarize(
    matches: list[TradeMatch],
    a_variant: str,
    b_variant: str,
) -> dict:
    by_cat: dict[str, list[TradeMatch]] = {
        "matched_aligned": [],
        "matched_shifted": [],
        "a_only_flip": [],
        "b_only_flip": [],
    }
    for m in matches:
        by_cat[m.category].append(m)

    def stats_for(trades: Iterable[Trade]) -> dict:
        ts = [t for t in trades if t is not None]
        if not ts:
            return {"n": 0, "win_rate_pct": None, "net_pnl": 0.0, "avg_pnl": None}
        wins = sum(1 for t in ts if t.pnl_dollars > 0)
        net = sum(t.pnl_dollars for t in ts)
        return {
            "n": len(ts),
            "win_rate_pct": round(wins / len(ts) * 100, 2),
            "net_pnl": round(net, 4),
            "avg_pnl": round(net / len(ts), 4),
        }

    out: dict = {
        "a_variant": a_variant,
        "b_variant": b_variant,
        "matched_aligned": stats_for([m.a for m in by_cat["matched_aligned"]]),
        "matched_shifted": stats_for([m.a for m in by_cat["matched_shifted"]]),
        "a_only_flip": stats_for([m.a for m in by_cat["a_only_flip"]]),
        "b_only_flip": stats_for([m.b for m in by_cat["b_only_flip"]]),
    }
    # Add a "shift distribution" summary
    shifts = [m.bar_offset for m in by_cat["matched_shifted"]]
    if shifts:
        s = pd.Series(shifts)
        out["shift_distribution"] = {
            "median_bars": int(s.median()),
            "p95_bars": int(s.abs().quantile(0.95)),
            "max_abs_bars": int(s.abs().max()),
        }
    else:
        out["shift_distribution"] = None
    return out


def matches_to_frame(matches: list[TradeMatch]) -> pd.DataFrame:
    rows = []
    for m in matches:
        row: dict = {"category": m.category, "bar_offset": m.bar_offset}
        if m.a is not None:
            row.update(
                {
                    "a_entry_time": m.a.entry_time,
                    "a_entry_idx": m.a.entry_idx,
                    "a_entry_price": m.a.entry_price,
                    "a_exit_price": m.a.exit_price,
                    "a_pnl": m.a.pnl_dollars,
                    "a_bars": m.a.bars_held,
                }
            )
        if m.b is not None:
            row.update(
                {
                    "b_entry_time": m.b.entry_time,
                    "b_entry_idx": m.b.entry_idx,
                    "b_entry_price": m.b.entry_price,
                    "b_exit_price": m.b.exit_price,
                    "b_pnl": m.b.pnl_dollars,
                    "b_bars": m.b.bars_held,
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)

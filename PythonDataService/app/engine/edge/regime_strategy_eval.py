"""Per-regime evaluation of a trade ledger.

Pure post-hoc analysis: takes a trade ledger from trade_simulator + a regime
label series, returns regime-conditional Sharpe / win-rate / etc.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def partition_by_regime(
    *,
    trades: pd.DataFrame,
    regime_labels: pd.Series,
    label_at_entry: bool = True,
) -> dict[int, dict]:
    """Group trades by regime label and compute per-regime stats.

    Inputs:
        trades         DataFrame with columns: entry_ts, exit_ts, net_pnl.
        regime_labels  Series indexed by int64 ms UTC, values are integer labels.
        label_at_entry If True, classify each trade by the regime at entry_ts.
                       If False, by exit_ts.

    Output:
        {regime_label: {n_trades, win_rate, total_pnl, avg_pnl, sharpe}}
    """
    timestamps = trades["entry_ts"] if label_at_entry else trades["exit_ts"]
    aligned_labels = regime_labels.reindex(timestamps).values

    out: dict[int, dict] = {}
    for label in np.unique(aligned_labels[~pd.isna(aligned_labels)]):
        mask = aligned_labels == label
        bucket = trades[mask]
        pnl = bucket["net_pnl"].to_numpy(dtype=np.float64)
        if pnl.size == 0:
            continue
        std = pnl.std(ddof=1) if pnl.size > 1 else 0.0
        out[int(label)] = {
            "n_trades": int(pnl.size),
            "win_rate": float((pnl > 0).mean()),
            "total_pnl": float(pnl.sum()),
            "avg_pnl": float(pnl.mean()),
            "sharpe": float(pnl.mean() / std) if std > 0 else 0.0,
        }
    return out


def regime_run_lengths(labels: Iterable[int]) -> dict[int, list[int]]:
    """Return lists of run lengths per regime label — useful for persistence diagnostics."""
    arr = np.asarray(list(labels))
    if arr.size == 0:
        return {}
    out: dict[int, list[int]] = {}
    cur_label = int(arr[0])
    cur_run = 1
    for i in range(1, arr.size):
        if arr[i] == cur_label:
            cur_run += 1
        else:
            out.setdefault(cur_label, []).append(cur_run)
            cur_label = int(arr[i])
            cur_run = 1
    out.setdefault(cur_label, []).append(cur_run)
    return out

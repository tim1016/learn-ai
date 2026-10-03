"""How much of a development result rests on its best month or its best trades (#2815, ADR 0074 decision 11).

Formula: with N the run's net profit, profit_m each ET month's net profit
  (``evidence.monthly_results``) and t_i each trade's net profit
  (``trade_net.trade_nets``), the trades ranked best first (ties: the earlier
  entry, then the earlier exit):
  * without the best month = N − max_m profit_m (ties: the earliest month);
  * without the best trades = N − Σ of the first k ranked t_i, where
    k = ⌈n / 20⌉ is 5% of the n trades rounded up, so at least one;
  * both are rounded to the cent before the rule reads them: N and t_i take
    different float paths, so a result that is truly $0 can come out a few
    trillionths either side, and that noise must not decide a stored status;
  * status — concern when either "without" value is ≤ $0, else meets; missing
    when the run was not evaluated, failed, made no trades, or its trades do
    not add up to N within a cent;
  * curve — point i = (i / n, Σ_{j≤i} t_j / N) for i = 0..n over the ranked
    trades, drawn only when N > 0: a share of a loss or of nothing means
    nothing.
The measure informs Compare's decision summary only. It never gates
selection, eligibility, the recommendation or the final test.
Reference: the owner's rule on https://github.com/tim1016/learn-ai/issues/2815
  (2026-10-02), specified in https://github.com/tim1016/learn-ai/issues/2821.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_concentration.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from itertools import accumulate
from typing import Any

from app.research.golden_search.evidence import monthly_results
from app.research.golden_search.selection import Metrics
from app.research.golden_search.trade_net import ReconciledRun, reconciled_run, to_cent

NOT_MEASURED = "Not measured for this study: its evidence was recorded before concentration was measured."
NO_PROFIT_TO_SHARE = "It did not make money over the development period, so there is no profit to take a share of."


def best_count(trades: int) -> int:
    """How many trades make the best 5% of ``trades``: one in twenty, rounded up."""
    return -(-trades // 20)


def _ranked(run: ReconciledRun) -> list[tuple[float, Mapping[str, Any]]]:
    """Each trade's net profit and its record, best first (ties: the earlier entry, then the earlier exit)."""
    order = sorted(range(len(run.trades)), key=lambda i: (-run.nets[i], int(run.trades[i]["entry_ms"]), int(run.trades[i]["exit_ms"])))
    return [(run.nets[i], run.trades[i]) for i in order]


def _missing(reason: str) -> dict[str, Any]:
    return {"status": "missing", "reason": reason}


def concentration(metrics: Metrics | None, detail: Mapping[str, Any] | None, *, commission_per_order: float) -> dict[str, Any]:
    """The measure the evidence stage stores for one candidate's development detail run."""
    run = reconciled_run(metrics, detail, commission_per_order=commission_per_order)
    if isinstance(run, str):
        return _missing(run)
    months = monthly_results(run.daily, run.capital)
    if not months:
        return _missing("The development run recorded no daily equity.")
    best_month = max(months, key=lambda month: month.net_profit)  # the earliest of equal months
    ranked = _ranked(run)
    removed = ranked[: best_count(len(ranked))]
    removed_net = math.fsum(net for net, _ in removed)
    without_month = to_cent(run.net_profit - best_month.net_profit)
    without_trades = to_cent(run.net_profit - removed_net)
    return {
        "status": "concern" if without_month <= 0 or without_trades <= 0 else "meets",
        "net_profit": run.net_profit,
        "trades": len(ranked),
        "best_month": {"month_start_ms": best_month.month_start_ms, "net_profit": best_month.net_profit},
        "without_best_month": without_month,
        "best_trades": [{"entry_ms": int(trade["entry_ms"]), "exit_ms": int(trade["exit_ms"]), "net_profit": net} for net, trade in removed],
        "best_trades_net_profit": removed_net,
        "without_best_trades": without_trades,
    }


def stored_concentration(item: Mapping[str, Any]) -> dict[str, Any]:
    """The measure stored with a candidate's evidence; evidence recorded before the stage measured it reads as not measured."""
    stored = item.get("concentration")
    return dict(stored) if stored is not None else _missing(NOT_MEASURED)


def missing_curve(reason: str) -> dict[str, Any]:
    return {"points": [], "best_count": None, "reason": reason}


def concentration_curve(metrics: Metrics | None, detail: Mapping[str, Any] | None, *, commission_per_order: float) -> dict[str, Any]:
    """The running share of net profit with the trades best first, or why none is drawn."""
    run = reconciled_run(metrics, detail, commission_per_order=commission_per_order)
    if isinstance(run, str):
        return missing_curve(run)
    if run.net_profit <= 0:
        return missing_curve(NO_PROFIT_TO_SHARE)
    ranked = _ranked(run)
    count = len(ranked)
    running = [0.0, *accumulate(net for net, _ in ranked)]
    points = [
        {"trades": i, "share_of_trades": i / count, "share_of_profit": total / run.net_profit, "net_profit": total}
        for i, total in enumerate(running)
    ]
    return {"points": points, "best_count": best_count(count), "reason": None}

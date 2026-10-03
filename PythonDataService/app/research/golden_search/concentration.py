"""How much of a development result rests on its best month or its best trades (#2815, ADR 0074 decision 11).

Formula: with N the run's net profit, profit_m each ET month's net profit
  (``evidence.monthly_results``) and t_i each trade's net profit
  (``trade_net.trade_nets``), the trades ranked best first (ties: the earlier
  entry, then the earlier exit):
  * without the best month = N − max_m profit_m (ties: the earliest month);
  * without the best trades = N − Σ of the first k ranked t_i, where
    k = ⌈n / 20⌉ is 5% of the n trades rounded up, so at least one;
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
from dataclasses import dataclass
from itertools import accumulate
from typing import Any, Literal

from app.research.golden_search.evidence import monthly_results
from app.research.golden_search.selection import Metrics
from app.research.golden_search.trade_net import trade_nets, unreconciled

Status = Literal["meets", "concern", "missing"]

NOT_EVALUATED = "Not evaluated: the study's budget ran out before this candidate's development run."
NO_PROFIT_TO_SHARE = "It did not make money over the development period, so there is no profit to take a share of."


def best_count(trades: int) -> int:
    """How many trades make the best 5% of ``trades``: one in twenty, rounded up."""
    return -(-trades // 20)


@dataclass(frozen=True)
class _Run:
    net_profit: float
    daily: list[tuple[int, float]]
    capital: float
    # Each trade's net profit and its record, best first.
    ranked: list[tuple[float, Mapping[str, Any]]]


def _run(metrics: Metrics | None, detail: Mapping[str, Any] | None, commission_per_order: float) -> _Run | str:
    """The development run with its trades ranked, or why it cannot be measured."""
    if metrics is None:
        return NOT_EVALUATED
    if metrics.status != "completed":
        return "The development run failed, so there is nothing to measure."
    if metrics.net_profit is None:
        return "The development run recorded no net profit."
    if detail is None:
        return "The development run kept no trade list."
    trades: list[Mapping[str, Any]] = list(detail["trades"])
    if not trades:
        return "The development run made no trades."
    nets = trade_nets(trades, commission_per_order)
    reason = unreconciled(nets, metrics.net_profit)
    if reason is not None:
        return reason
    order = sorted(range(len(trades)), key=lambda i: (-nets[i], int(trades[i]["entry_ms"]), int(trades[i]["exit_ms"])))
    return _Run(
        net_profit=metrics.net_profit,
        daily=[(int(ms), float(equity)) for ms, equity in detail["daily_equity"]],
        capital=float(detail["initial_cash"]),
        ranked=[(nets[i], trades[i]) for i in order],
    )


def _missing(reason: str) -> dict[str, Any]:
    return {
        "status": "missing",
        "reason": reason,
        "net_profit": None,
        "trades": 0,
        "best_month": None,
        "without_best_month": None,
        "best_trades": [],
        "best_trades_net_profit": None,
        "without_best_trades": None,
    }


def concentration(metrics: Metrics | None, detail: Mapping[str, Any] | None, *, commission_per_order: float) -> dict[str, Any]:
    """The measure the evidence stage stores for one candidate's development detail run."""
    run = _run(metrics, detail, commission_per_order)
    if isinstance(run, str):
        return _missing(run)
    months = monthly_results(run.daily, run.capital)
    if not months:
        return _missing("The development run recorded no daily equity.")
    best_month = max(months, key=lambda month: month.net_profit)  # the earliest of equal months
    removed = run.ranked[: best_count(len(run.ranked))]
    without_month = run.net_profit - best_month.net_profit
    removed_net = math.fsum(net for net, _ in removed)
    return {
        "status": "concern" if without_month <= 0 or run.net_profit - removed_net <= 0 else "meets",
        "reason": None,
        "net_profit": run.net_profit,
        "trades": len(run.ranked),
        "best_month": {"month_start_ms": best_month.month_start_ms, "net_profit": best_month.net_profit},
        "without_best_month": without_month,
        "best_trades": [{"entry_ms": int(trade["entry_ms"]), "exit_ms": int(trade["exit_ms"]), "net_profit": net} for net, trade in removed],
        "best_trades_net_profit": removed_net,
        "without_best_trades": run.net_profit - removed_net,
    }


def concentration_curve(metrics: Metrics | None, detail: Mapping[str, Any] | None, *, commission_per_order: float) -> dict[str, Any]:
    """The running share of net profit with the trades best first, or why none is drawn."""
    run = _run(metrics, detail, commission_per_order)
    if isinstance(run, str):
        return {"points": [], "best_count": 0, "reason": run}
    count = len(run.ranked)
    if run.net_profit <= 0:
        return {"points": [], "best_count": best_count(count), "reason": NO_PROFIT_TO_SHARE}
    running = [0.0, *accumulate(net for net, _ in run.ranked)]
    points = [
        {"trades": i, "share_of_trades": i / count, "share_of_profit": total / run.net_profit, "net_profit": total}
        for i, total in enumerate(running)
    ]
    return {"points": points, "best_count": best_count(count), "reason": None}

"""The Compare evidence's trade charts: each development trade's net, hold and entry, and what they add up to (#2821).

Formula: over a run whose trades reconcile (``trade_net.reconciled_run``),
  t_i each trade's net profit:
  * running net profit R_k = Σ_{i≤k} t_i, the trades in exit order (ties:
    the earlier entry);
  * decision bars held b_i = #{c ∈ C : entry_i < c ≤ exit_i}, where C holds
    the closes of the strategy's decision bars on the canonical NYSE
    calendar: each session's open + j·s for j = 1..⌊(close − open) / s⌋ at an
    intraday span s (whole bars only, so a half-day holds fewer and an
    overnight gap none), or the session close at a daily cadence. Sessions
    open and close on the half hour, so for a span that divides 30 minutes
    these are the closes of the engine's wall-clock-aligned bars; any other
    span is not counted;
  * histogram — over each t to the cent (so float noise from price × quantity
    never splits trades that net the same), the Freedman–Diaconis width
    h = 2·IQR·n^(−1/3), quartiles linearly interpolated (R type 7), and bins
    [k·h, (k+1)·h) for k from ⌊min t / h⌋ to ⌊max t / h⌋, so $0 is an edge and
    no bin holds both a win and a loss. When IQR = 0, h = (max − min) /
    ⌈log2 n + 1⌉ (Sturges); when h would need more than MAX_BINS bins,
    h = (max − min) / (MAX_BINS − 2), which needs at most MAX_BINS; when every
    trade nets the same, one bin [t, t]. The read names the rule that set h;
  * RSI bands — the gates [g_lo, g_hi] cut at every multiple of
    RSI_BAND_WIDTH strictly between them; a band [a, b) holds the trades
    whose RSI at entry lies in it (the last band also takes b = g_hi), with
    their count and mean t (none for an empty band);
  * entry times — each trade's entry in America/New_York, by weekday and by
    the half hour that contains it; per cell: count, mean t and total t, too
    few below MIN_CELL_TRADES trades. The rows and columns are the weekdays
    and half hours the calendar's sessions over the trades' span cover, plus
    any a trade entered outside them.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2821 "Server work by
  chart" (V28–V32); D. Freedman & P. Diaconis, "On the histogram as a density
  estimator: L2 theory", Z. Wahrscheinlichkeitstheorie verw. Gebiete 57
  (1981) 453–476; H. A. Sturges, "The choice of a class interval", JASA 21
  (1926) 65–66. The decision cadence is the registration's
  ``strategy_bars`` and the RSI gates its strategy view's ``rsi`` band, in
  app/engine/strategy/registry.py.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_trade_charts.py.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import accumulate
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration
from app.engine.strategy.strategy_view import ChartParamRef
from app.lean_sidecar.trading_calendar import SessionWindow, session_windows_ms_utc
from app.research.golden_search.evaluator import EXIT_AT_WINDOW_END
from app.research.golden_search.selection import Metrics
from app.research.golden_search.trade_net import reconciled_run, to_cent
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import ny_datetime

MAX_BINS = 60
RSI_BAND_WIDTH = 5
MIN_CELL_TRADES = 5
#: The trade indicator and strategy-view value that hold the RSI at entry.
RSI_KEY = "rsi"

STRATEGY_EXIT = "Exited by the strategy"
NO_CADENCE = "The strategy's decision cadence could not be read for these settings, so hold times are not counted."
UNALIGNED_CADENCE = "This strategy's decision bars do not line up with the session's half hours, so hold times are not counted."
NO_RSI_GATES = "This strategy has no RSI gates to compare entries against."
NO_SETTINGS = "This candidate's settings could not be read, so its RSI gates are unknown."
NO_RSI_RECORDED = "No trade recorded its RSI at entry."

BinRule = Literal["freedman_diaconis", "sturges", "capped", "single"]

_MINUTE_MS = 60_000
_HALF_HOUR_MS = 30 * _MINUTE_MS
_DAY_MS = 86_400_000
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ── What the strategy declares ───────────────────────────────────────────


def _params(registration: StrategyRegistration, point: Mapping[str, Any]) -> BaseModel | None:
    try:
        return registration.param_schema.model_validate(dict(point))
    except ValidationError:
        # A stored point the current parameter model no longer accepts: its declared values cannot be read.
        return None


def _resolve(registration: StrategyRegistration, point: Mapping[str, Any], value: float | ChartParamRef) -> float | None:
    if not isinstance(value, ChartParamRef):
        return float(value)
    params = _params(registration, point)
    return None if params is None else float(getattr(params, value.field))


def decision_bar_span_ms(strategy_key: str, point: Mapping[str, Any]) -> int | None:
    """The length of the strategy's decision bar at ``point``, or None when it cannot be read."""
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        return None
    cadence = registration.strategy_bars
    multiplier = _resolve(registration, point, cadence.multiplier)
    if multiplier is None:
        return None
    return int(multiplier) * (_MINUTE_MS if cadence.timespan == "minute" else _DAY_MS)


def entry_rsi_gates(strategy_key: str, point: Mapping[str, Any]) -> tuple[float, float] | str:
    """The RSI band the strategy enters inside at ``point``, or why there is none to read."""
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None or registration.strategy_view is None:
        return NO_RSI_GATES
    band = next((value.band for value in registration.strategy_view.values if value.key == RSI_KEY and value.band is not None), None)
    if band is None:
        return NO_RSI_GATES
    low, high = (_resolve(registration, point, bound) for bound in band)
    return NO_SETTINGS if low is None or high is None else (low, high)


# ── Decision bars ────────────────────────────────────────────────────────


def countable(bar_span_ms: int) -> bool:
    """Whether bars of this length can be counted from the calendar: a day or more, or a span that divides 30 minutes."""
    return bar_span_ms >= _DAY_MS or _HALF_HOUR_MS % bar_span_ms == 0


def decision_bar_closes(windows: Sequence[SessionWindow], bar_span_ms: int) -> list[int]:
    """Every decision bar's close over ``windows``, in order; the span must be ``countable``."""
    closes: list[int] = []
    for window in windows:
        if bar_span_ms >= _DAY_MS:
            closes.append(window.close_ms_utc)
            continue
        whole = (window.close_ms_utc - window.open_ms_utc) // bar_span_ms
        closes.extend(window.open_ms_utc + j * bar_span_ms for j in range(1, whole + 1))
    return closes


def bars_held(entry_ms: int, exit_ms: int, closes: Sequence[int]) -> int:
    """How many decision bars close after the entry and by the exit."""
    return bisect_right(closes, exit_ms) - bisect_right(closes, entry_ms)


# ── Histogram ────────────────────────────────────────────────────────────


def _bin(low: float, high: float, nets: Sequence[float]) -> dict[str, Any]:
    return {
        "low": low,
        "high": high,
        "trades": len(nets),
        "wins": sum(1 for net in nets if net > 0),
        "losses": sum(1 for net in nets if net < 0),
    }


def bin_width(nets: Sequence[float]) -> tuple[float, BinRule]:
    """The bin width and the rule that set it, per the module docstring; 0 when every trade nets the same."""
    low, high = min(nets), max(nets)
    if low == high:
        return 0.0, "single"
    count = len(nets)
    q1, _, q3 = statistics.quantiles(nets, n=4, method="inclusive")
    rule: BinRule = "freedman_diaconis"
    width = 2.0 * (q3 - q1) * count ** (-1.0 / 3.0)
    if width <= 0:
        width, rule = (high - low) / math.ceil(math.log2(count) + 1), "sturges"
    if math.floor(high / width) - math.floor(low / width) + 1 > MAX_BINS:
        width, rule = (high - low) / (MAX_BINS - 2), "capped"
    return width, rule


def histogram(nets: Sequence[float]) -> dict[str, Any]:
    """The trades' net profits, to the cent, in bins with $0 as an edge, each with its trades, wins and losses."""
    cents = [to_cent(net) for net in nets]
    width, rule = bin_width(cents)
    if width == 0:
        return {"bin_width": 0.0, "rule": rule, "bins": [_bin(cents[0], cents[0], cents)]}
    members: dict[int, list[float]] = {}
    for net in cents:
        members.setdefault(math.floor(net / width), []).append(net)
    first, last = min(members), max(members)
    return {"bin_width": width, "rule": rule, "bins": [_bin(k * width, (k + 1) * width, members.get(k, [])) for k in range(first, last + 1)]}


# ── RSI bands ────────────────────────────────────────────────────────────


def _band_edges(low: float, high: float) -> list[float]:
    """The gates and every multiple of the band width strictly between them."""
    inner = range(math.floor(low / RSI_BAND_WIDTH) + 1, math.ceil(high / RSI_BAND_WIDTH))
    return [low, *(float(RSI_BAND_WIDTH * k) for k in inner), high]


def rsi_bands(entries: Sequence[tuple[float | None, float]], gates: tuple[float, float]) -> dict[str, Any]:
    """Each band's trade count and mean net profit between the gates; ``entries`` are (RSI at entry, net profit)."""
    low, high = gates
    edges = _band_edges(low, high)
    members: list[list[float]] = [[] for _ in edges[1:]]
    unbanded = 0
    for rsi, net in entries:
        if rsi is None or not low <= rsi <= high:
            unbanded += 1
            continue
        # The last band is closed: an entry at the upper gate passes it.
        members[min(bisect_right(edges, rsi), len(edges) - 1) - 1].append(net)
    bands = [
        {"low": a, "high": b, "trades": len(nets), "mean_net_profit": math.fsum(nets) / len(nets) if nets else None}
        for a, b, nets in zip(edges[:-1], edges[1:], members, strict=True)
    ]
    return {"status": "measured", "gate_low": low, "gate_high": high, "bands": bands, "unbanded": unbanded}


# ── Entry times ──────────────────────────────────────────────────────────


def _half_hour(moment: datetime) -> int:
    """Minutes past ET midnight at the start of the half hour holding ``moment``."""
    return moment.hour * 60 + moment.minute // 30 * 30


def entry_times(entries: Sequence[tuple[int, float]], windows: Sequence[SessionWindow]) -> dict[str, Any]:
    """Count, mean and total net profit by ET weekday and half hour of entry; ``entries`` are (entry ms, net profit)."""
    weekdays: set[int] = set()
    half_hours: set[int] = set()
    for window in windows:
        opened = ny_datetime(window.open_ms_utc)
        weekdays.add(opened.weekday())
        half_hours.update(range(_half_hour(opened), _half_hour(ny_datetime(window.close_ms_utc - 1)) + 1, 30))
    cells: dict[tuple[int, int], list[float]] = {}
    for entry_ms, net in entries:
        entered = ny_datetime(entry_ms)
        weekdays.add(entered.weekday())
        half_hours.add(_half_hour(entered))
        cells.setdefault((entered.weekday(), _half_hour(entered)), []).append(net)
    rows, columns = sorted(weekdays), sorted(half_hours)
    return {
        "weekdays": [_WEEKDAYS[day] for day in rows],
        "half_hours": [f"{minutes // 60:02d}:{minutes % 60:02d}" for minutes in columns],
        "min_trades": MIN_CELL_TRADES,
        "cells": [
            {
                "weekday": rows.index(day),
                "half_hour": columns.index(minutes),
                "trades": len(nets),
                "mean_net_profit": math.fsum(nets) / len(nets),
                "total_net_profit": math.fsum(nets),
                "too_few": len(nets) < MIN_CELL_TRADES,
            }
            for (day, minutes), nets in sorted(cells.items())
        ],
    }


# ── The charts ───────────────────────────────────────────────────────────


def _rsi_at_entry(trade: Mapping[str, Any]) -> float | None:
    value = (trade.get("indicators") or {}).get(RSI_KEY)
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else None


def trade_charts(
    metrics: Metrics | None,
    detail: Mapping[str, Any] | None,
    *,
    commission_per_order: float,
    bar_span_ms: int | None,
    rsi_gates: tuple[float, float] | str,
) -> dict[str, Any]:
    """Each trade's record with its net, running net and bars held, and the histogram, RSI bands and entry-time cells."""
    run = reconciled_run(metrics, detail, commission_per_order=commission_per_order)
    if isinstance(run, str):
        return {"status": "missing", "reason": run}
    order = sorted(range(len(run.trades)), key=lambda i: (int(run.trades[i]["exit_ms"]), int(run.trades[i]["entry_ms"])))
    trades = [run.trades[i] for i in order]
    nets = [run.nets[i] for i in order]
    windows = session_windows_ms_utc(
        et_date_at_ms(min(int(trade["entry_ms"]) for trade in trades)), et_date_at_ms(max(int(trade["exit_ms"]) for trade in trades))
    )
    hold_reason = NO_CADENCE if bar_span_ms is None else None if countable(bar_span_ms) else UNALIGNED_CADENCE
    closes = None if hold_reason is not None or bar_span_ms is None else decision_bar_closes(windows, bar_span_ms)
    at_window_end = [trade.get("exit_reason") == EXIT_AT_WINDOW_END for trade in trades]
    records = [
        {
            "entry_ms": int(trade["entry_ms"]),
            "exit_ms": int(trade["exit_ms"]),
            "entry_price": float(trade["entry_price"]),
            "exit_price": float(trade["exit_price"]),
            "quantity": int(trade["quantity"]),
            "pnl": float(trade["pnl"]),
            "net_profit": net,
            "running_net_profit": running,
            "bars_held": None if closes is None else bars_held(int(trade["entry_ms"]), int(trade["exit_ms"]), closes),
            "entry_rsi": _rsi_at_entry(trade),
            "exit_kind": "window_end" if window_end else "strategy",
            "exit_reason": EXIT_AT_WINDOW_END if window_end else STRATEGY_EXIT,
        }
        for trade, net, running, window_end in zip(trades, nets, accumulate(nets), at_window_end, strict=True)
    ]
    rsis = [(record["entry_rsi"], record["net_profit"]) for record in records]
    if isinstance(rsi_gates, str):
        entry_rsi: dict[str, Any] = {"status": "missing", "reason": rsi_gates}
    elif all(rsi is None for rsi, _ in rsis):
        entry_rsi = {"status": "missing", "reason": NO_RSI_RECORDED}
    else:
        entry_rsi = rsi_bands(rsis, rsi_gates)
    return {
        "status": "measured",
        "bar_span_ms": None if hold_reason is not None else bar_span_ms,
        "hold_reason": hold_reason,
        "trades": records,
        "histogram": histogram(nets),
        "entry_rsi": entry_rsi,
        "entry_times": entry_times([(record["entry_ms"], record["net_profit"]) for record in records], windows),
    }

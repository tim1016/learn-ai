"""The Plan step's charts: a locked study's frozen windows, search space, workload, trade minimums and data coverage (#2821).

Formula, from the frozen protocol and lock receipt:
  * windows — the run-up [data start, development start), development, the
    recent fit's window, each fold's training and test windows (the
    receipt's frozen folds), all forward tests together and the final test.
    Each window's scheduled sessions are the canonical calendar's sessions
    in ``window_dates`` (``trading_session_count``); its trade minimum is
    ``TradeFloors.at`` (the final test's with ``final=True``), and none for the
    run-up and for a single fold's test, which has no minimum of its own;
  * search space — each declared knob, searched over [low, high] at its
    step with ``knob_value_counts`` values and its importance, or held at
    its fixed value; positions in the legal domain
    p(v) = (v − domain low) / (domain high − domain low) for the range's ends,
    the incumbent's value and, for Zoom, the seed's (where it starts and the
    value it keeps in every round);
  * workload — each estimate row's planned maximum against the engine runs
    used so far: the evaluation rows of the steps it plans (the search's
    include its pair audits) and, for the proof, the units it drew outside
    the evaluator; together they equal the study's consumed evaluations;
  * trade minimums — a frequency plan's minimum per window,
    ⌈F × Σ_y selected_y / scheduled_y⌉, with its yearly terms as the receipt
    froze them; a fixed-floor plan's two flat floors;
  * data coverage — for each ET calendar month of [data start, final end):
    the calendar's sessions, and how many the lake catalog holds complete,
    still fetching, stale, failed or not at all (minute trade bars under the
    study's price adjustment, on the active data root); a status it does not
    know fails the read.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2821 "Server work by
  chart" (V1–V6); the floors are app/research/golden_search/activity.py.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_plan_charts.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from app.lean_sidecar.trading_calendar import session_windows_ms_utc, trading_session_count
from app.research.golden_search.activity import TradeFloors, trading_years
from app.research.golden_search.declarations import declaration_for, knob_values, to_decimal
from app.research.golden_search.models import StudyRow
from app.research.golden_search.protocol import GoldenSearchProtocol, knob_value_counts, recent_window_ms
from app.research.grid_search.service import window_dates
from app.utils.session_anchors import et_midnight_ms

# The catalog's artifact statuses (``data_lake.types.ArtifactStatus``), and "missing" for a session it has no row for.
COVERAGE_STATUSES = ("complete", "fetching", "stale", "failed", "missing")


def _sessions(start_ms: int, end_ms: int) -> int:
    return trading_session_count(*window_dates(start_ms, end_ms))


def _window(key: str, label: str, kind: str, start_ms: int, end_ms: int, minimum: int | None) -> dict[str, Any]:
    return {"key": key, "label": label, "kind": kind, "start_ms": start_ms, "end_ms": end_ms, "sessions": _sessions(start_ms, end_ms), "minimum_trades": minimum}


def windows(row: StudyRow, protocol: GoldenSearchProtocol, floors: TradeFloors) -> list[dict[str, Any]]:
    """Every window the frozen plan evaluates, in time order within its kind."""
    intervals = row.receipt["intervals"]
    folds = list(row.receipt.get("folds", []))
    development = (protocol.development_start_ms, protocol.development_end_ms)
    result = [
        _window("run_up", "Run-up before development", "run_up", int(intervals["data_start_ms"]), protocol.development_start_ms, None),
        _window("development", "Development", "development", *development, floors.at(development)),
    ]
    if protocol.recent_window:
        recent = recent_window_ms(protocol)
        result.append(_window("recent", "Recent fit", "recent", *recent, floors.at(recent)))
    for fold in folds:
        index = int(fold["fold_index"])
        training = (int(fold["train_start_ms"]), int(fold["train_end_ms"]))
        result.append(_window(f"training_{index}", f"Fold {index + 1} training", "training", *training, floors.at(training)))
        result.append(_window(f"test_{index}", f"Fold {index + 1} test", "test", int(fold["test_start_ms"]), int(fold["test_end_ms"]), None))
    if folds:
        forward = (int(folds[0]["test_start_ms"]), int(folds[-1]["test_end_ms"]))
        result.append(_window("forward", "All forward tests", "forward", *forward, floors.at(forward)))
    final = (protocol.final_start_ms, protocol.final_end_ms)
    result.append(_window("final", "Final test", "final", *final, floors.at(final, final=True)))
    return result


def _position(value: Decimal, low: Decimal, high: Decimal) -> float | None:
    return None if high == low else float((value - low) / (high - low))


def search_space(row: StudyRow, protocol: GoldenSearchProtocol) -> list[dict[str, Any]]:
    """Each declared knob in search order: its range or held value, its value count and importance, and the incumbent's value."""
    declaration = declaration_for(row.strategy_key)
    if declaration is None:
        return []
    counts = knob_value_counts(protocol)
    incumbent = knob_values(declaration, protocol.incumbent.params)
    # Only Zoom has a starting point; Grid scores every combination.
    start = knob_values(declaration, protocol.seed) if protocol.method == "zoom" else None
    knobs = []
    for plan in protocol.knobs:
        knob = declaration.knob(plan.name)
        searched = plan.mode == "search"
        low = to_decimal(plan.low if searched else plan.fixed_value)
        high = to_decimal(plan.high if searched else plan.fixed_value)
        knobs.append(
            {
                "name": knob.name,
                "label": knob.label,
                "unit": knob.unit,
                "searched": searched,
                "low": float(low),
                "high": float(high),
                "step": plan.step if searched else None,
                "values": counts.get(plan.name),
                "importance": plan.importance,
                "current": float(incumbent[knob.name]),
                "domain_low": float(knob.domain_low),
                "domain_high": float(knob.domain_high),
                "low_position": _position(low, knob.domain_low, knob.domain_high),
                "high_position": _position(high, knob.domain_low, knob.domain_high),
                "current_position": _position(incumbent[knob.name], knob.domain_low, knob.domain_high),
                "start": None if start is None else float(start[knob.name]),
                "start_position": None if start is None else _position(start[knob.name], knob.domain_low, knob.domain_high),
            }
        )
    return knobs


# The estimate row (``budget.estimate``) that plans each evaluation step's runs.
ESTIMATE_ROW_OF_STEP = {"search": "search", "pair_audit": "search", "recent": "recent", "validation": "validation", "evidence": "evidence", "exam": "exam"}


def runs_used(rows_by_step: Mapping[str, int], proof_units: int) -> dict[str, int]:
    """Engine runs used per estimate row: each step's evaluation rows under the row that plans them, and the proof's units drawn outside the evaluator."""
    used = {"proof": proof_units}
    for step, count in rows_by_step.items():
        if step not in ESTIMATE_ROW_OF_STEP:
            raise ValueError(f"Evaluation step {step!r} has no row in the study's estimate.")
        used[ESTIMATE_ROW_OF_STEP[step]] = used.get(ESTIMATE_ROW_OF_STEP[step], 0) + count
    return used


def workload(row: StudyRow, used: Mapping[str, int]) -> dict[str, Any]:
    """Each estimate row's planned maximum against the engine runs it has used so far, within the study's cap."""
    estimate = row.receipt["estimate"]
    return {
        "cap": row.budget_cap,
        "consumed": row.consumed_evaluations,
        "planned_total": int(estimate["total_max"]),
        "stages": [
            {"stage": item["stage"], "label": item["label"], "planned": int(item["max_evaluations"]), "used": int(used.get(item["stage"], 0))}
            for item in estimate["stages"]
        ],
    }


def minimums(row: StudyRow, protocol: GoldenSearchProtocol) -> dict[str, Any]:
    """A frequency plan's minimum per window with its yearly terms, or a fixed-floor plan's two floors."""
    activity = row.receipt.get("activity")
    if activity is None:
        return {
            "expected_trades_per_year": None,
            "windows": [
                {"key": "selection", "label": "Each selection window", "minimum_trades": protocol.policy.min_trades, "trading_years": None, "years": []},
                {"key": "final", "label": "Final test", "minimum_trades": protocol.exam_min_trades, "trading_years": None, "years": []},
            ],
        }
    return {
        "expected_trades_per_year": int(activity["expected_trades_per_year"]),
        "windows": [
            {
                "key": item["key"],
                "label": item["label"],
                "minimum_trades": int(item["minimum_trades"]),
                "trading_years": float(trading_years(item["years"])),
                "years": [dict(year) for year in item["years"]],
            }
            for item in activity["windows"]
        ],
    }


def coverage(row: StudyRow, protocol: GoldenSearchProtocol, statuses: Mapping[date, str] | str) -> dict[str, Any]:
    """Each ET month of the data span: its scheduled sessions and their lake status; ``statuses`` is the catalog's by date, or why it could not be read."""
    if isinstance(statuses, str):
        return {"status": "missing", "reason": statuses}
    start, end = window_dates(int(row.receipt["intervals"]["data_start_ms"]), protocol.final_end_ms)
    months: dict[tuple[int, int], dict[str, int]] = {}
    for window in session_windows_ms_utc(start, end):
        day = window.session_date
        counts = months.setdefault((day.year, day.month), dict.fromkeys(COVERAGE_STATUSES, 0))
        status = statuses.get(day, "missing")
        if status not in counts:
            raise ValueError(f"The lake catalog reports status {status!r} for {day}, which the coverage chart does not know.")
        counts[status] += 1
    return {
        "status": "measured",
        "months": [
            {"month_start_ms": et_midnight_ms(date(year, month, 1)), "year": year, "month": month, "sessions": sum(counts.values()), **counts}
            for (year, month), counts in sorted(months.items())
        ],
    }


def coverage_span(row: StudyRow) -> tuple[date, date]:
    """The ET dates the coverage chart reads: the study's whole data span."""
    protocol = GoldenSearchProtocol.from_dict(row.protocol)
    return window_dates(int(row.receipt["intervals"]["data_start_ms"]), protocol.final_end_ms)


def plan_charts(row: StudyRow, *, used: Mapping[str, int], statuses: Mapping[date, str] | str) -> dict[str, Any]:
    """The Plan step's charts for a locked study."""
    protocol = GoldenSearchProtocol.from_dict(row.protocol)
    floors = TradeFloors(protocol, row.receipt)
    return {
        "windows": windows(row, protocol, floors),
        "search_space": search_space(row, protocol),
        "workload": workload(row, used),
        "minimums": minimums(row, protocol),
        "coverage": coverage(row, protocol, statuses),
    }

"""Candidate evidence: daily equity, drawdown, performance by month, neighborhoods, pair maps, and the recommendation.

Formula:
  * daily equity — the engine's per-bar ``equity_curve`` (``timestamp`` int ms,
    strictly increasing) reduced to the LAST point of each America/New_York
    date, anchored at that date's scheduled NYSE session close (canonical
    calendar, so half-days close early).
  * drawdown — ``d_t = E_t / max_{s<=t} E_s − 1`` (a fraction, ``<= 0``) over
    the daily points.
  * performance by month — for each ET calendar month with a daily point,
    ``profit_m = E(last point of m) − E(last point of the previous month)``,
    the first month measured from the starting capital, and
    ``return_m = profit_m / that base``; ``trades_m`` counts trades whose
    exit falls in ``m``. Months are labelled by their ET-midnight start, and by
    their ET year and month for the month calendar.
  * neighborhood — the center and the settings one neighbor step below and
    above it on one knob, each tested, failed, untested, invalid or outside
    the domain; one-sided when exactly one neighbor was tested.
  * recommendation — a closed map from findings to sentences; a headline
    names the stronger fitted candidate under the frozen objective and the
    weightiest caution. No score, no crown, deterministic.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Compare
  candidates and weaknesses".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_evidence.py (hand-computed
  values, ``atol=1e-9, rtol=0``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.research.golden_search.grid_procedure import AuditCell, PairGrid
from app.research.golden_search.protocol import SelectionPolicy
from app.research.golden_search.selection import Candidate, Metrics, best, ineligibility
from app.research.walk_forward_study.verdict import Verdict
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

# ── Equity, drawdown and months ─────────────────────────────────────────


def daily_equity(equity_curve: Sequence[Mapping[str, Any]]) -> list[tuple[int, float]]:
    """``(session close ms, equity)`` for the last equity point of each ET date; refuses unordered timestamps."""
    last_by_day: dict[date, float] = {}
    previous: int | None = None
    for row in equity_curve:
        timestamp = row["timestamp"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError(f"equity timestamp {timestamp!r} is not an int ms UTC")
        if previous is not None and timestamp <= previous:
            raise ValueError(f"equity timestamps must strictly increase; {timestamp} follows {previous}")
        previous = timestamp
        last_by_day[et_date_at_ms(timestamp)] = float(row["equity"])
    daily: list[tuple[int, float]] = []
    for day, equity in last_by_day.items():
        try:
            close_ms = session_close_ms_utc(day)
        except LookupError as exc:
            raise ValueError(f"an equity point falls on {day.isoformat()}, which is not a NYSE session") from exc
        daily.append((close_ms, equity))
    return daily


def drawdown_series(daily: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """``(ms, equity / running peak − 1)``; refuses a non-positive peak, where drawdown is undefined."""
    series: list[tuple[int, float]] = []
    peak = -math.inf
    for ms, equity in daily:
        peak = max(peak, equity)
        if peak <= 0:
            raise ValueError(f"drawdown is undefined while peak equity is {peak}")
        series.append((ms, equity / peak - 1.0))
    return series


@dataclass(frozen=True)
class MonthlyResult:
    month_start_ms: int
    year: int
    month: int
    net_profit: float
    return_fraction: float | None
    trades: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "month_start_ms": self.month_start_ms,
            "year": self.year,
            "month": self.month,
            "net_profit": self.net_profit,
            "return_fraction": self.return_fraction,
            "trades": self.trades,
        }


def _month_of(ms: int) -> tuple[int, int]:
    day = et_date_at_ms(ms)
    return day.year, day.month


def monthly_results(
    daily: Sequence[tuple[int, float]],
    initial_cash: float,
    trade_exit_ms: Sequence[int] = (),
) -> list[MonthlyResult]:
    """Profit, return and closed trades for each ET calendar month the daily equity covers."""
    month_end: dict[tuple[int, int], float] = {}
    for ms, equity in daily:
        month_end[_month_of(ms)] = equity
    trades: dict[tuple[int, int], int] = dict.fromkeys(month_end, 0)
    for exit_ms in trade_exit_ms:
        month = _month_of(exit_ms)
        if month not in trades:
            raise ValueError(f"a trade exits at {exit_ms}, outside every month the equity curve covers")
        trades[month] += 1
    results: list[MonthlyResult] = []
    base = initial_cash
    for (year, month), equity in month_end.items():
        profit = equity - base
        results.append(
            MonthlyResult(
                month_start_ms=et_midnight_ms(date(year, month, 1)),
                year=year,
                month=month,
                net_profit=profit,
                return_fraction=profit / base if base > 0 else None,
                trades=trades[(year, month)],
            )
        )
        base = equity
    return results


# ── Neighborhoods and pair maps ─────────────────────────────────────────

CellStatus = Literal["center", "tested", "failed", "untested", "invalid", "outside_domain"]


@dataclass(frozen=True)
class NeighborRow:
    value: float
    status: CellStatus
    metrics: Metrics | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "status": self.status,
            "metrics": None if self.metrics is None else self.metrics.as_dict(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Neighborhood:
    knob: str
    rows: tuple[NeighborRow, ...]
    one_sided: bool

    @property
    def losing_neighbors(self) -> tuple[NeighborRow, ...]:
        return tuple(
            row
            for row in self.rows
            if row.status == "tested" and row.metrics is not None and row.metrics.net_profit is not None and row.metrics.net_profit < 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {"knob": self.knob, "rows": [row.as_dict() for row in self.rows], "one_sided": self.one_sided}


def _audit_status(cell: AuditCell, metrics_by_hash: Mapping[str, Metrics]) -> tuple[CellStatus, Metrics | None]:
    if cell.status != "testable":
        return cell.status, None
    assert cell.point_hash is not None  # a testable cell always has its point
    metrics = metrics_by_hash.get(cell.point_hash)
    if metrics is None:
        return "untested", None
    return ("tested" if metrics.status == "completed" else "failed"), metrics


def neighborhood(
    knob: str,
    center_value: float,
    center_metrics: Metrics | None,
    probes: tuple[AuditCell, AuditCell],
    metrics_by_hash: Mapping[str, Metrics],
) -> Neighborhood:
    """The center and its two neighbors on ``knob`` (``probes`` from ``grid_procedure.neighbor_probes``)."""
    below, above = probes
    rows = []
    for cell in (below, above):
        status, metrics = _audit_status(cell, metrics_by_hash)
        rows.append(NeighborRow(value=cell.values[knob], status=status, metrics=metrics, reason=cell.reason))
    center = NeighborRow(value=center_value, status="center", metrics=center_metrics, reason=None)
    tested = sum(1 for row in rows if row.status == "tested")
    return Neighborhood(knob=knob, rows=(rows[0], center, rows[1]), one_sided=tested == 1)


def pair_map(grid: PairGrid, metrics_by_hash: Mapping[str, Metrics]) -> dict[str, Any]:
    """The parameter map a pair audit shows: every cell's status and, when tested, its metrics."""
    cells = []
    for cell in grid.cells:
        status, metrics = _audit_status(cell, metrics_by_hash)
        cells.append(
            {
                "x": cell.values[grid.x_knob],
                "y": cell.values[grid.y_knob],
                "status": status,
                "metrics": None if metrics is None else metrics.as_dict(),
                "reason": cell.reason,
            }
        )
    return {
        "x_knob": grid.x_knob,
        "y_knob": grid.y_knob,
        "x_values": list(grid.x_values),
        "y_values": list(grid.y_values),
        "cells": cells,
    }


# ── Candidates and the recommendation ───────────────────────────────────

CandidateKey = Literal["incumbent", "all_period", "recent"]
CANDIDATE_LABELS: dict[CandidateKey, str] = {
    "incumbent": "Current settings",
    "all_period": "All-period fit",
    "recent": "Recent fit",
}
FindingCode = Literal[
    "SAME_AS_INCUMBENT",
    "NEIGHBORS_LOSE_MONEY",
    "EDGE_OF_RANGE",
    "VALIDATION_STOPPED_WORKING",
    "VALIDATION_GOT_WORSE",
    "VALIDATION_NOT_JUDGED",
    "TOO_FEW_TRADES",
    "RECENT_DIFFERS_FROM_ALL_PERIOD",
    "STRESS_TURNS_NEGATIVE",
    "NONE_ELIGIBLE",
]


@dataclass(frozen=True)
class StressResult:
    scenario: str
    label: str
    metrics: Metrics | None


@dataclass(frozen=True)
class CandidateEvidence:
    key: CandidateKey
    point: dict[str, Any]
    point_hash: str
    metrics: Metrics | None
    neighborhoods: tuple[Neighborhood, ...] = ()
    stress: tuple[StressResult, ...] = ()
    edge_hits: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return CANDIDATE_LABELS[self.key]


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    text: str
    candidate: CandidateKey | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "text": self.text}


@dataclass(frozen=True)
class Recommendation:
    headline: str
    findings: tuple[Finding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"headline": self.headline, "findings": [finding.as_dict() for finding in self.findings]}


def same_as(candidates: Sequence[CandidateEvidence]) -> dict[CandidateKey, list[CandidateKey]]:
    """For each candidate, the other candidates with the identical point."""
    return {
        candidate.key: [other.key for other in candidates if other.key != candidate.key and other.point_hash == candidate.point_hash]
        for candidate in candidates
    }


_OBJECTIVE_VERB = {
    "sharpe_ratio": "has the higher Sharpe ratio",
    "total_return_pct": "returns more",
    "net_profit": "earns more",
}
# Weightiest first: the caution the headline names, and the evidence to inspect. A lead candidate
# is eligible, so a candidate's TOO_FEW_TRADES finding can never be its caution.
_CAUTIONS: tuple[tuple[FindingCode, str, str], ...] = (
    ("VALIDATION_STOPPED_WORKING", "the search procedure stopped working in later test periods", "test history"),
    ("NEIGHBORS_LOSE_MONEY", "nearby settings lose money", "sensitivity"),
    ("STRESS_TURNS_NEGATIVE", "extra costs erase its profit", "cost sensitivity"),
    ("VALIDATION_GOT_WORSE", "the search procedure got worse in later test periods", "test history"),
    ("EDGE_OF_RANGE", "it sits at the edge of the searched range", "range"),
    ("VALIDATION_NOT_JUDGED", "the search procedure could not be judged over time", "test history"),
)
_STUDY_WIDE: frozenset[FindingCode] = frozenset(
    {"VALIDATION_STOPPED_WORKING", "VALIDATION_GOT_WORSE", "VALIDATION_NOT_JUDGED"}
)


def _validation_finding(verdict: Verdict | None) -> Finding | None:
    if verdict is None:
        return Finding("VALIDATION_NOT_JUDGED", "The search procedure has not been tested over time.")
    if verdict.label == "stopped working":
        return Finding(
            "VALIDATION_STOPPED_WORKING",
            f"Tested over time, the search procedure stopped working: {verdict.reason} ({verdict.based_on}).",
        )
    if verdict.label == "got worse":
        return Finding(
            "VALIDATION_GOT_WORSE",
            f"Tested over time, the search procedure got worse: {verdict.reason} ({verdict.based_on}).",
        )
    if verdict.label in ("could not be judged", "too few trades"):
        return Finding("VALIDATION_NOT_JUDGED", f"The search procedure could not be judged over time: {verdict.reason}.")
    return None


def _candidate_findings(candidate: CandidateEvidence, policy: SelectionPolicy) -> list[Finding]:
    findings: list[Finding] = []
    label = candidate.label
    if candidate.metrics is not None and ineligibility(candidate.metrics, policy) in ("NO_TRADES", "TOO_FEW_TRADES"):
        findings.append(
            Finding(
                "TOO_FEW_TRADES",
                f"The {label.lower()} made {candidate.metrics.total_trades} trades, fewer than the development period's minimum of {policy.min_trades}.",
                candidate.key,
            )
        )
    losing = sorted({hood.knob for hood in candidate.neighborhoods if hood.losing_neighbors})
    if losing:
        findings.append(
            Finding(
                "NEIGHBORS_LOSE_MONEY",
                f"Settings one step away from the {label.lower()} lose money ({', '.join(losing)}).",
                candidate.key,
            )
        )
    if candidate.edge_hits:
        findings.append(
            Finding(
                "EDGE_OF_RANGE",
                f"The {label.lower()} sits at the edge of the searched range for {', '.join(candidate.edge_hits)}; "
                "a better setting may lie outside it.",
                candidate.key,
            )
        )
    base_net = None if candidate.metrics is None else candidate.metrics.net_profit
    if base_net is not None and base_net > 0:
        erased = [
            result.label
            for result in candidate.stress
            if result.metrics is not None and result.metrics.net_profit is not None and result.metrics.net_profit <= 0
        ]
        if erased:
            findings.append(
                Finding(
                    "STRESS_TURNS_NEGATIVE",
                    f"The {label.lower()} stops making money under: {', '.join(erased)}.",
                    candidate.key,
                )
            )
    return findings


def recommendation(
    candidates: Sequence[CandidateEvidence],
    validation_verdict: Verdict | None,
    policy: SelectionPolicy,
) -> Recommendation:
    """The headline and findings for the candidate comparison; never a score or a winner crown."""
    by_key = {candidate.key: candidate for candidate in candidates}
    incumbent = by_key.get("incumbent")
    fitted: list[CandidateEvidence] = []
    for key in ("all_period", "recent"):
        candidate = by_key.get(key)
        if candidate is not None and all(candidate.point_hash != other.point_hash for other in fitted):
            fitted.append(candidate)
    findings: list[Finding] = []
    for candidate in fitted:
        if incumbent is not None and candidate.point_hash == incumbent.point_hash:
            findings.append(Finding("SAME_AS_INCUMBENT", f"The {candidate.label.lower()} is the same as the current settings.", candidate.key))
    if "all_period" in by_key and "recent" in by_key and by_key["all_period"].point_hash != by_key["recent"].point_hash:
        findings.append(
            Finding(
                "RECENT_DIFFERS_FROM_ALL_PERIOD",
                "The recent fit and the all-period fit chose different settings, so the choice depends on the period.",
            )
        )
    for candidate in fitted:
        findings.extend(_candidate_findings(candidate, policy))
    validation = _validation_finding(validation_verdict)
    if validation is not None:
        findings.append(validation)
    eligible = [
        candidate
        for candidate in fitted
        if candidate.metrics is not None and ineligibility(candidate.metrics, policy) is None
    ]
    if not eligible:
        findings.append(Finding("NONE_ELIGIBLE", "No searched candidate meets your rules on the development data."))
        return Recommendation(
            headline="No searched candidate meets your rules on the development data. Keep the current settings, or revise the plan as a new study.",
            findings=tuple(findings),
        )
    new = [candidate for candidate in eligible if incumbent is None or candidate.point_hash != incumbent.point_hash]
    if not new:
        return Recommendation(
            headline="The search returned the current settings. A final test would only re-check them; keeping the current settings is a reasonable choice.",
            findings=tuple(findings),
        )
    ranked = best(
        [Candidate(point_hash=candidate.point_hash, point=candidate.point, metrics=candidate.metrics) for candidate in new if candidate.metrics is not None],
        policy,
    )
    assert ranked is not None  # every candidate in ``new`` is eligible
    lead = next(candidate for candidate in new if candidate.point_hash == ranked.point_hash)
    if len(new) > 1:
        opening = f"The {lead.label.lower()} {_OBJECTIVE_VERB[policy.objective]} in this replay"
    elif len(fitted) > 1:
        opening = f"The {lead.label.lower()} is the only new candidate that meets your rules"
    else:
        opening = f"The {lead.label.lower()} meets your rules in this replay"
    present = {(finding.code, finding.candidate) for finding in findings}
    for code, caution, subject in _CAUTIONS:
        if (code, None if code in _STUDY_WIDE else lead.key) in present:
            return Recommendation(
                headline=f"{opening}, but {caution}. Inspect that {subject} before using your final test.",
                findings=tuple(findings),
            )
    return Recommendation(headline=f"{opening}. Compare the evidence before using your final test.", findings=tuple(findings))

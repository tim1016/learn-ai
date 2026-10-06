"""Every run of a study, grouped by what asked for it, and the daily curve each kept (#2930).

Formula, for a run's stored equity ``E_t`` at each session close ``t`` and the
study's starting capital ``C``:
  * cumulative return ``r_t = E_t / C − 1``;
  * fall from peak ``d_t = E_t / max_{s ≤ t} E_s − 1``
    (``evidence.drawdown_series``); undefined, and read as none, while the
    peak is not above 0.
These are the two series a candidate's detail read draws
(``views.run_detail``). A run recorded before runs kept their curve has
none: it reads as not recorded, never as a flat line.

A run belongs to the group of the step that first asked for it; a point a
later step asks for again is answered from that first run:
  search, pair_audit, recent   the evaluation's own step
  fold_training, fold_test     a walk-forward run, told apart by its fold's
                               test window
  candidate, neighbor, stress  an evidence run: a candidate's detail run,
                               a base-cost neighbor, or a cost scenario
  final                        a final-test run
Reference: PRD https://github.com/tim1016/learn-ai/issues/2930 "Every run
  keeps a daily equity curve".
Canonical implementation: evidence.py for the two series; this file groups,
  names and orders the runs.
Validated against: tests/research/golden_search/test_run_curves.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from app.research.golden_search.declarations import SearchDeclaration, point_hash
from app.research.golden_search.evidence import CANDIDATE_LABELS, drawdown_series
from app.research.golden_search.guidance import params_sentence
from app.research.golden_search.models import EvaluationRecord, StudyRow
from app.research.golden_search.procedure_history import FoldWindow
from app.research.golden_search.protocol import BASE_SCENARIO

RunGroup = Literal["search", "pair_audit", "recent", "fold_training", "fold_test", "candidate", "neighbor", "stress", "final"]
RunSort = Literal["recorded", "total_return_pct", "sharpe_ratio", "max_drawdown_pct", "total_trades", "net_profit"]

RUN_GROUPS: tuple[RunGroup, ...] = get_args(RunGroup)
RUN_SORTS: tuple[RunSort, ...] = get_args(RunSort)
GROUP_LABELS: dict[RunGroup, str] = {
    "search": "All-period search",
    "pair_audit": "Pair audit",
    "recent": "Recent fit search",
    "fold_training": "Fold training",
    "fold_test": "Fold test",
    "candidate": "Candidate evidence",
    "neighbor": "Neighbor check",
    "stress": "Cost stress",
    "final": "Final test",
}
_GROUP_OF_STEP: dict[str, RunGroup] = {"search": "search", "pair_audit": "pair_audit", "recent": "recent", "exam": "final"}
#: The most curves one read returns: an overlay past this is unreadable, and the page asks again for more.
MAX_CURVES = 200


def group_of(record: EvaluationRecord, folds: Mapping[int, FoldWindow]) -> RunGroup:
    """The group of the step that first asked for ``record``'s run."""
    if record.stage in _GROUP_OF_STEP:
        return _GROUP_OF_STEP[record.stage]
    if record.stage == "validation":
        fold = None if record.fold_index is None else folds.get(record.fold_index)
        tested = fold is not None and (record.window_start_ms, record.window_end_ms) == (fold.test_start_ms, fold.test_end_ms)
        return "fold_test" if tested else "fold_training"
    if record.stage == "evidence":
        if record.scenario != BASE_SCENARIO:
            return "stress"
        return "candidate" if record.detail else "neighbor"
    raise ValueError(f"evaluation {record.evaluation_key} was asked for by an unknown step {record.stage!r}")


@dataclass(frozen=True)
class NamedPoints:
    """The names a study gives to settings: the current settings and each fit anywhere, a fold's winner inside that fold."""

    everywhere: Mapping[str, tuple[str, ...]]
    fold_winner: Mapping[int, str]

    def of(self, record: EvaluationRecord) -> list[str]:
        names = list(self.everywhere.get(record.point_hash, ()))
        if record.fold_index is not None and self.fold_winner.get(record.fold_index) == record.point_hash:
            names.append("Fold winner")
        return names


def named_points(row: StudyRow) -> NamedPoints:
    everywhere: dict[str, list[str]] = {}

    def name(point: Mapping[str, Any], label: str) -> None:
        everywhere.setdefault(point_hash(row.strategy_key, dict(point)), []).append(label)

    name(row.protocol["incumbent"]["params"], CANDIDATE_LABELS["incumbent"])
    for step, key in (("search", "all_period"), ("recent", "recent")):
        if step in row.results:
            name(row.results[step]["procedure"]["winner"], CANDIDATE_LABELS[key])
    winners = {
        int(fold["fold_index"]): point_hash(row.strategy_key, dict(fold["winner"]))
        for fold in row.results.get("validation", {}).get("folds", [])
        if fold.get("winner") is not None
    }
    return NamedPoints(everywhere={digest: tuple(labels) for digest, labels in everywhere.items()}, fold_winner=winners)


def _metric(record: EvaluationRecord, key: str) -> float | None:
    value = getattr(record, key)
    return None if value is None or not math.isfinite(value) else float(value)


def run_view(
    record: EvaluationRecord,
    *,
    group: RunGroup,
    names: Sequence[str],
    scenario_labels: Mapping[str, str],
    declaration: SearchDeclaration,
) -> dict[str, Any]:
    """One run as the All runs page lists it: what asked for it, its settings, its window and its numbers."""
    parts = [GROUP_LABELS[group] if record.fold_index is None else f"Fold {record.fold_index + 1} {'test' if group == 'fold_test' else 'training'}"]
    if record.scenario != BASE_SCENARIO:
        parts.append(scenario_labels.get(record.scenario, record.scenario))
    parts.extend(names)
    return {
        "evaluation_key": record.evaluation_key,
        "group": group,
        "group_label": GROUP_LABELS[group],
        "fold_index": record.fold_index,
        "label": " · ".join(parts),
        "names": list(names),
        "point": dict(record.point),
        "params_sentence": params_sentence(declaration, record.point),
        "window_start_ms": record.window_start_ms,
        "window_end_ms": record.window_end_ms,
        "scenario": record.scenario,
        "status": record.status,
        "total_trades": record.total_trades,
        "net_profit": _metric(record, "net_profit"),
        "total_return_pct": _metric(record, "total_return_pct"),
        "sharpe_ratio": _metric(record, "sharpe_ratio"),
        "max_drawdown_pct": _metric(record, "max_drawdown_pct"),
        "error": record.error,
        "has_curve": record.has_curve,
    }


def runs_page(
    row: StudyRow,
    records: Sequence[EvaluationRecord],
    *,
    folds: Sequence[FoldWindow],
    declaration: SearchDeclaration,
    group: RunGroup | None = None,
    fold_index: int | None = None,
    sort: RunSort = "recorded",
    descending: bool = False,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """The study's runs in one group (or all), sorted, one page of them, beside every group's counts.

    ``records`` arrive in recorded order. A run without the sorted measure
    sorts last whichever way the sort runs.
    """
    if page < 1 or not 1 <= page_size <= 500:
        raise ValueError("page must be positive and page_size between 1 and 500")
    by_fold = {fold.fold_index: fold for fold in folds}
    names = named_points(row)
    scenario_labels = {item["key"]: item["label"] for item in row.protocol.get("stress", [])}
    grouped = [(group_of(record, by_fold), record) for record in records]
    counts = {key: [0, 0] for key in RUN_GROUPS}
    for key, record in grouped:
        counts[key][0] += 1
        counts[key][1] += record.has_curve
    chosen = [(key, record) for key, record in grouped if (group is None or key == group) and (fold_index is None or record.fold_index == fold_index)]
    if sort != "recorded":
        measured = [item for item in chosen if _metric(item[1], sort) is not None]
        unmeasured = [item for item in chosen if _metric(item[1], sort) is None]
        measured.sort(key=lambda item: _metric(item[1], sort) or 0.0, reverse=descending)
        chosen = measured + unmeasured
    elif descending:
        chosen = chosen[::-1]
    start = (page - 1) * page_size
    return {
        "total": len(chosen),
        "page": page,
        "page_size": page_size,
        "groups": [{"key": key, "label": GROUP_LABELS[key], "runs": counts[key][0], "with_curve": counts[key][1]} for key in RUN_GROUPS if counts[key][0] > 0],
        "rows": [
            run_view(record, group=key, names=names.of(record), scenario_labels=scenario_labels, declaration=declaration)
            for key, record in chosen[start : start + page_size]
        ],
    }


def curve_view(evaluation_key: str, daily: Sequence[tuple[int, float]], capital: float) -> dict[str, Any]:
    """One run's stored equity as its cumulative return and its fall from peak at each session close."""
    try:
        falls: list[float] | None = [value for _, value in drawdown_series(daily)]
    except ValueError:
        # The run's peak equity never rose above 0, so a fall from it has no meaning.
        falls = None
    return {
        "evaluation_key": evaluation_key,
        "close_ms": [ms for ms, _ in daily],
        "cumulative_return": [equity / capital - 1.0 for _, equity in daily],
        "drawdown": falls,
    }


def curves_view(evaluation_keys: Sequence[str], stored: Mapping[str, Sequence[tuple[int, float]]], capital: float) -> dict[str, Any]:
    """The curves of the named runs, in the order asked, and the runs among them that kept none."""
    return {
        "capital": capital,
        "curves": [curve_view(key, stored[key], capital) for key in evaluation_keys if key in stored],
        "not_recorded": [key for key in evaluation_keys if key not in stored],
    }

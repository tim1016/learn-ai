"""Every run of a study, grouped by what asked for it, and the daily curve each kept (#2930).

Formula, for a run's stored equity ``E_t`` at each session close ``t`` and the
study's starting capital ``C``:
  * cumulative return ``r_t = E_t / C − 1``;
  * fall from peak ``d_t = E_t / max_{s ≤ t} E_s − 1``; undefined, and read
    as none, while the peak is not above 0;
  * deepest fall ``min_t d_t``, at a session close: the engine's
    ``max_drawdown_pct`` reads every bar and can be deeper.
The two series are the ones a candidate's detail read draws
(``views.run_detail``), from the same functions. A run recorded before runs kept their curve has
none: it reads as not recorded, never as a flat line. A read sends its
session closes once and each curve against them (``app/research/session_curves.py``).

A run belongs to the group of the step that first asked for it; a point a
later step asks for again is answered from that first run:
  search, pair_audit, recent   the evaluation's own step
  fold_training, fold_test     a walk-forward run, told apart by its fold's
                               test window
  candidate, neighbor, stress  an evidence run: a candidate's detail run,
                               a base-cost neighbor, or a cost scenario
  final                        a final-test run

A step draws one set of runs, each line with a role that says how to draw it:
  search, recent   every point the procedure scored (its group), the winner
                   among them, and the incumbent on the procedure's window —
                   from the procedure if its run kept a curve, else from
                   whichever step's did; a note says so when none did
  fold_tests       for each fold, its winner's and the incumbent's run on
                   the fold's test window
  candidate        one candidate's development run, its cost-stress runs,
                   and the settings one neighbor step either side on each
                   searched knob that its audit tested, each found by
                   the value the audit recorded
                   (``grid_procedure.neighbor_at``)
A named run is found by what it is — settings, window, base costs — never
by which step asked first, because an earlier step may have scored it. A
set returns every named line and at most ``MAX_OTHER_LINES`` of a search's
other points, spread evenly over the order they were scored; the rest are
counted, never dropped silently.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2930 "Every run
  keeps a daily equity curve".
Canonical implementation: app/research/sweep/daily_equity.py for the two
  series; this file groups, names and orders the runs.
Validated against: tests/research/golden_search/test_run_curves.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from app.research.golden_search.declarations import SearchDeclaration, point_hash
from app.research.golden_search.evidence import CANDIDATE_LABELS
from app.research.golden_search.grid_procedure import neighbor_at
from app.research.golden_search.guidance import params_sentence
from app.research.golden_search.models import EvaluationRecord, StudyRow
from app.research.golden_search.procedure_history import FoldWindow
from app.research.golden_search.protocol import BASE_SCENARIO
from app.research.session_curves import session_curves

RunGroup = Literal["search", "pair_audit", "recent", "fold_training", "fold_test", "candidate", "neighbor", "stress", "final"]
RunSort = Literal["recorded", "total_return_pct", "sharpe_ratio", "max_drawdown_pct", "total_trades"]
CurveSet = Literal["search", "recent", "fold_tests", "candidate"]
#: How a step draws a line: the set's chosen settings, the frozen incumbent, a candidate's own run, a cost
#: scenario or a neighbor of it, or one of the many other points a search scored.
LineRole = Literal["winner", "incumbent", "candidate", "stress", "neighbor", "other"]

RUN_GROUPS: tuple[RunGroup, ...] = get_args(RunGroup)
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
#: The most of a search's other points one step's set returns; a search can score more than a chart can show.
MAX_OTHER_LINES = 100


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
    """One run as the All runs page lists it: what asked for it, its settings, its window and its numbers.

    ``label`` with ``params_sentence`` tells any two runs of a study apart: a
    step scores a set of settings once per window, fold and cost scenario.
    """
    parts = [GROUP_LABELS[group] if record.fold_index is None else f"Fold {record.fold_index + 1} {'test' if group == 'fold_test' else 'training'}"]
    if record.scenario != BASE_SCENARIO:
        parts.append(scenario_labels.get(record.scenario, record.scenario))
    parts.extend(names)
    return {
        "evaluation_key": record.evaluation_key,
        "label": " · ".join(parts),
        "params_sentence": params_sentence(declaration, record.point),
        "window_start_ms": record.window_start_ms,
        "window_end_ms": record.window_end_ms,
        "status": record.status,
        "total_trades": record.total_trades,
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
    # Per group: every run, the completed ones, and those that kept a curve.
    counts = {key: [0, 0, 0] for key in RUN_GROUPS}
    for key, record in grouped:
        counts[key][0] += 1
        counts[key][1] += record.status == "completed"
        counts[key][2] += record.has_curve
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
        "sort": sort,
        "folds": len(folds),
        "groups": [
            {"key": key, "label": GROUP_LABELS[key], "runs": counts[key][0], "completed": counts[key][1], "with_curve": counts[key][2]}
            for key in RUN_GROUPS
            if counts[key][0] > 0
        ],
        "rows": [
            run_view(record, group=key, names=names.of(record), scenario_labels=scenario_labels, declaration=declaration)
            for key, record in chosen[start : start + page_size]
        ],
    }


# ── A step's set of lines ────────────────────────────────────────────────

Window = tuple[int, int]


@dataclass(frozen=True)
class SetLine:
    """One run of a step's set: how to draw it and what to call it there."""

    record: EvaluationRecord
    role: LineRole
    label: str
    #: The run's settings are the current settings, whatever its role: a winner can be the incumbent.
    incumbent: bool = False


@dataclass(frozen=True)
class ChosenLines:
    """A step's set as chosen: the lines to draw, what the cap left out, and what the set could not show."""

    lines: tuple[SetLine, ...]
    left_out: int
    not_recorded: int
    failed: int
    notes: tuple[str, ...]


class _BaseRuns:
    """A study's base-cost runs by what they are: settings and window, whichever step asked first."""

    def __init__(self, records: Sequence[EvaluationRecord]) -> None:
        self._runs: dict[tuple[str, Window], EvaluationRecord] = {}
        for record in records:
            if record.scenario != BASE_SCENARIO:
                continue
            key = (record.point_hash, (record.window_start_ms, record.window_end_ms))
            held = self._runs.get(key)
            # A summary run and a detail run of one point hold the same curve; keep the first that kept one.
            if held is None or (record.has_curve and not held.has_curve):
                self._runs[key] = record

    def of(self, point_digest: str, window: Window) -> EvaluationRecord | None:
        return self._runs.get((point_digest, window))


def _incumbent_hash(row: StudyRow) -> str:
    return point_hash(row.strategy_key, dict(row.protocol["incumbent"]["params"]))


def _procedure_lines(row: StudyRow, records: Sequence[EvaluationRecord], step: Literal["search", "recent"], folds: Mapping[int, FoldWindow]) -> tuple[list[SetLine], list[str]]:
    """Every point one search procedure scored, its winner among them, and the incumbent on its window."""
    stored = row.results.get(step)
    if stored is None:
        return [], []
    incumbent, winner = _incumbent_hash(row), point_hash(row.strategy_key, dict(stored["procedure"]["winner"]))
    lines: list[SetLine] = []
    for record in records:
        if group_of(record, folds) != step:
            continue
        if record.point_hash == winner:
            lines.append(SetLine(record, "winner", "Winner · current settings" if winner == incumbent else "Winner", incumbent=winner == incumbent))
        elif record.point_hash == incumbent:
            lines.append(SetLine(record, "incumbent", CANDIDATE_LABELS["incumbent"], incumbent=True))
        else:
            lines.append(SetLine(record, "other", "Scored point"))
    notes: list[str] = []
    own = next((line for line in lines if line.role == "incumbent"), None)
    if incumbent != winner and (own is None or not own.record.has_curve):
        # The procedure started elsewhere (a qualified incumbent is never the seed), or its own run of the
        # incumbent kept no curve: take the incumbent's run from whichever step kept one on this window.
        window = (int(stored["window"]["start_ms"]), int(stored["window"]["end_ms"]))
        elsewhere = _BaseRuns(records).of(incumbent, window)
        if elsewhere is not None and elsewhere.has_curve:
            lines = [line for line in lines if line is not own]
            lines.append(SetLine(elsewhere, "incumbent", CANDIDATE_LABELS["incumbent"], incumbent=True))
        elif own is None and elsewhere is None:
            notes.append("The current settings were not run on this window, so they have no line here.")
        else:
            notes.append("The current settings’ run on this window kept no curve, so they have no line here.")
    return lines, notes


def _fold_test_lines(row: StudyRow, records: Sequence[EvaluationRecord], folds: Mapping[int, FoldWindow]) -> list[SetLine]:
    """Each fold's winner and the incumbent on that fold's test window, fold by fold."""
    runs, incumbent = _BaseRuns(records), _incumbent_hash(row)
    lines: list[SetLine] = []
    for stored in row.results.get("validation", {}).get("folds", []):
        fold = folds.get(int(stored["fold_index"]))
        if fold is None:
            continue
        window, name = (fold.test_start_ms, fold.test_end_ms), f"Fold {fold.fold_index + 1}"
        winner = None if stored.get("winner") is None else point_hash(row.strategy_key, dict(stored["winner"]))
        tested = None if winner is None else runs.of(winner, window)
        if tested is not None:
            lines.append(SetLine(tested, "winner", f"{name} winner · current settings" if winner == incumbent else f"{name} winner", incumbent=winner == incumbent))
        benchmark = runs.of(incumbent, window)
        if benchmark is not None and winner != incumbent:
            lines.append(SetLine(benchmark, "incumbent", f"{name} current settings", incumbent=True))
    return lines


def _candidate_lines(row: StudyRow, records: Sequence[EvaluationRecord], candidate_key: str, declaration: SearchDeclaration) -> list[SetLine]:
    """One candidate's development run, its cost-stress runs and the one-step neighbors its audit tested."""
    stored = next((item for item in (row.results.get("evidence") or {}).get("candidates", []) if item["key"] == candidate_key), None)
    if stored is None:
        return []
    development = (int(row.protocol["development_start_ms"]), int(row.protocol["development_end_ms"]))
    scenario_labels = {item["key"]: item["label"] for item in row.protocol.get("stress", [])}
    runs = _BaseRuns(records)
    lines: list[SetLine] = []
    own = runs.of(stored["point_hash"], development)
    if own is not None:
        lines.append(SetLine(own, "candidate", CANDIDATE_LABELS[stored["key"]], incumbent=stored["point_hash"] == _incumbent_hash(row)))
    for record in records:
        stressed = record.scenario != BASE_SCENARIO and record.point_hash == stored["point_hash"]
        if stressed and (record.window_start_ms, record.window_end_ms) == development:
            lines.append(SetLine(record, "stress", scenario_labels.get(record.scenario, record.scenario)))
    # A neighbor is drawn only where the audit tested it, and is found by the value the audit recorded: a nearby
    # point the search happened to score is not an audit, and the knob's step may have changed since.
    for hood in stored.get("neighbors", []):
        knob = declaration.knob(hood["knob"])
        below, _center, above = hood["rows"]
        for side, audited in (("below", below), ("above", above)):
            if audited["status"] != "tested":
                continue
            cell = neighbor_at(declaration, stored["point"], knob.name, audited["value"])
            neighbor = None if cell.point_hash is None else runs.of(cell.point_hash, development)
            if neighbor is not None:
                lines.append(SetLine(neighbor, "neighbor", f"{knob.label} one step {side} ({audited['value']:g})"))
    return lines


def _within_cap(lines: Sequence[SetLine], notes: Sequence[str]) -> ChosenLines:
    """Keep every named line; of a search's other points, at most the cap, spread evenly over the order they were scored."""
    completed = [line for line in lines if line.record.status == "completed"]
    drawable = [line for line in completed if line.record.has_curve]
    others = [index for index, line in enumerate(drawable) if line.role == "other"]
    if len(others) > MAX_OTHER_LINES:
        kept = {others[(position * len(others)) // MAX_OTHER_LINES] for position in range(MAX_OTHER_LINES)}
        dropped = set(others) - kept
        drawable = [line for index, line in enumerate(drawable) if index not in dropped]
    return ChosenLines(
        lines=tuple(drawable),
        left_out=max(0, len(others) - MAX_OTHER_LINES),
        not_recorded=len(completed) - sum(line.record.has_curve for line in completed),
        failed=sum(line.record.status == "failed" for line in lines),
        notes=tuple(notes),
    )


def curve_set_lines(
    row: StudyRow,
    records: Sequence[EvaluationRecord],
    *,
    folds: Sequence[FoldWindow],
    declaration: SearchDeclaration,
    curve_set: CurveSet,
    candidate_key: str | None = None,
) -> ChosenLines:
    """The runs one step draws, in drawing order. ``records`` arrive in recorded order.

    A run still pending, or one that failed, has no curve and is not counted
    as one that kept none: ``not_recorded`` is completed runs only, and
    ``failed`` counts the failures, so a set of failed runs is not read as
    one nothing has run for yet.
    """
    by_fold = {fold.fold_index: fold for fold in folds}
    if curve_set == "search" or curve_set == "recent":
        return _within_cap(*_procedure_lines(row, records, curve_set, by_fold))
    if curve_set == "fold_tests":
        return _within_cap(_fold_test_lines(row, records, by_fold), ())
    if candidate_key is None:
        raise ValueError("a candidate's set needs the candidate")
    return _within_cap(_candidate_lines(row, records, candidate_key, declaration), ())


def curve_set_view(row: StudyRow, chosen: ChosenLines, stored: Mapping[str, Sequence[tuple[int, float]]], *, declaration: SearchDeclaration) -> dict[str, Any]:
    """A step's set as the chart reads it: each line's role, label, settings and curve."""
    drawn = [line for line in chosen.lines if line.record.evaluation_key in stored]
    sessions, curves = session_curves({line.record.evaluation_key: stored[line.record.evaluation_key] for line in drawn}, float(row.protocol["execution"]["initial_cash"]))
    return {
        "sessions_ms": sessions,
        "lines": [
            {
                "role": line.role,
                "label": line.label,
                "current_settings": line.incumbent,
                "params_sentence": params_sentence(declaration, line.record.point),
                "curve": {"evaluation_key": line.record.evaluation_key, **curves[line.record.evaluation_key]},
            }
            for line in drawn
        ],
        "left_out": chosen.left_out,
        "not_recorded": chosen.not_recorded,
        "failed": chosen.failed,
        "notes": list(chosen.notes),
    }

"""The Search step's charts: the procedure's path replayed, each knob's move and profile, and every scored point (#2821).

Formula, per procedure (the all-period search and the recent fit) over its
window, with the window's frozen selection policy (``TradeFloors.policy``):
  * replay — Zoom's recorded rounds rebuilt from the frozen seed: the seed's
    canonical point first, then, for each round in order, every value v its
    knob k evaluated, as the current point with k = v (canonical and hashed as
    the procedure built them), after which current[k] = the round's chosen
    value. Each tried point joins its base-scenario evaluation on the window
    by point hash. The replay is trusted only when its final point is the
    recorded winner and it holds every point the procedure's own step stored
    on the window (a budget stop mid-round stores points no round records);
    otherwise the convergence chart is not recorded;
  * best so far — after each tried point, the highest objective among the
    eligible points tried so far (``selection.objective_value`` and
    ``selection.ineligibility``); none until one is eligible;
  * knob moves — each searched knob's starting (seed) and retained (winner)
    value, and its position (v − low) / (high − low) in its planned range;
    an edge hit when the retained value is the range's low or high end;
  * profiles — Zoom: every value a knob's rounds evaluated in the last pass
    that searched it, every other knob held at its value then; Grid: every
    point the grid itself scored that matches the winner on every other knob;
  * eligibility map — every completed base-scenario evaluation on the
    window, with its ineligibility under the same policy; a failed run keeps
    its code and has no numbers (its zeros are placeholders, never results).
Reference: PRD https://github.com/tim1016/learn-ai/issues/2821 "Server work by
  chart" (V7–V10); the procedures are app/research/golden_search/zoom.py and
  grid_procedure.py, judged by selection.py.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_search_charts.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.research.golden_search.activity import TradeFloors
from app.research.golden_search.declarations import (
    SearchDeclaration,
    canonicalizer,
    declaration_for,
    knob_scalars,
    knob_values,
    point_hash,
    scalar,
    to_decimal,
)
from app.research.golden_search.models import EvaluationRecord, StudyRow
from app.research.golden_search.protocol import GoldenSearchProtocol, SelectionPolicy
from app.research.golden_search.repository import metrics_of
from app.research.golden_search.selection import Metrics, ineligibility, objective_value
from app.research.golden_search.zoom import ProcedureResult

NOT_REPLAYED = "The recorded path does not rebuild the recorded winner, so the search path is not drawn."
PARTIAL_ROUND = "The search ran out of budget partway through a round; the points it scored there belong to no recorded round, so the path is not drawn."
GRID_PATH = "Grid scores every combination at once, so it has no path to draw."


_NO_NUMBERS = {"sharpe_ratio": None, "net_profit": None, "total_return_pct": None, "total_trades": None, "max_drawdown_pct": None, "objective": None}


def _scored(metrics: Metrics | None, policy: SelectionPolicy) -> dict[str, Any]:
    """A scored point's numbers, or none when the window has no result for it or its run failed (a failed run's zeros are placeholders)."""
    if metrics is None:
        return {**_NO_NUMBERS, "ineligibility": "NOT_EVALUATED"}
    code = ineligibility(metrics, policy)
    if metrics.status != "completed":
        return {**_NO_NUMBERS, "ineligibility": code}
    return {
        "sharpe_ratio": metrics.sharpe_ratio,
        "net_profit": metrics.net_profit,
        "total_return_pct": metrics.total_return_pct,
        "total_trades": metrics.total_trades,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "ineligibility": code,
        "objective": objective_value(metrics, policy),
    }


def _position(value: Decimal, low: float, high: float) -> float | None:
    return None if high == low else float((value - to_decimal(low)) / (to_decimal(high) - to_decimal(low)))


class _Replay:
    """Zoom's tried points in the order the procedure evaluated them."""

    def __init__(self, declaration: SearchDeclaration, protocol: GoldenSearchProtocol, result: ProcedureResult) -> None:
        build = canonicalizer(declaration.strategy_key, protocol.symbol)
        self.declaration = declaration
        self.current = knob_values(declaration, protocol.seed)
        # (round index or None for the seed, the knob values tried, the canonical point, its hash)
        self.tried: list[tuple[int | None, dict[str, Decimal], dict[str, Any], str]] = []
        self._build = build
        self._add(None, dict(self.current))
        for index, round_ in enumerate(result.rounds):
            for value, _ in round_.results:
                self._add(index, {**self.current, round_.knob: to_decimal(value)})
            self.current[round_.knob] = to_decimal(round_.chosen)
        final = self._point(self.current)
        self.matches_winner = point_hash(declaration.strategy_key, final) == result.winner_hash

    def _point(self, values: Mapping[str, Decimal]) -> dict[str, Any]:
        return self._build(knob_scalars(self.declaration, values))

    def _add(self, round_index: int | None, values: dict[str, Decimal]) -> None:
        point = self._point(values)
        self.tried.append((round_index, values, point, point_hash(self.declaration.strategy_key, point)))


def _convergence(replay: _Replay, result: ProcedureResult, metrics: Mapping[str, Metrics], policy: SelectionPolicy) -> dict[str, Any]:
    if not replay.matches_winner:
        return {"status": "missing", "reason": NOT_REPLAYED}
    best: float | None = None
    tried = []
    for order, (round_index, values, point, hashed) in enumerate(replay.tried):
        scored = _scored(metrics.get(hashed), policy)
        if scored["ineligibility"] is None and scored["objective"] is not None and (best is None or scored["objective"] > best):
            best = scored["objective"]
        round_ = None if round_index is None else result.rounds[round_index]
        tried.append(
            {
                "order": order,
                "pass_index": None if round_ is None else round_.pass_index,
                "knob": None if round_ is None else round_.knob,
                "round_index": None if round_ is None else round_.round_index,
                "value": None if round_ is None else float(values[round_.knob]),
                "point": point,
                **scored,
                "best_so_far": best,
            }
        )
    return {"status": "measured", "tried": tried}


def _held(declaration: SearchDeclaration, values: Mapping[str, Decimal], knob: str) -> list[dict[str, Any]]:
    return [{"name": other.name, "label": other.label, "value": scalar(other, values[other.name])} for other in declaration.knobs if other.name != knob]


def _zoom_profiles(declaration: SearchDeclaration, protocol: GoldenSearchProtocol, replay: _Replay, result: ProcedureResult, metrics: Mapping[str, Metrics], policy: SelectionPolicy) -> list[dict[str, Any]]:
    winner = knob_values(declaration, result.winner)
    profiles = []
    for plan in protocol.search_knobs:
        knob = declaration.knob(plan.name)
        rounds = [index for index, round_ in enumerate(result.rounds) if round_.knob == knob.name]
        last_pass = max((result.rounds[index].pass_index for index in rounds), default=None)
        points: dict[Decimal, dict[str, Any]] = {}
        held: list[dict[str, Any]] = []
        for round_index, values, _, hashed in replay.tried:
            if round_index is None or round_index not in rounds or result.rounds[round_index].pass_index != last_pass:
                continue
            held = _held(declaration, values, knob.name)
            points[values[knob.name]] = {"value": scalar(knob, values[knob.name]), **_scored(metrics.get(hashed), policy), "retained": values[knob.name] == winner[knob.name]}
        profiles.append(_profile(knob.name, knob.label, knob.unit, plan.low, plan.high, last_pass, held, points))
    return profiles


def _grid_profiles(declaration: SearchDeclaration, protocol: GoldenSearchProtocol, result: ProcedureResult, records: Sequence[EvaluationRecord], policy: SelectionPolicy) -> list[dict[str, Any]]:
    winner = knob_values(declaration, result.winner)
    # Only the grid's own points: a pair audit or neighbor probe on the same window is no part of its lattice.
    own = set(result.evaluated_hashes)
    scored = [(knob_values(declaration, record.point), metrics_of(record)) for record in records if record.point_hash in own]
    profiles = []
    for plan in protocol.search_knobs:
        knob = declaration.knob(plan.name)
        points = {
            values[knob.name]: {"value": scalar(knob, values[knob.name]), **_scored(metrics, policy), "retained": values[knob.name] == winner[knob.name]}
            for values, metrics in scored
            if all(values[other.name] == winner[other.name] for other in declaration.knobs if other.name != knob.name)
        }
        profiles.append(_profile(knob.name, knob.label, knob.unit, plan.low, plan.high, None, _held(declaration, winner, knob.name), points))
    return profiles


def _profile(name: str, label: str, unit: str, low: float, high: float, pass_index: int | None, held: list[dict[str, Any]], points: Mapping[Decimal, dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "label": label, "unit": unit, "low": low, "high": high, "pass_index": pass_index, "held": held, "points": [points[value] for value in sorted(points)]}


def _moves(declaration: SearchDeclaration, protocol: GoldenSearchProtocol, result: ProcedureResult) -> list[dict[str, Any]]:
    start = knob_values(declaration, protocol.seed)
    retained = knob_values(declaration, result.winner)
    moves = []
    for plan in protocol.search_knobs:
        knob = declaration.knob(plan.name)
        moves.append(
            {
                "name": knob.name,
                "label": knob.label,
                "unit": knob.unit,
                "low": plan.low,
                "high": plan.high,
                "start": scalar(knob, start[knob.name]),
                "retained": scalar(knob, retained[knob.name]),
                "start_position": _position(start[knob.name], plan.low, plan.high),
                "retained_position": _position(retained[knob.name], plan.low, plan.high),
                "moved": start[knob.name] != retained[knob.name],
                "edge_hit": knob.name in result.edge_hits,
            }
        )
    return moves


def procedure_charts(
    *,
    key: str,
    record: Mapping[str, Any],
    declaration: SearchDeclaration,
    protocol: GoldenSearchProtocol,
    policy: SelectionPolicy,
    records: Sequence[EvaluationRecord],
) -> dict[str, Any]:
    """One procedure's charts from its stored record and every scored point on its window."""
    result = ProcedureResult.from_dict(record["procedure"])
    metrics = {record_.point_hash: metrics_of(record_) for record_ in records}
    if result.method == "zoom":
        replay = _Replay(declaration, protocol, result)
        # A budget stop mid-batch stores the points scored before it but records no round for them: the path would be short of them.
        replayed = {hashed for *_, hashed in replay.tried}
        unrecorded = any(record_.stage == key and record_.point_hash not in replayed for record_ in records)
        convergence = {"status": "missing", "reason": PARTIAL_ROUND} if unrecorded and replay.matches_winner else _convergence(replay, result, metrics, policy)
        profiles = _zoom_profiles(declaration, protocol, replay, result, metrics, policy) if replay.matches_winner else []
    else:
        convergence = {"status": "missing", "reason": GRID_PATH}
        profiles = _grid_profiles(declaration, protocol, result, records, policy)
    return {
        "key": key,
        "method": result.method,
        "window": dict(record["window"]),
        "policy": {
            "objective": policy.objective,
            "min_trades": policy.min_trades,
            "max_drawdown_ceiling": policy.max_drawdown_ceiling,
            "require_positive_net": policy.require_positive_net,
        },
        "winner_hash": result.winner_hash,
        "convergence": convergence,
        "moves": _moves(declaration, protocol, result),
        "profiles": profiles,
        "points": [
            {"point": dict(record_.point), "point_hash": record_.point_hash, **_scored(metrics_of(record_), policy), "winner": record_.point_hash == result.winner_hash}
            for record_ in records
        ],
    }


Window = tuple[int, int]
#: The procedures the Search step shows, in its order.
PROCEDURES = ("search", "recent")


def procedure_windows(row: StudyRow) -> list[Window]:
    """The windows of the procedures the study has recorded, whose scored points the charts read."""
    return [(int(record["window"]["start_ms"]), int(record["window"]["end_ms"])) for key in PROCEDURES if (record := row.results.get(key)) is not None]


def search_charts(row: StudyRow, evaluations: Mapping[Window, Sequence[EvaluationRecord]]) -> dict[str, Any]:
    """The Search step's charts for every procedure the study recorded; ``evaluations`` holds each window's scored points."""
    declaration = declaration_for(row.strategy_key)
    if declaration is None:
        return {"procedures": []}
    protocol = GoldenSearchProtocol.from_dict(row.protocol)
    floors = TradeFloors(protocol, row.receipt)
    procedures = []
    for key in PROCEDURES:
        record = row.results.get(key)
        if record is None:
            continue
        window = (int(record["window"]["start_ms"]), int(record["window"]["end_ms"]))
        procedures.append(
            procedure_charts(key=key, record=record, declaration=declaration, protocol=protocol, policy=floors.policy(window), records=evaluations.get(window, ()))
        )
    return {"procedures": procedures}

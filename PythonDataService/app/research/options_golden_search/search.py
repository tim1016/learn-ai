"""Deterministic complete Grid and coordinate Zoom selection for full Options setups.

Formula: eligible candidates maximize exact net dollars or Sharpe, or minimize
  starting-capital drawdown, as frozen in policy. Ties use profit, drawdown,
  Sharpe and setup hash. Zoom preserves the incumbent on a primary-objective tie.
Reference: https://github.com/tim1016/learn-ai/issues/3332.
Canonical implementation: this file.
Validated against: tests/research/options_golden_search/test_protocol_search.py.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from itertools import product
from typing import Any, Literal

from pydantic import Field, ValidationError

from app.options_backtest.setup import OptionsBacktestSetup
from app.research.options_golden_search.protocol import (
    FrozenModel,
    OptionsSearchProtocol,
    Window,
    materialize,
    point_hash,
    read_path,
)


class Evaluation(FrozenModel):
    point_hash: str
    net_profit_usd: Decimal
    max_drawdown_fraction: Decimal = Field(ge=0)
    sharpe: float | None
    eligible: bool
    evidence_id: str


class Trial(FrozenModel):
    ordinal: int
    role: str
    point: dict[str, Any]
    point_hash: str | None
    status: Literal["evaluated", "cache_hit", "invalid"]
    reason: str | None = None


class SearchResult(FrozenModel):
    winner: Evaluation | None
    eligible: tuple[Evaluation, ...]
    rejected: tuple[Evaluation, ...] = ()
    trials: tuple[Trial, ...]
    points: dict[str, dict[str, Any]]
    unique_evaluations: int
    complete: bool = True
    limitation: str | None = None


def ranking(evaluation: Evaluation, rank_by: str = "net_profit") -> tuple[Decimal | float, Decimal, Decimal, float, str]:
    """Frozen primary objective followed by deterministic presentation tie-breaks."""
    primary = (evaluation.max_drawdown_fraction if rank_by == "drawdown" else
               -(evaluation.sharpe if evaluation.sharpe is not None else float("-inf")) if rank_by == "sharpe" else
               -evaluation.net_profit_usd)
    return (primary, -evaluation.net_profit_usd, evaluation.max_drawdown_fraction,
            -(evaluation.sharpe if evaluation.sharpe is not None else float("-inf")), evaluation.point_hash)


def run_search(protocol: OptionsSearchProtocol, window: Window,
               evaluate: Callable[[OptionsBacktestSetup, Window, str], Evaluation]) -> SearchResult:
    """Run the entire frozen procedure, failing rather than trimming its authorized space."""
    cache: dict[str, Evaluation] = {}
    points: dict[str, dict[str, Any]] = {}
    trials: list[Trial] = []
    prefetched: set[str] = set()
    incumbent_point = {k.path: read_path(protocol.setup, k.path) for k in protocol.knobs}

    def record(trial: Trial) -> None:
        trials.append(trial)
        hook = getattr(evaluate, "on_trial", None)
        if hook is not None:
            hook(trial, window)

    def visit(point: dict[str, Any], role: str) -> Evaluation | None:
        try:
            setup = materialize(protocol, point)
        except (ValueError, ValidationError) as exc:
            record(Trial(ordinal=len(trials), role=role, point=point, point_hash=None,
                                status="invalid", reason=str(exc)))
            return None
        digest = point_hash(setup)
        hit = digest in cache and digest not in prefetched
        prefetched.discard(digest)
        if digest not in cache:
            result = evaluate(setup, window, "base")
            if result.point_hash != digest:
                raise ValueError("evaluator returned evidence for another setup")
            cache[digest] = result
            points[digest] = dict(point)
        record(Trial(ordinal=len(trials), role=role, point=point, point_hash=digest,
                            status="cache_hit" if hit else "evaluated"))
        return cache[digest]

    def visit_batch(candidates: list[dict[str, Any]], role: str) -> list[tuple[dict[str, Any], Evaluation | None]]:
        batch_evaluator = getattr(evaluate, "evaluate_batch", None)
        if batch_evaluator is not None:
            requested: dict[str, tuple[OptionsBacktestSetup, Window, str]] = {}
            requested_points: dict[str, dict[str, Any]] = {}
            for point in candidates:
                try:
                    setup = materialize(protocol, point)
                except (ValueError, ValidationError):
                    continue
                digest = point_hash(setup)
                if digest not in cache:
                    requested.setdefault(digest, (setup, window, "base"))
                    requested_points.setdefault(digest, dict(point))
            if requested:
                results = batch_evaluator(list(requested.values()))
                if len(results) != len(requested):
                    raise ValueError("batch evaluator returned an incomplete candidate batch")
                for (digest, _request), result in zip(requested.items(), results, strict=True):
                    if result.point_hash != digest:
                        raise ValueError("batch evaluator returned reordered or foreign evidence")
                    cache[digest] = result
                    prefetched.add(digest)
                    points[digest] = requested_points[digest]
        return [(point, visit(point, role)) for point in candidates]

    incumbent = visit(incumbent_point, "seed")
    axes = protocol.search_knobs

    def choose(batch: list[tuple[dict[str, Any], Evaluation | None]]) -> None:
        nonlocal incumbent, incumbent_point
        viable = [(point, result) for point, result in batch if result is not None and result.eligible]
        if not viable:
            return
        point, best = min(viable, key=lambda item: ranking(item[1], protocol.policy.rank_by))
        best_rank = ranking(best, protocol.policy.rank_by)
        if (incumbent is None or not incumbent.eligible or best_rank[0] < ranking(incumbent, protocol.policy.rank_by)[0]
                or (protocol.method == "grid" and best_rank < ranking(incumbent, protocol.policy.rank_by))):
            incumbent, incumbent_point = best, dict(point)

    if protocol.method == "grid":
        candidates = [{**incumbent_point, **{k.path: value for k, value in zip(axes, values, strict=True)}}
                      for values in product(*(k.values for k in axes))]
        choose(visit_batch(candidates, "grid"))
    else:
        for pass_index in range(protocol.zoom.passes):
            for knob in axes:
                low, high = 0, len(knob.values) - 1
                for refinement in range(protocol.zoom.refinements + 1):
                    # Integer index arithmetic avoids floating-point category drift.
                    width = high - low
                    count = min(protocol.zoom.points, width + 1)
                    indexes = sorted({low + (index * width // (count - 1)) for index in range(count)}) if count > 1 else [low]
                    candidates = [{**incumbent_point, knob.path: knob.values[index]} for index in indexes]
                    choose(visit_batch(candidates, f"zoom:{pass_index}:{knob.path}:{refinement}"))
                    current = incumbent_point[knob.path]
                    if current not in knob.values:
                        # No feasible move: preserve seed and continue the next declared dimension.
                        break
                    center = knob.values.index(current)
                    spacing = max(1, (width + max(1, count - 2)) // max(1, count - 1))
                    low, high = max(0, center - spacing), min(len(knob.values) - 1, center + spacing)
    by_path = {k.path: k for k in axes}
    for a, b in protocol.pair_audits:
        candidates = [{**incumbent_point, a: va, b: vb} for va, vb in product(by_path[a].values, by_path[b].values)]
        choose(visit_batch(candidates, f"pair:{a}:{b}"))
    eligible = tuple(sorted((result for result in cache.values() if result.eligible), key=lambda result: ranking(result, protocol.policy.rank_by)))
    return SearchResult(winner=incumbent if incumbent is not None and incumbent.eligible else None,
        eligible=eligible, rejected=tuple(result for result in cache.values() if not result.eligible),
        trials=tuple(trials), points=points, unique_evaluations=len(cache),
        limitation="Coordinate Zoom is local and order-sensitive; simultaneous changes can be missed." if protocol.method == "zoom" else None)


class ForwardResult(FrozenModel):
    fold_index: int
    selection: SearchResult
    forward: Evaluation | None
    status: Literal["evaluated", "no_eligible_point"]


def run_forward(protocol: OptionsSearchProtocol,
                evaluate: Callable[[OptionsBacktestSetup, Window, str], Evaluation]) -> tuple[ForwardResult, ...]:
    """Reset every fold to the original protocol and never let its test data select its winner."""
    outcomes = []
    for fold in protocol.folds:
        selection = run_search(protocol, fold.training, evaluate)
        winner = selection.winner
        if winner is None:
            outcomes.append(ForwardResult(fold_index=fold.index, selection=selection, forward=None, status="no_eligible_point"))
            continue
        setup = materialize(protocol, selection.points[winner.point_hash])
        forward = evaluate(setup, fold.forward, "base")
        if forward.point_hash != winner.point_hash:
            raise ValueError("forward evidence does not match its independently selected training winner")
        outcomes.append(ForwardResult(fold_index=fold.index, selection=selection, forward=forward, status="evaluated"))
    return tuple(outcomes)

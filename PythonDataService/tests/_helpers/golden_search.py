"""Builders for Golden Search's pure-core tests: a synthetic declaration, a valid plan, and landscapes.

The synthetic program is not registered, so procedures under test receive
:func:`synthetic_point` as their point builder; everything else — sampling,
constraints, selection, budget — runs the production code.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.engine.strategy.registry import _STRATEGY_REGISTRY, public_params_schema
from app.research.golden_search.declarations import KnobConstraint, SearchDeclaration, SearchKnob, declaration_for
from app.research.golden_search.protocol import (
    GoldenSearchProtocol,
    IncumbentRef,
    KnobPlan,
    SelectionPolicy,
    ZoomSettings,
)
from app.research.golden_search.selection import Metrics
from app.utils.session_anchors import et_midnight_ms

SYMBOL = "SPY"
STRATEGY = "synthetic_program"


def knob(
    name: str,
    *,
    kind: str = "integer",
    low: str = "0",
    high: str = "4",
    quantum: str = "1",
    neighbor: str = "1",
    default: str = "0",
    searchable: bool = True,
    step: str | None = None,
) -> SearchKnob:
    return SearchKnob(
        name=name,
        label=name.upper(),
        unit="units",
        kind=kind,  # type: ignore[arg-type]
        domain_low=Decimal(low),
        domain_high=Decimal(high),
        quantum=Decimal(quantum),
        default_low=Decimal(low),
        default_high=Decimal(high),
        neighbor_step=Decimal(neighbor),
        default_step=Decimal(step if step is not None else quantum),
        searchable_by_default=searchable,
        warmup_dependent=False,
        default_value=Decimal(default),
    )


def declaration(*knobs: SearchKnob, constraints: Sequence[KnobConstraint] = ()) -> SearchDeclaration:
    return SearchDeclaration(
        strategy_key=STRATEGY,
        knobs=knobs or (knob("x"), knob("y")),
        fixed=(),
        constraints=tuple(constraints),
        default_pair_audits=(),
    )


def ema_declaration_in_schema() -> SearchDeclaration:
    """The shipped EMA declaration narrowed to the knobs this branch's EMA parameter model accepts.

    The fast/slow/hold knobs arrive with #2696 Track E; until then a point
    carrying them would be refused by the model, so registry-backed tests use
    the knobs that exist and run unchanged once they all do.
    """
    full = declaration_for("ema_crossover_signal")
    assert full is not None
    present = set(public_params_schema(_STRATEGY_REGISTRY["ema_crossover_signal"])["properties"])
    return dataclasses.replace(
        full,
        knobs=tuple(k for k in full.knobs if k.name in present),
        constraints=tuple(c for c in full.constraints if {c.left, c.right} <= present),
        default_pair_audits=tuple(pair for pair in full.default_pair_audits if set(pair) <= present),
    )


def synthetic_point(values: Mapping[str, object]) -> dict[str, Any]:
    """The synthetic program's canonical point: every knob value plus the symbol."""
    return {"symbol": SYMBOL, **values}


def plans_for(decl: SearchDeclaration, *, step: float | None = None) -> tuple[KnobPlan, ...]:
    """Each knob at its declared defaults; ``step`` overrides every searched knob's default step."""
    return tuple(
        KnobPlan(
            name=k.name,
            mode="search" if k.searchable_by_default else "fixed",
            low=float(k.default_low),
            high=float(k.default_high),
            fixed_value=float(k.default_value),
            step=(step if step is not None else float(k.default_step)) if k.searchable_by_default else None,
        )
        for k in decl.knobs
    )


def protocol(decl: SearchDeclaration, **overrides: Any) -> GoldenSearchProtocol:
    """A plan every validation rule accepts: 12 development months (three 6/2 folds) then 3 final months."""
    seed = overrides.pop("seed", None) or synthetic_point({k.name: int(k.default_value) if k.kind == "integer" else float(k.default_value) for k in decl.knobs})
    base = GoldenSearchProtocol(
        strategy_key=decl.strategy_key,
        symbol=SYMBOL,
        method="zoom",
        knobs=plans_for(decl),
        seed=seed,
        incumbent=IncumbentRef(source="registry", qualification_id=None, params=dict(seed)),
        policy=SelectionPolicy(objective="sharpe_ratio", min_trades=30, max_drawdown_ceiling=0.2, require_positive_net=True),
        zoom=ZoomSettings(points=5, refinements=2, passes=2),
        development_start_ms=et_midnight_ms(date(2024, 1, 1)),
        development_end_ms=et_midnight_ms(date(2025, 1, 1)),
        final_start_ms=et_midnight_ms(date(2025, 1, 1)),
        final_end_ms=et_midnight_ms(date(2025, 4, 1)),
    )
    return dataclasses.replace(base, **overrides)


def metrics(
    objective: float | None,
    *,
    trades: int = 50,
    net: float | None = 1_000.0,
    drawdown: float | None = 0.10,
    total_return: float | None = 0.01,
    status: str = "completed",
) -> Metrics:
    return Metrics(
        status=status,  # type: ignore[arg-type]
        total_trades=trades,
        net_profit=net,
        total_return_pct=total_return,
        sharpe_ratio=objective,
        max_drawdown_pct=drawdown,
        win_rate=0.5,
    )


class Landscape:
    """An evaluate callback scoring each point by ``score(point)``; records every batch it was sent."""

    def __init__(self, score: Callable[[Mapping[str, Any]], Metrics | float]) -> None:
        self._score = score
        self.batches: list[list[dict[str, Any]]] = []

    def __call__(self, points: Sequence[dict[str, Any]]) -> list[Metrics]:
        self.batches.append([dict(point) for point in points])
        results = []
        for point in points:
            scored = self._score(point)
            results.append(scored if isinstance(scored, Metrics) else metrics(scored))
        return results

    @property
    def sent(self) -> list[dict[str, Any]]:
        return [point for batch in self.batches for point in batch]

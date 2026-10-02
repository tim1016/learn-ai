"""Golden Search capability declarations: which knobs a strategy exposes, and the one canonical point.

Formula: a declaration lists, in a fixed order, each searchable knob's exact
public parameter name, legal domain ``[domain_low, domain_high]``, quantum
``q`` (the smallest distinct step), default search range, default plan step
and neighbor step.
A value ``v`` is quantized to ``q · round_half_even(v / q)`` in exact
``Decimal`` arithmetic (seeded from the float's shortest repr, as
``app.research.sweep.grid`` does), then clamped to the domain. A point is the
registered parameter model's ``model_dump(mode="json")`` of the quantized
knob values plus ``symbol`` — integer knobs as ``int``, decimal knobs as
``float(Decimal)`` — so two spellings of one setting (``0.30000000000000004``
and ``0.3``; ``7.0`` and ``7``) are one point with one hash. The model owns
which defaults are identity-neutral and omitted from the dump. Constraints
are typed ``left < right`` predicates checked before a point exists, so a
constraint-violating combination is never canonicalized or evaluated.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Plan before
  seeing results"; point identity reuses ``app.research.sweep.grid.params_hash``.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_declarations.py.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.sweep.eligibility import (
    REASON_NO_SIGNAL_PROGRAM,
    REASON_NON_NUMERIC_PUBLIC_PARAMETER,
    REASON_NOT_PRODUCTION_CANDIDATE,
    sweep_eligibility,
)
from app.research.sweep.grid import params_hash

KnobKind = Literal["integer", "decimal"]
#: Builds the one canonical point from complete knob values; ``symbol`` is the caller's to add.
Canonicalize = Callable[[Mapping[str, object]], dict[str, Any]]


@dataclass(frozen=True)
class SearchKnob:
    """One numeric parameter a study may search or hold fixed."""

    name: str
    label: str
    unit: str
    kind: KnobKind
    domain_low: Decimal
    domain_high: Decimal
    quantum: Decimal
    default_low: Decimal
    default_high: Decimal
    neighbor_step: Decimal
    # The smallest step a new plan starts from for this knob (#2696): Grid samples at it and
    # Zoom stops refining at it. Always a multiple of ``quantum``.
    default_step: Decimal
    searchable_by_default: bool
    warmup_dependent: bool
    # The registered parameter model's default — the value a canonical point
    # stands for when the model omits an identity-neutral default.
    default_value: Decimal
    # Operator copy for a knob whose meaning is not obvious from its label.
    note: str = ""


@dataclass(frozen=True)
class FixedControl:
    """A control the trader sees but this program version does not expose."""

    label: str
    value: str
    reason: str


@dataclass(frozen=True)
class KnobConstraint:
    """``left < right`` between two knobs of the same point."""

    left: str
    op: Literal["<"]
    right: str
    message: str


@dataclass(frozen=True)
class SearchDeclaration:
    strategy_key: str
    knobs: tuple[SearchKnob, ...]
    fixed: tuple[FixedControl, ...]
    constraints: tuple[KnobConstraint, ...]
    default_pair_audits: tuple[tuple[str, str], ...]

    def knob(self, name: str) -> SearchKnob:
        for knob in self.knobs:
            if knob.name == name:
                return knob
        raise KeyError(f"{self.strategy_key} declares no knob {name!r}")


def _d(text: str) -> Decimal:
    return Decimal(text)


_EMA_DECLARATION = SearchDeclaration(
    strategy_key="ema_crossover_signal",
    # Declared order is the owner's search order (#2696).
    knobs=(
        SearchKnob(
            name="gap",
            label="Crossover gap",
            unit="price ($)",
            kind="decimal",
            domain_low=_d("0"),
            domain_high=_d("2"),
            quantum=_d("0.01"),
            default_low=_d("0.00"),
            default_high=_d("0.60"),
            neighbor_step=_d("0.05"),
            default_step=_d("0.05"),
            searchable_by_default=True,
            warmup_dependent=False,
            default_value=_d("0.2"),
        ),
        SearchKnob(
            name="rsi_min",
            label="RSI lower gate",
            unit="RSI points",
            kind="decimal",
            domain_low=_d("0"),
            domain_high=_d("100"),
            quantum=_d("1"),
            default_low=_d("30"),
            default_high=_d("60"),
            neighbor_step=_d("2"),
            default_step=_d("1"),
            searchable_by_default=True,
            warmup_dependent=False,
            default_value=_d("50"),
        ),
        SearchKnob(
            name="rsi_max",
            label="RSI upper gate",
            unit="RSI points",
            kind="decimal",
            domain_low=_d("0"),
            domain_high=_d("100"),
            quantum=_d("1"),
            default_low=_d("60"),
            default_high=_d("90"),
            neighbor_step=_d("2"),
            default_step=_d("1"),
            searchable_by_default=True,
            warmup_dependent=False,
            default_value=_d("70"),
        ),
        SearchKnob(
            name="fast_period",
            label="Fast EMA length",
            unit="decision bars",
            kind="integer",
            domain_low=_d("2"),
            domain_high=_d("30"),
            quantum=_d("1"),
            default_low=_d("3"),
            default_high=_d("12"),
            neighbor_step=_d("1"),
            default_step=_d("1"),
            searchable_by_default=True,
            warmup_dependent=True,
            default_value=_d("5"),
        ),
        SearchKnob(
            name="slow_period",
            label="Slow EMA length",
            unit="decision bars",
            kind="integer",
            domain_low=_d("3"),
            domain_high=_d("40"),
            quantum=_d("1"),
            default_low=_d("8"),
            default_high=_d("30"),
            neighbor_step=_d("1"),
            default_step=_d("1"),
            searchable_by_default=True,
            warmup_dependent=True,
            default_value=_d("10"),
        ),
        SearchKnob(
            name="hold_bars",
            label="Hold time",
            unit="decision bars",
            kind="integer",
            domain_low=_d("1"),
            domain_high=_d("26"),
            quantum=_d("1"),
            default_low=_d("2"),
            default_high=_d("12"),
            neighbor_step=_d("1"),
            default_step=_d("1"),
            searchable_by_default=True,
            warmup_dependent=False,
            default_value=_d("5"),
        ),
        SearchKnob(
            name="gap_bps",
            label="Crossover gap (bps)",
            unit="basis points",
            kind="decimal",
            domain_low=_d("0"),
            domain_high=_d("100"),
            quantum=_d("0.5"),
            default_low=_d("0"),
            default_high=_d("5"),
            neighbor_step=_d("0.5"),
            default_step=_d("0.5"),
            searchable_by_default=False,
            warmup_dependent=False,
            default_value=_d("0"),
            note=(
                "Both gap floors apply together: an entry must clear the price gap and this basis-point gap. "
                "It stays fixed at 0 by default, which imposes no basis-point floor."
            ),
        ),
    ),
    fixed=(
        FixedControl(label="RSI length", value="14", reason="Fixed in this program version."),
        FixedControl(
            label="Decision cadence",
            value="15 minutes",
            reason="The program decides on 15-minute bars; fixed in this program version.",
        ),
    ),
    constraints=(
        KnobConstraint(
            left="fast_period",
            op="<",
            right="slow_period",
            message="The fast EMA length must be shorter than the slow EMA length.",
        ),
        KnobConstraint(
            left="rsi_min",
            op="<",
            right="rsi_max",
            message="The RSI lower gate must be below the RSI upper gate.",
        ),
    ),
    default_pair_audits=(("fast_period", "slow_period"), ("rsi_min", "rsi_max")),
)

_DECLARATIONS: dict[str, SearchDeclaration] = {_EMA_DECLARATION.strategy_key: _EMA_DECLARATION}

NO_DECLARATION_REASON = "No Golden Search declaration has shipped for this strategy yet."
_INELIGIBILITY_COPY = {
    REASON_NOT_PRODUCTION_CANDIDATE: "This strategy is an operational harness, not a production candidate.",
    REASON_NO_SIGNAL_PROGRAM: "This strategy has no Signal Program, so its warmup cannot be measured.",
}


def declaration_for(strategy_key: str) -> SearchDeclaration | None:
    """The shipped declaration, or ``None`` when the strategy has none."""
    return _DECLARATIONS.get(strategy_key)


def unavailable_reason(strategy_key: str) -> str | None:
    """``None`` when a declaration has shipped; otherwise the plain-English reason Golden Search cannot run it."""
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        return f"No strategy named {strategy_key!r} is registered."
    eligibility = sweep_eligibility(registration)
    if not eligibility.eligible:
        reasons = [_INELIGIBILITY_COPY[code] for code in eligibility.reason_codes if code in _INELIGIBILITY_COPY]
        if REASON_NON_NUMERIC_PUBLIC_PARAMETER in eligibility.reason_codes:
            reasons.append(f"Some parameters are not plain numbers: {', '.join(eligibility.offending_parameters)}.")
        return " ".join(reasons)
    if strategy_key not in _DECLARATIONS:
        return NO_DECLARATION_REASON
    return None


# ── Values ───────────────────────────────────────────────────────────────


def to_decimal(value: object) -> Decimal:
    """A finite number as the exact decimal its shortest repr denotes; refuses bools, strings and non-finite values."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError(f"{value!r} is not a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{value!r} is not finite")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{value!r} is not finite")
        return value
    return Decimal(str(value))


def snap(knob: SearchKnob, value: Decimal) -> Decimal:
    """``value`` rounded half-even to the knob's quantum, without clamping."""
    steps = (value / knob.quantum).to_integral_value(rounding=ROUND_HALF_EVEN)
    snapped = steps * knob.quantum
    # ``to_integral_value``, not ``quantize``: quantize raises past the context's 28 digits.
    return snapped.to_integral_value() if knob.kind == "integer" else snapped


def quantize(knob: SearchKnob, value: Decimal) -> Decimal:
    """``value`` rounded half-even to the knob's quantum, then clamped to its domain."""
    return min(max(snap(knob, value), knob.domain_low), knob.domain_high)


def is_quantized(knob: SearchKnob, value: Decimal) -> bool:
    return snap(knob, value) == value


def scalar(knob: SearchKnob, value: Decimal) -> int | float:
    """The JSON scalar a point carries: ``int`` for an integer knob, ``float(Decimal)`` for a decimal one."""
    return int(value) if knob.kind == "integer" else float(value)


def knob_scalars(declaration: SearchDeclaration, values: Mapping[str, Decimal]) -> dict[str, int | float]:
    """Every declared knob's value as the JSON scalar a point carries."""
    return {knob.name: scalar(knob, values[knob.name]) for knob in declaration.knobs}


def knob_values(declaration: SearchDeclaration, point: Mapping[str, object]) -> dict[str, Decimal]:
    """Every declared knob's value in ``point``, standing in the declared default for an omitted one."""
    return {knob.name: to_decimal(point[knob.name]) if knob.name in point else knob.default_value for knob in declaration.knobs}


def violates(declaration: SearchDeclaration, values: Mapping[str, object]) -> str | None:
    """The first violated constraint's message, or ``None``; an omitted knob stands at its declared default."""
    for constraint in declaration.constraints:
        left = _value_or_default(declaration, values, constraint.left)
        right = _value_or_default(declaration, values, constraint.right)
        if not left < right:
            return constraint.message
    return None


def _value_or_default(declaration: SearchDeclaration, values: Mapping[str, object], name: str) -> Decimal:
    if name in values:
        return to_decimal(values[name])
    return declaration.knob(name).default_value


# ── The canonical point ─────────────────────────────────────────────────


def canonical_point(strategy_key: str, symbol: str, values: Mapping[str, object]) -> dict[str, Any]:
    """The ONE canonical form of a parameter assignment, ``symbol`` included.

    Declared knobs are snapped to their quantum (never clamped: a value
    outside the domain is refused, not moved) and typed by kind before the
    registered model validates and dumps them, so the dump — including the
    model's own omission of identity-neutral defaults — is the identity.
    Raises ``ValueError`` for an unknown strategy, a non-numeric or
    out-of-domain knob, or anything the model refuses.
    """
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        raise ValueError(f"unknown strategy {strategy_key!r}")
    declaration = declaration_for(strategy_key)
    prepared: dict[str, object] = dict(values)
    if declaration is not None:
        for knob in declaration.knobs:
            if knob.name not in prepared:
                continue
            snapped = snap(knob, to_decimal(prepared[knob.name]))
            if not knob.domain_low <= snapped <= knob.domain_high:
                raise ValueError(
                    f"{knob.name}={snapped} is outside its domain {knob.domain_low}..{knob.domain_high}"
                )
            prepared[knob.name] = scalar(knob, snapped)
    model = registration.param_schema.model_validate({**prepared, "symbol": symbol})
    return model.model_dump(mode="json")


def canonicalizer(strategy_key: str, symbol: str) -> Canonicalize:
    """:func:`canonical_point` bound to one strategy and symbol — the default point builder of every procedure."""

    def _canonicalize(values: Mapping[str, object]) -> dict[str, Any]:
        return canonical_point(strategy_key, symbol, values)

    return _canonicalize


def point_hash(strategy_key: str, point: Mapping[str, Any]) -> str:
    """Key-order-independent identity of one canonical point (``sweep.grid.params_hash``)."""
    return params_hash(strategy_key, point)

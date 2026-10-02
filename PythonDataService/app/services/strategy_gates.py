"""Custom Dark Bright Gates: parse, resolve and judge one linear expression (#2639 D8–D10).

A custom gate is ``a₁·X₁ + a₂·X₂ + … + c``, bright where it is above zero
(``gt``) or below it (``lt``). This module is the one place a custom gate is
read and judged, for the bot page and Strategy Lab alike; the browser does no
gate arithmetic.

A variable resolves in this order (D9):

1. a value the strategy recorded on the bar (its view's variable, ``EMA5``);
2. a deployed setting by name (``gap``, ``rsi_min``);
3. the candle's own ``open``/``high``/``low``/``close``/``volume``;
4. a catalogue indicator written as name plus length (``EMA20``, ``ATR14``)
   or a parameterless one (``VWAP``, ``OBV``), computed on the decision
   candles and marked chart-computed.

So a catalogue name the strategy already records is always the bot's own
value: one number never has two sources. A catalogue indicator is computed on
the decision candles the caller sends, with no history before them, so it has
no value until its own warmup has passed within those candles.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import Any

from app.schemas.strategy_gates import (
    CustomGate,
    CustomGateInput,
    GateCandle,
    GateCatalogueEntry,
    GateSign,
    GateTerm,
)
from app.services.chart_indicator_service import CHART_INDICATOR_NAMES, ChartIndicatorService
from app.services.dataset_service import INDICATOR_CONFIGS, list_available_indicators
from app.services.strategy_view import ResolvedStrategyView

logger = logging.getLogger(__name__)

DRAFT_GATE_ID = "draft"
CANDLE_FIELDS = ("open", "high", "low", "close", "volume")

_TOKEN = re.compile(r"\s*(?:(?P<number>[0-9]+(?:\.[0-9]+)?|\.[0-9]+)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[-+*/()]))")
_CATALOGUE_NAME = re.compile(r"^(?P<name>[A-Za-z]+?)(?P<length>\d+)?$")


class GateExpressionError(ValueError):
    """A gate the owner wrote that cannot be saved or judged; the message says why, plainly."""


@dataclass(frozen=True)
class LinearExpression:
    """``Σ coefficient·variable + constant``, each variable once, in first-seen order."""

    terms: tuple[tuple[float, str], ...]
    constant: float


def parse_linear(text: str) -> LinearExpression:
    """Read one linear expression, or refuse it with a plain reason."""
    normalized = text.replace("−", "-").replace("·", "*").replace("×", "*").strip()
    if not normalized:
        raise GateExpressionError("The expression is empty.")
    if any(mark in normalized for mark in "<>="):
        raise GateExpressionError("Leave the comparison out of the expression; choose > 0 or < 0 instead.")
    tokens = _tokenize(normalized)
    parser = _Parser(tokens)
    coefficients, constant = parser.expression()
    if parser.position != len(tokens):
        raise GateExpressionError(f"Unexpected '{tokens[parser.position]}' in the expression.")
    if not all(math.isfinite(number) for number in (*coefficients.values(), constant)):
        raise GateExpressionError("A number in the expression is too large to judge.")
    terms = tuple((coefficient, name) for name, coefficient in coefficients.items() if coefficient != 0)
    if not terms:
        raise GateExpressionError("The expression uses no variable, so it would shade every candle the same.")
    return LinearExpression(terms=terms, constant=constant)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            remainder = text[position:].strip()
            if not remainder:
                break
            raise GateExpressionError(f"'{remainder[0]}' cannot appear in a gate.")
        tokens.append(match.group("number") or match.group("name") or match.group("op"))
        position = match.end()
    return tokens


_Linear = tuple[dict[str, float], float]


class _Parser:
    """Recursive descent over ``+ - * / ( )`` that keeps every sub-expression linear."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.position = 0

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise GateExpressionError("The expression ends too soon.")
        self.position += 1
        return token

    def expression(self) -> _Linear:
        result = self._term()
        while self._peek() in ("+", "-"):
            operator = self._take()
            right = self._term()
            result = _combine(result, right, -1.0 if operator == "-" else 1.0)
        return result

    def _term(self) -> _Linear:
        result = self._factor()
        while self._peek() in ("*", "/"):
            operator = self._take()
            right = self._factor()
            if operator == "*":
                result = _multiply(result, right)
            else:
                if right[0]:
                    raise GateExpressionError("A gate can divide only by a number, not by a variable.")
                if right[1] == 0:
                    raise GateExpressionError("The expression divides by zero.")
                result = _scale(result, 1.0 / right[1])
        return result

    def _factor(self) -> _Linear:
        token = self._take()
        if token in ("+", "-"):
            inner = self._factor()
            return inner if token == "+" else _scale(inner, -1.0)
        if token == "(":
            inner = self.expression()
            if self._peek() != ")":
                raise GateExpressionError("A '(' is never closed.")
            self._take()
            return inner
        if token in (")", "*", "/"):
            raise GateExpressionError(f"Unexpected '{token}' in the expression.")
        if token[0].isdigit() or token[0] == ".":
            return {}, float(token)
        return {token: 1.0}, 0.0


def _combine(left: _Linear, right: _Linear, factor: float) -> _Linear:
    coefficients = dict(left[0])
    for name, coefficient in right[0].items():
        coefficients[name] = coefficients.get(name, 0.0) + factor * coefficient
    return coefficients, left[1] + factor * right[1]


def _scale(value: _Linear, factor: float) -> _Linear:
    return {name: coefficient * factor for name, coefficient in value[0].items()}, value[1] * factor


def _multiply(left: _Linear, right: _Linear) -> _Linear:
    if left[0] and right[0]:
        raise GateExpressionError("A gate must be linear: it cannot multiply one variable by another.")
    if left[0]:
        return _scale(left, right[1])
    return _scale(right, left[1])


class VariableSource(StrEnum):
    """Where a gate variable's number comes from (D9's order)."""

    RECORDED = "recorded"
    SETTING = "setting"
    CANDLE = "candle"
    CATALOGUE = "catalogue"


@dataclass(frozen=True)
class GateVariable:
    """One resolved variable: its source, and what to read there."""

    name: str
    source: VariableSource
    # The recorded value key, the setting or candle field, or the catalogue indicator.
    key: str
    catalogue_params: tuple[tuple[str, int], ...] = ()

    @property
    def identity(self) -> tuple[object, ...]:
        """What the variable reads, whatever it was called: ``ema5`` and ``EMA5`` are one."""
        return (self.source, self.key, self.catalogue_params)


# Bars the catalogue probe computes each indicator on: long enough for every
# catalogue default to warm up.
_PROBE_BARS = 600
_PROBE_BAR_MS = 60_000
_PROBE_START_MS = 1_700_000_000_000


def _probe_bars() -> list[dict[str, float]]:
    bars = []
    for index in range(_PROBE_BARS):
        close = 100.0 + 5.0 * math.sin(index / 17.0) + 0.01 * index
        bars.append(
            {
                "t": _PROBE_START_MS + index * _PROBE_BAR_MS,
                "o": close - 0.2,
                "h": close + 0.5,
                "l": close - 0.5,
                "c": close,
                "v": 1_000.0 + 10.0 * (index % 7),
            }
        )
    return bars


@dataclass(frozen=True)
class _CatalogueProbe:
    usable: tuple[GateCatalogueEntry, ...]
    # Why each other no-setting or length-only indicator cannot be read by a gate.
    refused: dict[str, str]


@cache
def _probe_catalogue() -> _CatalogueProbe:
    """Compute each no-setting or length-only catalogue indicator once, to see what a gate can read.

    An indicator qualifies when the canonical chart computation gives it
    exactly one line on a probe of synthetic bars, so the list cannot drift
    from what judging does.
    """
    descriptions = {
        item["name"]: item["description"] for items in list_available_indicators().values() for item in items
    }
    service = ChartIndicatorService()
    bars = _probe_bars()
    usable: list[GateCatalogueEntry] = []
    refused: dict[str, str] = {}
    for name in sorted(CHART_INDICATOR_NAMES):
        params = INDICATOR_CONFIGS[name]
        if params and [definition["name"] for definition in params] != ["length"]:
            continue
        length = params[0] if params else None
        entry: dict[str, Any] = {"name": name, "params": {"length": length["default"]} if length else {}}
        try:
            _symbol, series = service.compute("SPY", bars, [entry])
        except ValueError as exc:
            logger.info("gate_catalogue_indicator_not_computable", extra={"indicator": name, "error": str(exc)})
            refused[name] = f"{name.upper()} could not be computed on candles, so a gate cannot use it yet."
            continue
        if len(series) != 1 or not isinstance(series[0]["data"], list):
            refused[name] = f"{name.upper()} draws more than one line, so a gate cannot use it yet."
            continue
        usable.append(
            GateCatalogueEntry(
                name=name,
                description=descriptions.get(name, ""),
                variable=f"{name.upper()}{length['default'] if length else ''}",
                default_length=length["default"] if length else None,
                min_length=length["min"] if length else None,
                max_length=length["max"] if length else None,
            )
        )
    return _CatalogueProbe(usable=tuple(usable), refused=refused)


def gate_catalogue() -> tuple[GateCatalogueEntry, ...]:
    """The catalogue indicators a gate can read: one line, and no setting or only a length."""
    return _probe_catalogue().usable


def resolve_variable(view: ResolvedStrategyView, name: str) -> GateVariable:
    """Resolve ``name`` in D9's order, or refuse it as unknown."""
    recorded = {spec.variable.upper(): spec.key for spec in view.declaration().values}
    if name.upper() in recorded:
        return GateVariable(name=name, source=VariableSource.RECORDED, key=recorded[name.upper()])
    settings = view.settings
    if name in settings:
        value = settings[name]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise GateExpressionError(f"The setting '{name}' is not a number, so a gate cannot use it.")
        return GateVariable(name=name, source=VariableSource.SETTING, key=name)
    if name.lower() in CANDLE_FIELDS:
        return GateVariable(name=name, source=VariableSource.CANDLE, key=name.lower())
    return _catalogue_variable(name)


def _catalogue_variable(name: str) -> GateVariable:
    match = _CATALOGUE_NAME.match(name)
    indicator = match.group("name").lower() if match else ""
    if not match or indicator not in CHART_INDICATOR_NAMES:
        raise GateExpressionError(f"'{name}' is not a value, a setting, a candle field or a catalogue indicator.")
    params = {definition["name"]: definition for definition in INDICATOR_CONFIGS[indicator]}
    if params and set(params) != {"length"}:
        raise GateExpressionError(f"{indicator.upper()} has more than one setting, so a gate cannot use it yet.")
    refusal = _probe_catalogue().refused.get(indicator)
    if refusal is not None:
        raise GateExpressionError(refusal)
    length = match.group("length")
    if not params:
        if length is not None:
            raise GateExpressionError(f"{indicator.upper()} takes no length; write it as {indicator.upper()}.")
        return GateVariable(name=name, source=VariableSource.CATALOGUE, key=indicator)
    if length is None:
        raise GateExpressionError(f"{indicator.upper()} needs a length, e.g. {indicator.upper()}20.")
    bounds = params["length"]
    if not bounds["min"] <= int(length) <= bounds["max"]:
        raise GateExpressionError(f"{indicator.upper()}'s length must be between {bounds['min']} and {bounds['max']}.")
    return GateVariable(
        name=name, source=VariableSource.CATALOGUE, key=indicator, catalogue_params=(("length", int(length)),)
    )


def compile_gate(view: ResolvedStrategyView, draft: CustomGateInput) -> tuple[list[GateTerm], float]:
    """Parse and resolve a gate for ``view``'s strategy, or refuse it with a plain reason.

    Names that read the same number (``EMA5`` and ``ema5``) are one term,
    written as first spelled; terms that cancel out are dropped.
    """
    expression = parse_linear(draft.expression)
    merged: dict[tuple[object, ...], tuple[str, float]] = {}
    for coefficient, name in expression.terms:
        identity = resolve_variable(view, name).identity
        spelled, total = merged.get(identity, (name, 0.0))
        merged[identity] = (spelled, total + coefficient)
    if not all(math.isfinite(total) for _spelled, total in merged.values()):
        raise GateExpressionError("A number in the expression is too large to judge.")
    terms = [GateTerm(coefficient=total, variable=spelled) for spelled, total in merged.values() if total != 0]
    if not terms:
        raise GateExpressionError("The expression's variables cancel out, so it would shade every candle the same.")
    return terms, expression.constant


@dataclass(frozen=True)
class _GateForm:
    gate_id: str
    terms: Sequence[GateTerm]
    constant: float
    sign: GateSign


def evaluate_gates(
    view: ResolvedStrategyView,
    gates: Sequence[CustomGate],
    candles: Sequence[GateCandle],
    *,
    symbol: str,
    draft: CustomGateInput | None = None,
) -> tuple[dict[str, list[bool | None]], list[str], list[str]]:
    """Judge each gate on every candle: ``(results by gate id, chart-computed names, notices)``.

    A saved gate whose variable no longer resolves for these settings is
    reported in a notice and judged nowhere (every entry ``None``); a draft
    that does not compile raises, because the owner is looking at it.
    """
    forms = [_GateForm(gate.gate_id, gate.terms, gate.constant, gate.sign) for gate in gates]
    if draft is not None:
        terms, constant = compile_gate(view, draft)
        forms.append(_GateForm(DRAFT_GATE_ID, terms, constant, draft.sign))
    notices: list[str] = []
    columns: dict[str, list[float | None]] = {}
    chart_computed: list[str] = []
    results: dict[str, list[bool | None]] = {}
    for form in forms:
        try:
            for term in form.terms:
                if term.variable not in columns:
                    variable = resolve_variable(view, term.variable)
                    columns[term.variable] = _column(view, variable, candles, symbol=symbol)
                    if variable.source is VariableSource.CATALOGUE:
                        chart_computed.append(term.variable)
        except GateExpressionError as exc:
            if form.gate_id == DRAFT_GATE_ID:
                raise
            notices.append(f"A saved gate could not be judged for these settings: {exc}")
            results[form.gate_id] = [None] * len(candles)
            continue
        results[form.gate_id] = [_judge(form, columns, index) for index in range(len(candles))]
    return results, chart_computed, notices


def _judge(form: _GateForm, columns: dict[str, list[float | None]], index: int) -> bool | None:
    total = form.constant
    for term in form.terms:
        value = columns[term.variable][index]
        if value is None or not math.isfinite(value):
            return None
        total += term.coefficient * value
    if not math.isfinite(total):
        return None
    return total > 0 if form.sign == "gt" else total < 0


def _column(
    view: ResolvedStrategyView, variable: GateVariable, candles: Sequence[GateCandle], *, symbol: str
) -> list[float | None]:
    if variable.source is VariableSource.RECORDED:
        return [candle.values.get(variable.key) for candle in candles]
    if variable.source is VariableSource.SETTING:
        return [float(view.settings[variable.key])] * len(candles)
    if variable.source is VariableSource.CANDLE:
        return [float(getattr(candle, variable.key)) for candle in candles]
    return _catalogue_column(variable, candles, symbol=symbol)


def _catalogue_column(variable: GateVariable, candles: Sequence[GateCandle], *, symbol: str) -> list[float | None]:
    """The catalogue indicator over the decision candles, aligned to them by close."""
    if not candles:
        return []
    entry: dict[str, Any] = {"name": variable.key, "params": dict(variable.catalogue_params)}
    bars = [
        {
            "t": candle.bar_close_ms,
            "o": candle.open,
            "h": candle.high,
            "l": candle.low,
            "c": candle.close,
            "v": candle.volume,
        }
        for candle in candles
    ]
    try:
        _symbol, series = ChartIndicatorService().compute(symbol, bars, [entry])
    except ValueError as exc:
        raise GateExpressionError(f"{variable.name} could not be computed on these candles ({exc}).") from exc
    if len(series) != 1 or not isinstance(series[0]["data"], list):
        raise GateExpressionError(f"{variable.name} draws more than one line, so a gate cannot use it yet.")
    by_close = {point["t"]: point["value"] for point in series[0]["data"]}
    return [by_close.get(candle.bar_close_ms) for candle in candles]


__all__ = [
    "CANDLE_FIELDS",
    "DRAFT_GATE_ID",
    "GateExpressionError",
    "GateVariable",
    "LinearExpression",
    "VariableSource",
    "compile_gate",
    "evaluate_gates",
    "gate_catalogue",
    "parse_linear",
    "resolve_variable",
]

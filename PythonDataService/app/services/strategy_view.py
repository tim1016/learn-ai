"""The strategy view's one renderer and gate evaluator (#2639).

A ``ResolvedStrategyView`` is one strategy's registered view bound to one set
of deployed settings. It is the only place that words a recorded check, labels
a recorded value or decides whether a Dark Bright Gate held on a bar, so the
bot page and Strategy Lab read the same sentences and the same shading.

The default gate is bright exactly where the strategy's own rule passed: it
reads that check's recorded pass/fail and compares nothing itself, which is
what keeps its edges the strategy's (an RSI of exactly 50 shades the way the
bot judged it).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.engine.strategy.params import StrategyParamsBase, decision_timeframe_ms_for
from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration
from app.engine.strategy.strategy_view import ChartParamRef, StrategyView, ViewCheck, ViewValue
from app.schemas.decision_explanation import DecisionExplanationRecord, ExplainedCheckRecord
from app.schemas.strategy_view import (
    CatalogueIndicatorRef,
    DecisionExplanationView,
    ExplainedCheckView,
    ExplainedValueView,
    StrategyViewDeclarationView,
    StrategyViewGateView,
    StrategyViewValueSpec,
)

logger = logging.getLogger(__name__)

_NOT_AVAILABLE = "not available"


class StrategyViewUnavailableError(ValueError):
    """This strategy declares no strategy view (or is not registered)."""


@dataclass(frozen=True)
class ResolvedStrategyView:
    """One strategy's view, bound to the settings a bot (or a run) was deployed with."""

    strategy_key: str
    registration: StrategyRegistration
    view: StrategyView
    params: StrategyParamsBase

    @classmethod
    def for_settings(
        cls, strategy_key: str, strategy_params: Mapping[str, Any] | None, *, symbol: str
    ) -> ResolvedStrategyView:
        """Resolve ``strategy_key``'s view for these parameters (registered defaults fill the rest)."""
        registration = _STRATEGY_REGISTRY.get(strategy_key)
        if registration is None:
            raise StrategyViewUnavailableError(f"Strategy '{strategy_key}' is not registered in this build.")
        if registration.strategy_view is None:
            raise StrategyViewUnavailableError(f"Strategy '{strategy_key}' declares no strategy view.")
        try:
            params = registration.param_schema(**{**(strategy_params or {}), "symbol": symbol})
        except ValidationError as exc:
            raise StrategyViewUnavailableError(
                f"This bot's saved settings no longer fit strategy '{strategy_key}': {exc.error_count()} invalid."
            ) from exc
        return cls(strategy_key=strategy_key, registration=registration, view=registration.strategy_view, params=params)

    @property
    def settings(self) -> dict[str, Any]:
        """The deployed settings by name, as templates and custom gates read them.

        Read field by field: a parameter dump may omit a field sitting at its
        default (EMA's lengths, #2696), and a label still needs its value.
        """
        return {name: getattr(self.params, name) for name in type(self.params).model_fields}

    @property
    def scalar_settings(self) -> dict[str, float | int | str | bool | None]:
        """The deployed settings that are plain scalars, as the wire carries them."""
        return {
            name: value
            for name, value in self.settings.items()
            if value is None or isinstance(value, bool | int | float | str)
        }

    @property
    def decision_timeframe_ms(self) -> int:
        contract = self.registration.signal_program_contract
        qualified_ms = contract.decision_timeframe_ms if contract is not None else 60_000
        return decision_timeframe_ms_for(self.params, qualified_ms=qualified_ms)

    def declaration(self) -> StrategyViewDeclarationView:
        """The view's values and gates, labelled for these settings."""
        settings = self.settings
        gate = self.view.default_gate
        return StrategyViewDeclarationView(
            values=[self._value_spec(value, settings) for value in self.view.values],
            gates=[
                StrategyViewGateView(
                    gate_id=gate.gate_id,
                    label=gate.label.format(**settings),
                    expression=gate.expression,
                    source="strategy",
                )
            ],
            default_gate_id=gate.gate_id,
        )

    def render(self, record: DecisionExplanationRecord) -> DecisionExplanationView:
        """Word one recorded explanation for the owner."""
        settings = self.settings
        return DecisionExplanationView(
            ready=record.ready,
            holding=record.holding,
            signal=record.signal,
            values=[self._value_view(value, record.values.get(value.key), settings) for value in self.view.values],
            checks=[self._check_view(check, record.holding) for check in record.checks],
        )

    def render_or_none(self, record: DecisionExplanationRecord) -> DecisionExplanationView | None:
        """``render``, or ``None`` with a warning when this build cannot word the row.

        A stored row outlives the build that wrote it, and presentation may
        change freely (#2639 D14), so one row this build cannot word must
        never take a page down with it.
        """
        try:
            return self.render(record)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            logger.warning(
                "A recorded decision explanation could not be worded",
                extra={
                    "action": "strategy_view_explanation_unrenderable",
                    "strategy_key": self.strategy_key,
                    "bar_close_ms": record.bar.end_ms,
                    "reason": repr(exc),
                },
            )
            return None

    def gate_results(self, record: DecisionExplanationRecord) -> dict[str, bool | None]:
        """Whether each gate held on this bar; ``None`` when the bar recorded no such rule."""
        gate = self.view.default_gate
        check = next((check for check in record.checks if check.check_id == gate.check_id), None)
        return {gate.gate_id: None if check is None else check.passed}

    def _value_spec(self, value: ViewValue, settings: Mapping[str, Any]) -> StrategyViewValueSpec:
        catalogue = value.catalogue
        return StrategyViewValueSpec(
            key=value.key,
            label=value.label.format(**settings),
            variable=value.variable.format(**settings),
            pane=value.pane,
            band=None if value.band is None else [self._number(bound) for bound in value.band],
            decimals=value.decimals,
            catalogue=(
                None
                if catalogue is None
                else CatalogueIndicatorRef(
                    name=catalogue.name,
                    params={name: self._number(param) for name, param in catalogue.params.items()},
                )
            ),
        )

    def _value_view(self, value: ViewValue, recorded: float | None, settings: Mapping[str, Any]) -> ExplainedValueView:
        return ExplainedValueView(
            key=value.key,
            label=value.label.format(**settings),
            value=recorded,
            text=_NOT_AVAILABLE if recorded is None else f"{recorded:.{value.decimals}f}",
        )

    def _check_view(self, record: ExplainedCheckRecord, holding: bool) -> ExplainedCheckView:
        """Word one rule from its record: the operator and bound are the decision's own."""
        wording: ViewCheck = self.view.check(record.check_id) or ViewCheck(
            # The view-pin test keeps every emitted check declared; a row an
            # older build wrote may still name one this build dropped.
            check_id=record.check_id,
            label=record.check_id,
            chip=record.check_id,
        )
        if record.comparison == "state":
            token = str(record.observed)
            observed_text = wording.states.get(token, token)
            needs = wording.needs
            chip = wording.chip
        else:
            needs = _needs(record.comparison, record.threshold, wording)
            if record.observed is None:
                observed_text, chip = _NOT_AVAILABLE, wording.chip
            else:
                observed_text = _observed(record.observed, wording)
                chip = f"{wording.chip} {observed_text}"
        return ExplainedCheckView(
            check_id=record.check_id,
            role=record.role,
            label=wording.label,
            chip=chip,
            observed_text=observed_text,
            needs=needs,
            passed=record.passed,
            applies=(record.role == "entry") != holding,
        )

    def _number(self, value: int | float | ChartParamRef) -> float:
        resolved = getattr(self.params, value.field) if isinstance(value, ChartParamRef) else value
        return float(resolved)


_OPERATOR_TEXT = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<"}


def _observed(value: int | float | str, wording: ViewCheck) -> str:
    """The observed number at the check's precision, signed when the check is a spread."""
    number = float(value)
    sign = "+" if wording.signed else ""
    return f"{number:{sign}.{wording.decimals}f}{wording.unit}"


def _bound(value: float, wording: ViewCheck) -> str:
    """A threshold as the owner set it: whole numbers stay whole (50, not 50.0)."""
    return f"{int(value)}" if float(value).is_integer() else f"{value:.{wording.decimals}f}"


def _needs(comparison: str, threshold: int | float | list[float] | None, wording: ViewCheck) -> str:
    """What passing requires, derived from the recorded operator and bound."""
    if threshold is None:
        return ""
    if isinstance(threshold, list):
        low, high = threshold
        return f"in {_bound(low, wording)}–{_bound(high, wording)}{wording.unit}"
    return f"{_OPERATOR_TEXT[comparison]} {_bound(threshold, wording)}{wording.unit}"


__all__ = ["ResolvedStrategyView", "StrategyViewUnavailableError"]

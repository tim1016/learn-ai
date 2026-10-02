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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
        params = registration.param_schema(**{**(strategy_params or {}), "symbol": symbol})
        return cls(strategy_key=strategy_key, registration=registration, view=registration.strategy_view, params=params)

    @property
    def settings(self) -> dict[str, Any]:
        """The deployed settings by name, as templates and custom gates read them.

        Read field by field: a parameter dump may omit a field sitting at its
        default (EMA's lengths, #2696), and a label still needs its value.
        """
        return {name: getattr(self.params, name) for name in type(self.params).model_fields}

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
            checks=[self._check_view(check, record.holding, settings) for check in record.checks],
        )

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

    def _check_view(
        self, record: ExplainedCheckRecord, holding: bool, settings: Mapping[str, Any]
    ) -> ExplainedCheckView:
        wording: ViewCheck | None = self.view.check(record.check_id)
        applies = (record.role == "entry") != holding
        if wording is None:
            # The view-pin test keeps every emitted check declared; a row
            # written by an older build may still name one this build dropped.
            return ExplainedCheckView(
                check_id=record.check_id,
                role=record.role,
                label=record.check_id,
                chip=record.check_id,
                observed_text=_NOT_AVAILABLE if record.observed is None else str(record.observed),
                needs="",
                passed=record.passed,
                applies=applies,
            )
        fields = {**settings, **_threshold_fields(record.threshold), "observed": record.observed}
        if isinstance(record.observed, str):
            observed_text = wording.states.get(record.observed, record.observed)
            chip = wording.chip.format(**fields)
        elif record.observed is None:
            observed_text = _NOT_AVAILABLE
            chip = wording.label
        else:
            observed_text = wording.observed.format(**fields)
            chip = wording.chip.format(**fields)
        return ExplainedCheckView(
            check_id=record.check_id,
            role=record.role,
            label=wording.label,
            chip=chip,
            observed_text=observed_text,
            needs=wording.needs.format(**fields),
            passed=record.passed,
            applies=applies,
        )

    def _number(self, value: int | float | ChartParamRef) -> float:
        resolved = getattr(self.params, value.field) if isinstance(value, ChartParamRef) else value
        return float(resolved)


def _threshold_fields(threshold: int | float | list[float] | None) -> dict[str, Any]:
    if isinstance(threshold, list):
        return {"low": threshold[0], "high": threshold[1], "threshold": threshold}
    return {"threshold": threshold}


__all__ = ["ResolvedStrategyView", "StrategyViewUnavailableError"]

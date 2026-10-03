"""Each strategy view names exactly the indicators its sealed contract seals (#2639).

The view declaration is content, not sealed identity, so nothing in admission
holds it to the contract. This does: every series the contract seals is a
drawn value with a catalogue twin of the same length, and no drawn twin names
a series the contract does not seal. It holds at the validated point and at a
point off it, because the view and the seal both follow the deployed
parameters (#2796).
"""

from __future__ import annotations

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.strategy_view import ChartParamRef
from tests._helpers.signal_program import sealed_series_points, series_point_id


@pytest.mark.parametrize(("program_key", "overrides"), sealed_series_points(), ids=series_point_id)
def test_the_view_draws_exactly_the_sealed_series(program_key: str, overrides: dict[str, int]) -> None:
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    view = registration.strategy_view
    assert contract is not None and view is not None
    validated = registration.param_schema(**{**contract.validated_settings, "symbol": "SPY"})
    params = registration.param_schema(**{**contract.validated_settings, **overrides, "symbol": "SPY"})
    sealed = {series.name: series for series in contract.resolved_signals(params)}

    twins = {value.key: value.catalogue for value in view.values if value.catalogue is not None}

    def resolved(setting: int | float | ChartParamRef) -> int | float:
        if not isinstance(setting, ChartParamRef):
            return setting
        # Off the validated point, a period left where it was would prove nothing.
        assert not overrides or getattr(params, setting.field) != getattr(validated, setting.field), setting.field
        return getattr(params, setting.field)

    assert set(twins) == set(sealed)
    for key, twin in twins.items():
        assert twin is not None
        if "length" in twin.params:
            period = resolved(twin.params["length"])
        else:
            # MACD has no one length: it seals the bar its signal EMA turns ready.
            assert twin.name == "macd", f"{program_key}.{key}: nothing ties this twin to its sealed period"
            period = resolved(twin.params["slow"]) + resolved(twin.params["signal"]) - 1
        assert period == sealed[key].period, f"{program_key}.{key}"

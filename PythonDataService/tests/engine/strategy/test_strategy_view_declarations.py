"""Each strategy view names exactly the indicators its sealed contract seals (#2639).

The view declaration is content, not sealed identity, so nothing in admission
holds it to the contract. This does: at the validated point, every series the
contract seals is a drawn value with a catalogue twin of the same length, and
no drawn twin names a series the contract does not seal.
"""

from __future__ import annotations

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.strategy_view import ChartParamRef

PROGRAM_KEYS = sorted(key for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None)


@pytest.mark.parametrize("program_key", PROGRAM_KEYS)
def test_the_view_draws_exactly_the_sealed_series(program_key: str) -> None:
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    view = registration.strategy_view
    assert contract is not None and view is not None
    params = registration.param_schema(**{**contract.validated_settings, "symbol": "SPY"})
    sealed = {series.name: series for series in contract.resolved_signals(params)}

    twins = {value.key: value.catalogue for value in view.values if value.catalogue is not None}

    assert set(twins) == set(sealed)
    for key, twin in twins.items():
        assert twin is not None
        length = twin.params.get("length")
        if length is None:
            continue  # MACD seals its signal-EMA warmup, not one length
        resolved = getattr(params, length.field) if isinstance(length, ChartParamRef) else length
        assert resolved == sealed[key].period, f"{program_key}.{key}"

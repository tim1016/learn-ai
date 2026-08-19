"""Conservative indicator warmup derived from catalog type metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

INDICATOR_WARMUP_MULTIPLIER = 5
MINIMUM_INDICATOR_LOOKBACK = 200

IndicatorParameter = Mapping[str, object]
IndicatorCatalog = Mapping[str, Sequence[IndicatorParameter]]


def configured_indicator_warmup_bars(indicator_configs: IndicatorCatalog) -> int:
    """Cover every catalog-valid integer window at its configured maximum.

    Parameter names are deliberately irrelevant. The catalog's existing
    ``type="int"`` metadata defines a potentially bar-count-bearing value, so a
    newly named integer window automatically widens this conservative budget.
    Float parameters such as standard-deviation or ATR multipliers do not.
    """
    maximum_lookback = MINIMUM_INDICATOR_LOOKBACK
    for definitions in indicator_configs.values():
        for definition in definitions:
            if definition.get("type") != "int":
                continue
            upper_bound = definition.get("max")
            if isinstance(upper_bound, bool) or not isinstance(upper_bound, int):
                raise ValueError("integer indicator parameters require an integer maximum")
            maximum_lookback = max(maximum_lookback, upper_bound)
    return maximum_lookback * INDICATOR_WARMUP_MULTIPLIER

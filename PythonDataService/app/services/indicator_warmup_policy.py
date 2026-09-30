"""Conservative indicator warmup derived from catalog type metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

INDICATOR_WARMUP_MULTIPLIER = 5
MINIMUM_INDICATOR_LOOKBACK = 200

IndicatorParameter = Mapping[str, object]
IndicatorCatalog = Mapping[str, Sequence[IndicatorParameter]]
#: What a caller actually asked to compute: ``{"name": ..., "params": {...}}``.
IndicatorEntry = Mapping[str, object]


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


def requested_indicator_warmup_lookback(indicator_entries: Sequence[IndicatorEntry]) -> int:
    """The largest whole-number window among the recipes a caller requested.

    Name-independent like :func:`configured_indicator_warmup_bars`: any
    integer-valued parameter is a potential bar-count-bearing window, and a
    float counts only when it is whole — a fractional multiplier or
    standard-deviation never widens the budget, a whole-valued one (3.0) only
    ever over-covers it. Unlike the catalog helper there is no floor — the
    requested recipes alone decide how far back a lead-in reaches (#2611), so
    an EMA-20 daily chart reads ~100 sessions of lead-in instead of the
    ~1,000 a fixed 200-bar floor forced on every daily, weekly and monthly
    chart.

    Returns the raw lookback, not warm-up bars:
    :func:`dataset_service.resolve_indicator_window` applies
    :data:`INDICATOR_WARMUP_MULTIPLIER` — the multiplier lives in one place.
    """
    maximum_lookback = 0
    for entry in indicator_entries:
        for value in entry.get("params", {}).values():
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                maximum_lookback = max(maximum_lookback, value)
            elif isinstance(value, float) and value.is_integer():
                maximum_lookback = max(maximum_lookback, int(value))
    return maximum_lookback

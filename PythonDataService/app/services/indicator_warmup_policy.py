"""Indicator warm-up budgets: the catalog-wide budget and a request's own lead-in.

:func:`configured_indicator_warmup_bars` covers every catalog-valid recipe at
its maximum. :func:`requested_indicator_warmup_lookback` sizes the Data Lab
lead-in (chart, dataset export, reliability, quality report)
from the recipes a caller actually asked for (#2611).

Sizing a request (#2611; owner decision 2026-09-30, "reduce the accuracy
needed at warmup, judiciously")
------------------------------------------------------------------------

Before #2611 every request warmed up on at least a 200-bar lookback — 1,000
bars at :data:`INDICATOR_WARMUP_MULTIPLIER` — whatever it asked for. A shorter
lead-in leaves a finite-window indicator exactly as it was, but an indicator
that smooths recursively still carries a share of the state it started the
lead-in with (its seed), so its first visible values can move. How fast that
share decays depends on how the indicator smooths, so every catalog indicator
belongs to one :class:`WarmupFamily`, and the family's memory factor ``m``
scales the indicator's own length ``N`` (its largest whole-number parameter,
missing parameters filled from the catalog defaults) before
``dataset_service.resolve_indicator_window`` applies the ×5 — the lead-in is
``5 × m × N`` bars:

* Finite window, m = 1. The value reads only its last ``N`` bars (a chained
  smoother such as Stochastic's reads ``k + d + 1`` ≤ ``5N``), so it equals
  today's value at ``atol=1e-9, rtol=0``.
* EMA-smoothed, m = 1. An EMA keeps ``(1 − α)^k`` of its seed after ``k``
  bars, α = 2/(N + 1); at ``k = 5N`` that is ``((N − 1)/(N + 1))^{5N} ≤
  e^{−10} ≈ 4.5e-5``.
* Wilder-smoothed, m = 2. Wilder's RMA is an EMA with α = 1/N — the EMA of
  span ``2N − 1`` — so it forgets half as fast: ``(1 − 1/N)^{5N} ≈ e^{−5} ≈
  6.7e-3`` of its seed survives 5N bars, which moved RSI-14 by 0.76 points.
  ``m = 2`` gives it the EMA's own tail, ``(1 − 1/N)^{10N} ≤ e^{−10}``.
* Double-Wilder-smoothed, m = 3. ADX smooths DX — built from Wilder-smoothed
  directional movement and ATR — with a second RMA; the first stage's seed
  reaches the output through the second as ``(k/N)·e^{−k/N}`` (a repeated
  pole), so ``m = 2`` still leaves ``10·e^{−10} ≈ 4.5e-4`` of it, and
  ``m = 3`` leaves ``15·e^{−15} ≈ 4.6e-6``.
* Path-dependent and hidden-memory: no factor, today's lead-in exactly,
  ``max(N, MINIMUM_INDICATOR_LOOKBACK)``. A cumulative sum (OBV, A/D, VWAP)
  never forgets; PSAR's and Supertrend's trend latches carry a state no
  lead-in bounds; and the memory of a hidden-memory indicator is set by
  something the catalog does not expose (KAMA's slow=30, StochRSI's
  rsi_length=14, TSI's signal=13, Fisher's fixed 0.67/0.5 recursion,
  Squeeze's momentum windows).

The bound this buys, measured against today's 1,000-bar lead-in: at most
~0.01 points on a 0–100 oscillator and ~1e-4 of the value on a price scale
(an accepted departure, ADR 0069 §8, whose receipt holds the per-indicator
table and the derivation's check). At its edges:

* Anything the families do not size keeps today's lead-in: an indicator the
  catalog does not hold, a request setting a parameter the catalog does not
  expose (a smoothing mode or a second length can change the memory), and a
  request with no whole-number length. A catalog indicator without a family
  fails ``test_every_catalog_indicator_has_a_warmup_family``.
* ``m × N`` exceeds today's 200-bar floor only where today's lead-in was too
  short for the family — an ADX longer than 66 bars, an RMA longer than 100
  (this subsumes the indicator table's old ``adx_length × 2`` widening).
  There the lead-in grows and the values move toward the fully warmed value
  by at most the residual today's lead-in left.

The live bots' warm-up does not use this sizing:
:func:`configured_indicator_warmup_bars` is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import NotRequired, TypedDict

INDICATOR_WARMUP_MULTIPLIER = 5
MINIMUM_INDICATOR_LOOKBACK = 200

IndicatorParameter = Mapping[str, object]
IndicatorCatalog = Mapping[str, Sequence[IndicatorParameter]]


class IndicatorEntry(TypedDict):
    """What a caller asked to compute — the entry ``calculate_dynamic_indicators`` takes."""

    name: str
    params: NotRequired[Mapping[str, object]]


class WarmupFamily(Enum):
    """How an indicator forgets the history before its lead-in (#2611)."""

    FINITE_WINDOW = "finite_window"
    EMA_SMOOTHED = "ema_smoothed"
    WILDER_SMOOTHED = "wilder_smoothed"
    DOUBLE_WILDER_SMOOTHED = "double_wilder_smoothed"
    PATH_DEPENDENT = "path_dependent"
    HIDDEN_MEMORY = "hidden_memory"


#: The multiple of an indicator's own length its lookback is sized on. A family
#: absent here keeps today's lookback (module docstring).
_MEMORY_FACTORS: Mapping[WarmupFamily, int] = {
    WarmupFamily.FINITE_WINDOW: 1,
    WarmupFamily.EMA_SMOOTHED: 1,
    WarmupFamily.WILDER_SMOOTHED: 2,
    WarmupFamily.DOUBLE_WILDER_SMOOTHED: 3,
}

#: Every Data Lab catalog indicator (``dataset_service.INDICATOR_CONFIGS``), by
#: how the pinned pandas-ta 0.4.71b0 smooths it with the catalog's parameters.
INDICATOR_WARMUP_FAMILIES: Mapping[str, WarmupFamily] = {
    **dict.fromkeys(
        (
            "sma",
            "wma",
            "hma",
            "alma",
            "bbands",
            "stoch",
            "cci",
            "willr",
            "roc",
            "mom",
            "donchian",
            "aroon",
            "cmf",
            "mfi",
        ),
        WarmupFamily.FINITE_WINDOW,
    ),
    # NATR smooths its true range with an EMA in pandas-ta, not Wilder's RMA.
    **dict.fromkeys(("ema", "dema", "tema", "zlma", "macd", "kc", "natr"), WarmupFamily.EMA_SMOOTHED),
    **dict.fromkeys(("rma", "rsi", "atr"), WarmupFamily.WILDER_SMOOTHED),
    "adx": WarmupFamily.DOUBLE_WILDER_SMOOTHED,
    # Supertrend smooths its ATR with Wilder's RMA, but its band latch is PSAR's kind of state.
    **dict.fromkeys(("obv", "ad", "vwap", "psar", "supertrend"), WarmupFamily.PATH_DEPENDENT),
    **dict.fromkeys(("kama", "stochrsi", "tsi", "fisher", "squeeze"), WarmupFamily.HIDDEN_MEMORY),
}


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


def requested_indicator_warmup_lookback(indicator_entries: Iterable[IndicatorEntry], catalog: IndicatorCatalog) -> int:
    """The lookback the requested recipes warm up on: the largest entry's.

    Formula: per entry, N = the largest whole-number parameter after the
      catalog defaults fill the missing ones; lookback = m × N for a family
      with memory factor m, else max(N, MINIMUM_INDICATOR_LOOKBACK) — today's.
    Reference: repository-internal policy (module docstring; owner decision
      2026-09-30 on #2611).
    Canonical implementation: this file.
    Validated against: tests/services/test_indicator_warmup_policy.py (family
      sizing, defaults, the fallbacks, catalog coverage);
      tests/services/test_data_lab_chart_indicator_warmup.py (warmed values vs
      today's 1,000-bar lead-in at each family's documented tolerance).

    Returns a lookback, not warm-up bars: ``resolve_indicator_window`` applies
    :data:`INDICATOR_WARMUP_MULTIPLIER`, so the multiplier lives in one place.
    """
    return max((_entry_lookback(entry, catalog) for entry in indicator_entries), default=0)


def _entry_lookback(entry: IndicatorEntry, catalog: IndicatorCatalog) -> int:
    name = entry.get("name", "")
    requested = entry.get("params", {})
    definitions = catalog.get(name, ())
    defaults = {definition["name"]: definition["default"] for definition in definitions}
    length = _largest_whole_number({**defaults, **requested}.values())
    family = INDICATOR_WARMUP_FAMILIES.get(name)
    if family not in _MEMORY_FACTORS or not length or not requested.keys() <= defaults.keys():
        return max(length, MINIMUM_INDICATOR_LOOKBACK)
    return length * _MEMORY_FACTORS[family]


def _largest_whole_number(values: Iterable[object]) -> int:
    """The largest bar count among ``values``: ints and whole floats; never a bool or a fraction."""
    whole = [
        int(value)
        for value in values
        if not isinstance(value, bool) and (isinstance(value, int) or (isinstance(value, float) and value.is_integer()))
    ]
    return max(whole, default=0)

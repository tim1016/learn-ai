"""What is particular to each study's record: its question, the instruments and window it covers, its headline.

A record keeps the inputs the run was asked and the result it returned; everything the History shows about it is read
from those by the study's spec, so a record never stores a sentence that a later wording change would leave stale. A
spec reads what it needs with ``.get`` and tolerates a missing or odd field: the result is the study's own, it changes
on the study's schedule, and a headline that cannot be built is a row without a headline, never a record that is lost.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.research.study_records.models import StudyType
from app.utils.session_anchors import et_midnight_ms

Inputs = Mapping[str, Any]
Settled = tuple[Literal["completed", "partial", "failed"], str | None]
"""How a result the study delivered ended, and why when it did not end cleanly."""


@dataclass(frozen=True, slots=True)
class StudySpec:
    study: StudyType
    label: str
    """The store in a phrase, for a sentence about it: "saved Feature study runs"."""
    question: Callable[[Inputs], str]
    symbols: Callable[[Inputs], tuple[str, ...]]
    window: Callable[[Inputs], tuple[int, int] | None]
    headline: Callable[[Mapping[str, Any]], str | None]
    settle: Callable[[Mapping[str, Any]], Settled]


# ── Reading a result without trusting its shape ─────────────────────────────────────────────────


def _dig(value: Any, *path: str) -> Any:
    """``value[path[0]][path[1]]...``, or ``None`` as soon as a step is not a mapping holding that key."""
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _number(value: Any, spec: str, *, signed: bool = False) -> str:
    """A finite number formatted, or "unavailable" for anything else (a null is how JSON stores a NaN)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return "unavailable"
    return format(value, f"+{spec}" if signed else spec)


def _count(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "an unknown number of"


def _stage(result: Mapping[str, Any], *path: str) -> str:
    info = _dig(result, *path)
    stage, label = _dig(info, "stage"), _dig(info, "label")
    if stage is None:
        return "no stage reported"
    return f"stage {stage}" + (f" ({label})" if label else "")


def _failed_if_unsuccessful(result: Mapping[str, Any]) -> Settled:
    if result.get("success") is False:
        reason = result.get("error")
        return "failed", reason if isinstance(reason, str) and reason else "the study reported no result"
    return "completed", None


def _upper(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("a record's inputs name no symbol")
    return value.upper()


def _span_of_dates(inputs: Inputs) -> tuple[int, int]:
    """The requested window: the ET midnights the History shows as dates, the way the run stores keep theirs."""
    return et_midnight_ms(date.fromisoformat(inputs["from_date"])), et_midnight_ms(date.fromisoformat(inputs["to_date"]))


def _one_symbol(inputs: Inputs) -> tuple[str, ...]:
    return (_upper(inputs.get("symbol")),)


# ── Feature study ───────────────────────────────────────────────────────────────────────────────


def _feature_headline(result: Mapping[str, Any]) -> str:
    return (
        f"Mean IC {_number(result.get('mean_ic'), '.4f')}, NW t-stat {_number(result.get('nw_t_stat'), '.2f')}; "
        f"{_stage(result, 'validation_verdict', 'stage_info')}; {_count(result.get('bars_used'))} bars"
    )


FEATURE_STUDY = StudySpec(
    study="feature-study",
    label="saved Feature study runs",
    question=lambda i: f"Does {i['feature_name']} predict the next move of {_upper(i.get('symbol'))}?",
    symbols=_one_symbol,
    window=_span_of_dates,
    headline=_feature_headline,
    settle=_failed_if_unsuccessful,
)


# ── Signal study ────────────────────────────────────────────────────────────────────────────────


def _signal_headline(result: Mapping[str, Any]) -> str:
    grade, status = _dig(result, "graduation", "overall_grade"), _dig(result, "graduation", "status_label")
    graded = "no grade reported" if grade is None else f"Grade {grade}" + (f" ({status})" if status else "")
    return (
        f"{graded}; mean OOS Sharpe {_number(_dig(result, 'walk_forward', 'mean_oos_sharpe'), '.2f')}; "
        f"best threshold {_number(result.get('best_threshold'), 'g')}; {_count(result.get('bars_used'))} bars"
    )


SIGNAL_STUDY = StudySpec(
    study="signal-study",
    label="saved Signal study runs",
    question=lambda i: f"Does a trading signal built on {i['feature_name']} hold up out of sample on {_upper(i.get('symbol'))}?",
    symbols=_one_symbol,
    window=_span_of_dates,
    headline=_signal_headline,
    settle=_failed_if_unsuccessful,
)


# ── Cross-sectional study ───────────────────────────────────────────────────────────────────────

_CROSS_TARGETS = {
    "directional": "the next day's direction",
    "volatility": "the next five days' volatility",
    "abs_return": "the next day's absolute return",
}


def _members(inputs: Inputs) -> tuple[str, ...]:
    symbols = inputs.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("a cross-sectional record's inputs name no universe")
    return tuple(dict.fromkeys(_upper(symbol) for symbol in symbols))


def _cross_question(inputs: Inputs) -> str:
    members = len(_members(inputs))
    target = _CROSS_TARGETS.get(str(inputs.get("target_type")), "what it does next")
    return f"Does {inputs['feature_name']} predict {target} across {members} {'instrument' if members == 1 else 'instruments'}?"


def _cross_headline(result: Mapping[str, Any]) -> str:
    tested = result.get("tickers_tested_raw", result.get("tickers_tested"))
    return (
        f"Aggregate IC {_number(result.get('aggregate_ic'), '.4f')}; {_count(result.get('tickers_passed'))} of "
        f"{_count(result.get('tickers_valid'))} evaluated members passed ({_count(tested)} asked); "
        f"{_stage(result, 'stage_info')}"
    )


def _cross_settle(result: Mapping[str, Any]) -> Settled:
    """Partial when a member of the universe was not evaluated: the aggregate is over fewer members than were asked."""
    unsuccessful = _failed_if_unsuccessful(result)
    if unsuccessful[0] == "failed":
        return unsuccessful
    asked, evaluated = result.get("tickers_tested_raw", result.get("tickers_tested")), result.get("tickers_valid")
    if not isinstance(asked, int) or not isinstance(evaluated, int) or evaluated >= asked:
        return "completed", None
    reasons = [
        f"{count} {words}"
        for key, words in (("errored", "errored"), ("invalid_iv", "had no usable IV"), ("invalid_data", "had no data"))
        if isinstance(count := _dig(result, "validity_summary", key), int) and count > 0
    ]
    return "partial", f"{evaluated} of {asked} members evaluated" + (f": {', '.join(reasons)}" if reasons else "")


CROSS_SECTIONAL_STUDY = StudySpec(
    study="cross-sectional-study",
    label="saved Cross-sectional study runs",
    question=_cross_question,
    symbols=_members,
    window=_span_of_dates,
    headline=_cross_headline,
    settle=_cross_settle,
)


# ── Indicator reliability ───────────────────────────────────────────────────────────────────────


def _indicator_name(inputs: Inputs) -> str:
    params = inputs.get("indicator_params")
    shown = ", ".join(f"{key}={value}" for key, value in params.items()) if isinstance(params, Mapping) else ""
    return f"{inputs['indicator_name']}({shown})" if shown else str(inputs["indicator_name"])


def _reliability_headline(result: Mapping[str, Any]) -> str:
    best = result.get("best_horizon")
    row = next((r for r in result.get("results") or [] if _dig(r, "horizon") == best), None)
    verdict = _dig(result, "verdict")
    said = (
        f"; {_dig(verdict, 'direction')}, {str(_dig(verdict, 'strength')).lower()}"
        if _dig(verdict, "direction") and _dig(verdict, "strength")
        else ""
    )
    if row is None:
        return f"No horizon stood out{said}"
    return (
        f"Best horizon {best} bars: in-sample IC {_number(_dig(row, 'is_mean_ic'), '.4f')}, "
        f"out-of-sample IC {_number(_dig(row, 'oos_mean_ic'), '.4f')}{said}"
    )


INDICATOR_RELIABILITY = StudySpec(
    study="indicator-reliability",
    label="saved Indicator reliability runs",
    question=lambda i: f"Is {_indicator_name(i)} on {_upper(i.get('symbol'))} a reliable predictor of what price does next?",
    symbols=_one_symbol,
    window=_span_of_dates,
    headline=_reliability_headline,
    settle=_failed_if_unsuccessful,
)


# ── Realized vs IV ──────────────────────────────────────────────────────────────────────────────

_IV_SOURCES = {
    "caller_supplied": "supplied with the request",
    "recorder": "the recorder",
    "absent": "none, so realized volatility alone",
}


def _rviv_window(inputs: Inputs) -> tuple[int, int] | None:
    bars = inputs.get("bars")
    first, last = _dig(bars, "first_ts_ms"), _dig(bars, "last_ts_ms")
    return (first, last) if isinstance(first, int) and isinstance(last, int) else None


def _rviv_headline(result: Mapping[str, Any]) -> str:
    coverage = _dig(result, "coverage")
    source = _IV_SOURCES.get(str(_dig(result, "iv_source")), "unknown")
    return (
        f"{_count(_dig(coverage, 'n_bars'))} bars; IV from {source}; "
        f"{_count(_dig(coverage, 'forward_nan_bars'))} without a forward window"
    )


REALIZED_VS_IV = StudySpec(
    study="realized-vs-iv",
    label="saved Realized vs IV runs",
    question=lambda i: (
        f"Is implied volatility richer or cheaper than the volatility {_upper(i.get('symbol'))} then realizes? "
        f"({i['tenor_days']}-day tenor, {i['bar_size']} bars)"
    ),
    symbols=_one_symbol,
    window=_rviv_window,
    headline=_rviv_headline,
    settle=_failed_if_unsuccessful,
)


STUDY_SPECS: Mapping[StudyType, StudySpec] = {
    spec.study: spec
    for spec in (FEATURE_STUDY, SIGNAL_STUDY, CROSS_SECTIONAL_STUDY, INDICATOR_RELIABILITY, REALIZED_VS_IV)
}
"""Every study that keeps records, by type. ``tests`` assert it covers ``STUDY_TYPES`` exactly."""

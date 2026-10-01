"""Capability declarations, quantization and the one canonical point."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY, public_params_schema
from app.research.golden_search.declarations import (
    NO_DECLARATION_REASON,
    SearchKnob,
    canonical_point,
    declaration_for,
    is_quantized,
    point_hash,
    quantize,
    unavailable_reason,
    violates,
)
from app.research.sweep.eligibility import sweep_eligibility

EMA = "ema_crossover_signal"
_EMA_DECLARATION = declaration_for(EMA)
assert _EMA_DECLARATION is not None
_EMA_SCHEMA = public_params_schema(_STRATEGY_REGISTRY[EMA])["properties"]
_TRACK_E_ABSENT = "fast_period" not in _EMA_SCHEMA
_needs_track_e = pytest.mark.skipif(
    _TRACK_E_ABSENT,
    reason="the EMA fast/slow/hold parameters (#2696 Track E) are not in this branch's EMA parameter schema",
)


def test_ema_declaration_follows_the_owner_order_and_table() -> None:
    rows = [
        (
            k.name,
            k.kind,
            k.domain_low,
            k.domain_high,
            k.quantum,
            k.default_low,
            k.default_high,
            k.neighbor_step,
            k.default_step,
            k.searchable_by_default,
        )
        for k in _EMA_DECLARATION.knobs
    ]

    D = Decimal
    assert rows == [
        ("gap", "decimal", D("0"), D("2"), D("0.01"), D("0"), D("0.6"), D("0.05"), D("0.05"), True),
        ("rsi_min", "decimal", D("0"), D("100"), D("1"), D("30"), D("60"), D("2"), D("1"), True),
        ("rsi_max", "decimal", D("0"), D("100"), D("1"), D("60"), D("90"), D("2"), D("1"), True),
        ("fast_period", "integer", D("2"), D("30"), D("1"), D("3"), D("12"), D("1"), D("1"), True),
        ("slow_period", "integer", D("3"), D("40"), D("1"), D("8"), D("30"), D("1"), D("1"), True),
        ("hold_bars", "integer", D("1"), D("26"), D("1"), D("2"), D("12"), D("1"), D("1"), True),
        ("gap_bps", "decimal", D("0"), D("100"), D("0.5"), D("0"), D("5"), D("0.5"), D("0.5"), False),
    ]
    assert [k.name for k in _EMA_DECLARATION.knobs if k.warmup_dependent] == ["fast_period", "slow_period"]
    assert [(f.label, f.value) for f in _EMA_DECLARATION.fixed] == [("RSI length", "14"), ("Decision cadence", "15 minutes")]
    assert [(c.left, c.op, c.right) for c in _EMA_DECLARATION.constraints] == [
        ("fast_period", "<", "slow_period"),
        ("rsi_min", "<", "rsi_max"),
    ]
    assert _EMA_DECLARATION.default_pair_audits == (("fast_period", "slow_period"), ("rsi_min", "rsi_max"))


@pytest.mark.parametrize("knob", _EMA_DECLARATION.knobs, ids=lambda k: k.name)
def test_ema_knob_agrees_with_the_registered_parameter_model(knob: SearchKnob) -> None:
    if knob.name not in _EMA_SCHEMA:
        pytest.skip(f"{knob.name} is not in this branch's EMA parameter schema (#2696 Track E)")
    schema = _EMA_SCHEMA[knob.name]

    assert schema["type"] == ("integer" if knob.kind == "integer" else "number")
    assert Decimal(str(schema["default"])) == knob.default_value
    assert Decimal(str(schema["minimum"])) <= knob.domain_low
    if "maximum" in schema:
        assert knob.domain_high <= Decimal(str(schema["maximum"]))


@pytest.mark.parametrize("knob", _EMA_DECLARATION.knobs, ids=lambda k: k.name)
def test_ema_knob_ranges_and_steps_sit_on_its_quantum_lattice(knob: SearchKnob) -> None:
    for value in (
        knob.domain_low,
        knob.domain_high,
        knob.default_low,
        knob.default_high,
        knob.neighbor_step,
        knob.default_step,
        knob.default_value,
    ):
        assert is_quantized(knob, value), value
    assert knob.default_step > 0
    assert knob.domain_low <= knob.default_low < knob.default_high <= knob.domain_high
    assert knob.domain_low <= knob.default_value <= knob.domain_high


def test_ema_declaration_names_only_declared_searchable_knobs_in_rules_and_audits() -> None:
    names = {k.name for k in _EMA_DECLARATION.knobs}
    searchable = {k.name for k in _EMA_DECLARATION.knobs if k.searchable_by_default}

    assert all({c.left, c.right} <= names for c in _EMA_DECLARATION.constraints)
    assert all({a, b} <= searchable for a, b in _EMA_DECLARATION.default_pair_audits)
    assert violates(_EMA_DECLARATION, {k.name: k.default_value for k in _EMA_DECLARATION.knobs}) is None


def test_unavailable_reason_explains_every_registered_strategy() -> None:
    assert unavailable_reason(EMA) is None
    for key, registration in _STRATEGY_REGISTRY.items():
        if key == EMA:
            continue
        reason = unavailable_reason(key)
        assert declaration_for(key) is None
        if sweep_eligibility(registration).eligible:
            assert reason == NO_DECLARATION_REASON
        else:
            assert reason and reason != NO_DECLARATION_REASON
    assert unavailable_reason("deployment_validation") == "This strategy is an operational harness, not a production candidate."
    assert unavailable_reason("no_such_program") == "No strategy named 'no_such_program' is registered."


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("gap", "0.225", "0.22"),  # half-even: 22.5 hundredths -> 22
        ("gap", "0.235", "0.24"),  # half-even: 23.5 hundredths -> 24
        ("gap", "0.30000000000000004", "0.3"),
        ("gap", "2.5", "2"),  # clamped to the domain
        ("gap", "-0.4", "0"),
        ("gap_bps", "7.25", "7"),  # half-even on a 0.5 quantum: 14.5 halves -> 14
        ("gap_bps", "7.75", "8"),
        ("fast_period", "6.5", "6"),
        ("fast_period", "41", "30"),
    ],
)
def test_quantize_rounds_half_even_to_the_quantum_then_clamps(name: str, value: str, expected: str) -> None:
    assert quantize(_EMA_DECLARATION.knob(name), Decimal(value)) == Decimal(expected)


def test_violates_reads_omitted_knobs_at_their_defaults() -> None:
    assert violates(_EMA_DECLARATION, {"fast_period": 10}) == "The fast EMA length must be shorter than the slow EMA length."
    assert violates(_EMA_DECLARATION, {"fast_period": 9}) is None
    assert violates(_EMA_DECLARATION, {"rsi_min": 70}) == "The RSI lower gate must be below the RSI upper gate."


def test_canonical_point_snaps_decimals_and_includes_the_symbol() -> None:
    point = canonical_point(EMA, "QQQ", {"gap": 0.30000000000000004, "rsi_min": 45})

    assert point == {"symbol": "QQQ", "gap": 0.3, "gap_bps": 0.0, "rsi_min": 45.0, "rsi_max": 70.0}
    assert canonical_point(EMA, "QQQ", {"gap": 0.304}) == canonical_point(EMA, "QQQ", {"gap": 0.3})


def test_canonical_point_refuses_out_of_domain_and_unknown_inputs() -> None:
    with pytest.raises(ValueError, match="outside its domain"):
        canonical_point(EMA, "SPY", {"gap": 2.5})
    with pytest.raises(ValueError, match="outside its domain"):
        canonical_point(EMA, "SPY", {"rsi_min": 1e300})
    with pytest.raises(ValueError, match="unknown strategy"):
        canonical_point("no_such_program", "SPY", {})
    with pytest.raises(ValueError, match="not a number"):
        canonical_point(EMA, "SPY", {"gap": "wide"})
    with pytest.raises(ValueError):
        canonical_point(EMA, "SPY", {"rsi_min": 80})  # the model refuses rsi_min >= rsi_max


def test_point_hash_ignores_key_order() -> None:
    point = canonical_point(EMA, "SPY", {"gap": 0.35})

    assert point_hash(EMA, point) == point_hash(EMA, dict(reversed(list(point.items()))))
    assert point_hash(EMA, point) != point_hash(EMA, {**point, "symbol": "QQQ"})


@_needs_track_e
def test_canonical_point_types_integer_knobs_and_omits_identity_neutral_defaults() -> None:
    at_defaults = canonical_point(EMA, "SPY", {"fast_period": 5.0, "slow_period": 10, "hold_bars": 5, "gap": 0.2})
    moved = canonical_point(EMA, "SPY", {"fast_period": 7.0, "slow_period": 21, "hold_bars": 5})

    assert at_defaults == {"gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0, "symbol": "SPY"}
    assert moved["fast_period"] == 7 and isinstance(moved["fast_period"], int)
    assert moved["slow_period"] == 21 and isinstance(moved["slow_period"], int)
    assert "hold_bars" not in moved
    assert canonical_point(EMA, "SPY", moved) == moved

"""Custom Dark Bright Gates: parsing, refusal, resolution and judgement (#2639 D8–D10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.decision_explanation import DecisionExplanationRecord
from app.schemas.strategy_gates import CustomGate, CustomGateInput, GateCandle, GateTerm
from app.services.strategy_gates import (
    DRAFT_GATE_ID,
    GateExpressionError,
    VariableSource,
    compile_gate,
    evaluate_gates,
    gate_catalogue,
    parse_linear,
    resolve_variable,
)
from app.services.strategy_view import ResolvedStrategyView
from scripts.fixture_generators.ema_decision_explanation_2026_09_29 import replay

EXP_001_INPUT = Path(__file__).resolve().parents[1] / "fixtures/golden/strategy-explanation/EXP-001/v1/input.json"


def _ema_view(**params: float) -> ResolvedStrategyView:
    return ResolvedStrategyView.for_settings("ema_crossover_signal", params or None, symbol="SPY")


@pytest.fixture(scope="module")
def candles() -> list[GateCandle]:
    """The 2026-09-29 bot's decision candles with its own recorded values."""
    minutes = json.loads(EXP_001_INPUT.read_text(encoding="utf-8"))["minutes"]
    out: list[GateCandle] = []
    for bar, decision in replay(minutes):
        record = DecisionExplanationRecord.from_decision(bar, decision)
        assert record is not None
        out.append(
            GateCandle(
                bar_close_ms=record.bar.end_ms,
                open=record.bar.open,
                high=record.bar.high,
                low=record.bar.low,
                close=record.bar.close,
                volume=record.bar.volume,
                values=dict(record.values),
            )
        )
    return out


def _saved(expression: str, sign: str = "gt", gate_id: str = "g-000000000001") -> CustomGate:
    parsed = parse_linear(expression)
    return CustomGate(
        gate_id=gate_id,
        strategy_key="ema_crossover_signal",
        label="test",
        expression=expression,
        sign=sign,  # type: ignore[arg-type]
        terms=[GateTerm(coefficient=c, variable=v) for c, v in parsed.terms],
        constant=parsed.constant,
        created_at_ms=0,
        updated_at_ms=0,
    )


@pytest.mark.parametrize(
    ("text", "terms", "constant"),
    [
        ("EMA5 − EMA10 − gap", ((1.0, "EMA5"), (-1.0, "EMA10"), (-1.0, "gap")), 0.0),
        ("2*(close - EMA10) + 0.5", ((2.0, "close"), (-2.0, "EMA10")), 0.5),
        ("RSI14/2 - 25", ((0.5, "RSI14"),), -25.0),
        ("-(EMA5 - EMA5) + close", ((1.0, "close"),), 0.0),
    ],
)
def test_a_linear_expression_reads_as_terms_plus_a_constant(text: str, terms: tuple, constant: float) -> None:
    parsed = parse_linear(text)

    assert (parsed.terms, parsed.constant) == (terms, constant)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("EMA5 * EMA10", "cannot multiply one variable by another"),
        ("EMA5 / close", "divide only by a number"),
        ("EMA5 / 0", "divides by zero"),
        ("EMA5 - EMA10 > 0", "Leave the comparison out"),
        ("3 + 4", "uses no variable"),
        ("(EMA5 - EMA10", "never closed"),
        ("EMA5 $ 2", "cannot appear"),
        ("EMA5 -", "ends too soon"),
        # A number past float range would be saved as null and break every strategy's gates.
        ("1" + "0" * 330 + " * EMA5", "too large to judge"),
        ("EMA5 / 0." + "0" * 309 + "1", "too large to judge"),
        # Only ASCII digits are numbers.
        ("\u0663 * EMA5", "cannot appear"),
    ],
)
def test_an_invalid_gate_is_refused_with_a_plain_reason(text: str, reason: str) -> None:
    with pytest.raises(GateExpressionError, match=reason):
        parse_linear(text)


def test_variables_resolve_in_the_order_the_owner_was_promised() -> None:
    view = _ema_view()

    assert resolve_variable(view, "EMA5").source is VariableSource.RECORDED
    assert resolve_variable(view, "rsi14").source is VariableSource.RECORDED
    assert resolve_variable(view, "gap").source is VariableSource.SETTING
    assert resolve_variable(view, "close").source is VariableSource.CANDLE
    assert resolve_variable(view, "EMA20").source is VariableSource.CATALOGUE
    assert resolve_variable(view, "VWAP").source is VariableSource.CATALOGUE


def test_a_catalogue_name_the_bot_records_is_the_bots_own_value_only_at_its_length() -> None:
    assert resolve_variable(_ema_view(), "EMA5").source is VariableSource.RECORDED
    # Deployed at 8/21, EMA5 is no longer a recorded value; EMA8 is.
    deployed = _ema_view(fast_period=8, slow_period=21)
    assert resolve_variable(deployed, "EMA5").source is VariableSource.CATALOGUE
    assert resolve_variable(deployed, "EMA8").source is VariableSource.RECORDED


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("FOO", "not a value, a setting"),
        ("EMA", "needs a length"),
        ("MACD12", "more than one setting"),
        ("VWAP14", "takes no length"),
        ("EMA9999", "length must be between"),
        ("symbol", "not a number"),
        # Each draws more than one line, so there is no one number to read.
        ("AROON25", "more than one line"),
        ("STOCHRSI14", "more than one line"),
        ("FISHER9", "more than one line"),
        ("ADX14", "more than one line"),
    ],
)
def test_an_unknown_variable_is_refused_with_a_plain_reason(name: str, reason: str) -> None:
    with pytest.raises(GateExpressionError, match=reason):
        resolve_variable(_ema_view(), name)


def test_one_saved_gate_follows_each_bots_deployed_settings(candles: list[GateCandle]) -> None:
    gate = _saved("EMA5 - EMA10 - gap")

    at_020, _, _ = evaluate_gates(_ema_view(gap=0.20), [gate], candles, symbol="SPY")
    at_030, _, _ = evaluate_gates(_ema_view(gap=0.30), [gate], candles, symbol="SPY")

    bright_020 = sum(1 for result in at_020[gate.gate_id] if result)
    bright_030 = sum(1 for result in at_030[gate.gate_id] if result)
    assert bright_020 > bright_030 > 0


def test_below_zero_is_the_other_side_of_above_zero(candles: list[GateCandle]) -> None:
    above, below = _saved("RSI14 - 50", "gt", "g-000000000001"), _saved("RSI14 - 50", "lt", "g-000000000002")

    results, chart_computed, _ = evaluate_gates(_ema_view(), [above, below], candles, symbol="SPY")

    pairs = [
        (a, b)
        for a, b, candle in zip(results[above.gate_id], results[below.gate_id], candles, strict=True)
        if a is not None and candle.values["rsi"] != 50
    ]
    assert pairs and all(a != b for a, b in pairs)
    # Unready bars had no RSI: neither bright nor dark.
    assert results[above.gate_id][0] is None
    assert chart_computed == []


def test_a_catalogue_variable_is_computed_on_the_decision_candles_and_marked(candles: list[GateCandle]) -> None:
    gate = _saved("close - EMA20")

    results, chart_computed, notices = evaluate_gates(_ema_view(), [gate], candles, symbol="SPY")

    assert chart_computed == ["EMA20"]
    assert notices == []
    judged = results[gate.gate_id]
    assert judged[0] is None  # EMA20 not ready on the first candle
    assert {True, False} <= set(judged[25:])


def test_a_draft_is_judged_but_a_broken_draft_is_refused(candles: list[GateCandle]) -> None:
    results, _, _ = evaluate_gates(
        _ema_view(), [], candles, symbol="SPY", draft=CustomGateInput(label="d", expression="close - open", sign="gt")
    )
    assert len(results[DRAFT_GATE_ID]) == len(candles)

    with pytest.raises(GateExpressionError, match="not a value"):
        evaluate_gates(
            _ema_view(), [], candles, symbol="SPY", draft=CustomGateInput(label="d", expression="NOPE", sign="gt")
        )


def test_a_saved_gate_that_no_longer_resolves_is_reported_not_raised(candles: list[GateCandle]) -> None:
    stale = _saved("EMA5 - FOO")

    results, _, notices = evaluate_gates(_ema_view(), [stale], candles, symbol="SPY")

    assert set(results[stale.gate_id]) == {None}
    assert notices == [
        "A saved gate could not be judged for these settings: 'FOO' is not a value, a setting, a candle field or a catalogue indicator."
    ]


def test_names_that_read_the_same_number_are_one_term() -> None:
    view = _ema_view()

    def compiled(expression: str) -> list[tuple[float, str]]:
        terms, _constant = compile_gate(view, CustomGateInput(label="t", expression=expression, sign="gt"))
        return [(term.coefficient, term.variable) for term in terms]

    assert compiled("EMA5 - ema5 + close") == [(1.0, "close")]
    assert compiled("EMA20 + ema20") == [(2.0, "EMA20")]
    with pytest.raises(GateExpressionError, match="cancel out"):
        compiled("EMA5 - ema5")


def test_the_gate_catalogue_offers_only_one_line_indicators_with_no_setting_or_a_length() -> None:
    offered = {indicator.variable for indicator in gate_catalogue()}

    assert {"EMA10", "SMA20", "RSI14", "VWAP", "OBV"} <= offered
    assert not offered & {"AROON25", "STOCHRSI14", "FISHER9", "ADX14"}
    assert not any(variable.startswith("MACD") for variable in offered)

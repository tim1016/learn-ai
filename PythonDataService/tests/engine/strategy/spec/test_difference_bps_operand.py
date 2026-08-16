"""Golden and boundary tests for the DifferenceBps strategy operand."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.engine.strategy.spec.primitives import EvalContext, evaluate_operand
from app.engine.strategy.spec.schema import Operand


@dataclass
class _ReadyIndicator:
    current_value: Decimal
    is_ready: bool = True


def _context(left: str, right: str) -> EvalContext:
    return EvalContext(
        indicators={
            "left": _ReadyIndicator(Decimal(left)),
            "right": _ReadyIndicator(Decimal(right)),
        },
        current_bar_count=1,
        bar_close_time=datetime(2024, 1, 2, tzinfo=UTC),
        bar_close_price=Decimal(right),
    )


def _operand() -> Operand:
    return TypeAdapter(Operand).validate_python(
        {
            "kind": "DifferenceBps",
            "left": {"kind": "IndicatorRef", "indicator": "left"},
            "right": {"kind": "IndicatorRef", "indicator": "right"},
        }
    )


def test_difference_bps_matches_decimal_golden_fixture() -> None:
    fixture_dir = Path(__file__).resolve().parents[3] / "fixtures" / "golden" / "spy-ema-difference-bps"
    expected_rows = json.loads((fixture_dir / "output.json").read_text(encoding="utf-8"))

    for row in expected_rows:
        if row.get("error") == "division_by_zero":
            with pytest.raises(ZeroDivisionError, match="denominator evaluated to zero"):
                evaluate_operand(_operand(), _context(row["left"], row["right"]))
        else:
            actual = evaluate_operand(_operand(), _context(row["left"], row["right"]))
            assert actual == Decimal(row["difference_bps"])


def test_difference_bps_returns_none_during_indicator_warmup() -> None:
    ctx = _context("500.20", "500.00")
    ctx.indicators["left"].is_ready = False

    assert evaluate_operand(_operand(), ctx) is None


def test_difference_bps_rejects_zero_denominator() -> None:
    with pytest.raises(ZeroDivisionError, match="denominator evaluated to zero"):
        evaluate_operand(_operand(), _context("1", "0"))


def test_difference_bps_rejects_non_indicator_operands() -> None:
    with pytest.raises(ValueError):
        TypeAdapter(Operand).validate_python(
            {
                "kind": "DifferenceBps",
                "left": {"kind": "Const", "value": 1},
                "right": {"kind": "IndicatorRef", "indicator": "right"},
            }
        )

"""Tests for spec-derived decision-column descriptors (#396)."""

from __future__ import annotations

from pathlib import Path

from app.engine.strategy.spec import schema as spec_schema
from app.engine.strategy.spec.descriptors import (
    _format_for_dtype,
    decision_column_descriptors,
    humanize_column,
)
from app.engine.strategy.spec.schema import load_spec_from_path

FIXTURE = Path(spec_schema.__file__).parent / "fixtures" / "spy_ema_crossover.spec.json"


def test_humanize_column() -> None:
    assert humanize_column("ema5") == "EMA 5"
    assert humanize_column("ema10") == "EMA 10"
    assert humanize_column("rsi") == "RSI"
    assert humanize_column("supertrend") == "Supertrend"


def test_format_for_dtype() -> None:
    assert _format_for_dtype("float64") == "decimal"
    assert _format_for_dtype("int64") == "integer"
    assert _format_for_dtype("bool") == "boolean"
    assert _format_for_dtype("string") == "text"


def test_descriptors_from_spec() -> None:
    spec = load_spec_from_path(FIXTURE)
    by_name = {d["name"]: d for d in decision_column_descriptors(spec)}
    assert set(by_name) == {"ema5", "ema10", "rsi"}
    assert by_name["ema5"] == {
        "name": "ema5",
        "label": "EMA 5",
        "type": "float64",
        "format": "decimal",
        "semantic": by_name["ema5"]["semantic"],
    }
    assert by_name["ema5"]["semantic"]  # carried through from the spec

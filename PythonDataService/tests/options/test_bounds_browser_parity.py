"""Keep the browser's decimal-bound fixture pinned to the canonical Bounds.fault."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.options.bounds import Bounds

_FIXTURE = Path(__file__).resolve().parents[3] / "Frontend/src/app/components/broker/broker-deploy-page/option-structure/option-structure.bounds.parity.json"


def test_browser_bounds_fixture_matches_python_canonical() -> None:
    fixture = json.loads(_FIXTURE.read_text())
    assert fixture["schema_version"] == 1
    assert fixture["canonical"] == "PythonDataService/app/options/bounds.py::Bounds.fault"
    for case in fixture["cases"]:
        raw = case["bounds"]
        bounds = Bounds(
            lowest=None if raw["min"] is None else Decimal(raw["min"]),
            lowest_included=raw["min_included"],
            highest=None if raw["max"] is None else Decimal(raw["max"]),
            highest_included=raw["max_included"],
            step=None if raw["step"] is None else Decimal(raw["step"]),
        )
        try:
            accepted = bounds.fault(Decimal(case["value"])) is None
        except (InvalidOperation, ValueError):
            accepted = False
        assert accepted is case["accepted"], case["name"]

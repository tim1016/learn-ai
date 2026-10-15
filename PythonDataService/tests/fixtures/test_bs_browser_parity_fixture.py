"""Golden BS-008: the browser parity fixture holds what the canonical returns.

``Frontend/src/app/utils/black-scholes.ts`` is a browser copy of ``bs_greeks.py`` and
``strategy_engine.py`` (ADR 0068 exception 1). ``black-scholes.parity.spec.ts`` holds it to
``Frontend/src/testing/bs-parity/grid.json``. These tests hold that file to the canonical, so
the browser is never checked against a stale or hand-edited answer.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from scripts.fixture_generators.bs_browser_parity import FRONTEND_COPY, build_documents

FIXTURE_DIR = Path(__file__).parent / "golden" / "options-pricing" / "BS-008" / "v1"
# Manifest BS-008: strict float. The canonical is re-evaluated here, so only platform ulps are
# left to allow for. A flipped last digit of a 4 or 6 dp rounding (1e-4, 1e-6) is far above
# this and fails: it means the canonical moved, and the fixture is to be regenerated.
ATOL = 1e-9


def _stored_output() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))


def _assert_same(stored: Any, canonical: Any, where: str) -> None:
    if isinstance(canonical, dict):
        assert isinstance(stored, dict), where
        assert stored.keys() == canonical.keys(), where
        for key, value in canonical.items():
            _assert_same(stored[key], value, f"{where}.{key}")
    elif isinstance(canonical, list):
        assert isinstance(stored, list), where
        assert len(stored) == len(canonical), where
        for index, (stored_item, canonical_item) in enumerate(zip(stored, canonical, strict=True)):
            _assert_same(stored_item, canonical_item, f"{where}[{index}]")
    elif isinstance(canonical, float):
        assert math.isclose(stored, canonical, rel_tol=0.0, abs_tol=ATOL), (
            f"{where}: stored {stored!r}, canonical {canonical!r}; regenerate with "
            '`DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.fixture_generators.bs_browser_parity`'
        )
    else:
        assert stored == canonical, where


def test_stored_values_are_what_the_canonical_returns() -> None:
    _, canonical = build_documents()

    _assert_same(_stored_output(), canonical, "output")


def test_frontend_copy_is_the_stored_output_byte_for_byte() -> None:
    assert FRONTEND_COPY.read_bytes() == (FIXTURE_DIR / "output.json").read_bytes()


def _strategy(name: str) -> dict[str, Any]:
    return next(item for item in _stored_output()["strategies"] if item["name"] == name)


def test_a_leg_without_iv_is_worth_its_intrinsic_value_before_expiry() -> None:
    # What the browser is held to (#3221): no IV, no time value, so the current-time
    # P&L is the expiry payoff, -6 / -6 / -1 / +4 / +9 for a 95 call bought at 6 over 90..110.
    strategy = _strategy("long_call_95_without_iv")

    assert strategy["prices"] == [90.0, 95.0, 100.0, 105.0, 110.0]
    assert strategy["expected"]["pnl"]["now"] == [-6.0, -6.0, -1.0, 4.0, 9.0]
    assert strategy["expected"]["pnl"]["now"] == strategy["expected"]["expiry_payoff"]


def test_ten_vol_points_off_legs_under_ten_floors_them_at_zero_vol() -> None:
    # #3221: a 5% and a 4% vol leg shifted -10 points are not priced at a negative or a 1%
    # vol; both are at zero vol, so worth intrinsic, and the P&L is the expiry payoff.
    strategy = _strategy("long_straddle_100_low_iv")

    assert strategy["expected"]["pnl"]["iv_down_10"] == strategy["expected"]["expiry_payoff"]
    assert strategy["expected"]["pnl"]["now"] != strategy["expected"]["expiry_payoff"]

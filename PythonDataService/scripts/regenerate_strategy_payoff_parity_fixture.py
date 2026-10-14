"""Regenerate the Strategy Builder's payoff parity fixture (#3186).

The Strategy Builder re-reads a structure's max profit, max loss and
breakevens in the browser on every leg edit (ADR 0068 exception 1). The
canonical is ``app/services/strategy_engine.py`` (``compute_max_profit_loss``
and ``find_breakevens``). This script runs the canonical over ``CASES`` and
writes what it answers to ``Frontend/src/testing/payoff-parity/cases.json``:

- ``Frontend/src/app/utils/strategy-payoff.parity.spec.ts`` asserts the
  browser copy gives those answers.
- ``tests/contracts/test_strategy_payoff_parity_fixture.py`` regenerates the
  fixture from the live canonical on every run and requires the committed file
  to match byte for byte, so neither a hand edit nor a change to the canonical
  that skips this script passes.
- ``tests/test_strategy_engine.py`` holds the hand-derived answer for each
  case by name, so the canonical is checked against arithmetic, not only
  against itself.

Usage::

    DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.regenerate_strategy_payoff_parity_fixture
"""

from __future__ import annotations

import json
import logging
import math
import sys
from pathlib import Path
from typing import Final

from app.models.strategy import StrategyLeg
from app.services.strategy_engine import compute_max_profit_loss, find_breakevens

logger = logging.getLogger(__name__)

FIXTURE_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "Frontend" / "src" / "testing" / "payoff-parity" / "cases.json"
)

# name -> legs as (strike, option_type, position, premium, quantity).
CASES: Final[dict[str, list[tuple[float, str, str, float, int]]]] = {
    "short_put_590_at_5": [(590, "put", "short", 5.0, 1)],
    "short_put_590_at_5_times_3": [(590, "put", "short", 5.0, 3)],
    "long_call_100_at_5": [(100, "call", "long", 5.0, 1)],
    "short_call_100_at_5": [(100, "call", "short", 5.0, 1)],
    "bull_put_spread_590_580": [(590, "put", "short", 5.0, 1), (580, "put", "long", 2.0, 1)],
    "wide_bull_call_spread_400_600": [(400, "call", "long", 110.0, 1), (600, "call", "short", 10.0, 1)],
    "long_strangle_570_610_at_10": [(570, "put", "long", 10.0, 1), (610, "call", "long", 10.0, 1)],
    "long_straddle_100": [(100, "call", "long", 5.0, 1), (100, "put", "long", 4.0, 1)],
    "synthetic_long_100": [(100, "call", "long", 5.0, 1), (100, "put", "short", 4.0, 1)],
    "ratio_call_spread_100_110x2": [(100, "call", "long", 6.0, 1), (110, "call", "short", 2.5, 2)],
    "iron_condor_wide_wings": [
        (480, "put", "long", 1.0, 1),
        (500, "put", "short", 3.0, 1),
        (520, "call", "short", 3.0, 1),
        (540, "call", "long", 1.0, 1),
    ],
    # The payoff reaches zero at 105 and falls again: it touches zero, never crosses.
    "touches_zero_at_a_strike": [(100, "call", "long", 10.0, 1), (105, "call", "short", 2.5, 2)],
    # Zero from 105 to 110, then rising: the crossing is where zero is first reached.
    "crosses_zero_along_a_flat_stretch": [
        (100, "call", "long", 5.0, 1),
        (105, "call", "short", 0.0, 1),
        (110, "call", "long", 0.0, 1),
    ],
}

# Python rounds what it returns to 4 dp (an error of at most 5e-5); the browser
# copy does not round. 1e-4, one 4-dp step, bounds that with margin.
_TOLERANCE: Final[dict[str, object]] = {
    "atol": 1e-4,
    "rtol": 0.0,
    "rationale": (
        "strategy_engine.py rounds max profit, max loss and breakevens to 4 dp (error <= 5e-5); "
        "strategy-payoff.ts does not round. atol is one 4-dp step."
    ),
}


def _legs(legs: list[tuple[float, str, str, float, int]]) -> list[StrategyLeg]:
    # `iv` takes no part in an expiry payoff; the model requires one.
    return [
        StrategyLeg(
            strike=strike, option_type=option_type, position=position, premium=premium, iv=0.25, quantity=quantity
        )
        for strike, option_type, position, premium, quantity in legs
    ]


def _finite_or_none(value: float) -> float | None:
    """A side with no limit is ``None``: JSON has no infinity."""
    return None if math.isinf(value) else value


def build_fixture() -> dict[str, object]:
    """The fixture, deterministically ordered as ``CASES``."""
    cases = []
    for name, raw_legs in CASES.items():
        legs = _legs(raw_legs)
        extremes = compute_max_profit_loss(legs)
        cases.append(
            {
                "name": name,
                "legs": [
                    {
                        "strike": strike,
                        "option_type": option_type,
                        "position": position,
                        "premium": premium,
                        "quantity": quantity,
                    }
                    for strike, option_type, position, premium, quantity in raw_legs
                ],
                "expected": {
                    "max_profit": _finite_or_none(extremes.max_profit),
                    "max_loss": _finite_or_none(extremes.max_loss),
                    "breakevens": find_breakevens(legs),
                },
            }
        )
    return {
        "schema_version": 1,
        "generated_by": "PythonDataService/scripts/regenerate_strategy_payoff_parity_fixture.py",
        "source": "PythonDataService/app/services/strategy_engine.py (compute_max_profit_loss, find_breakevens)",
        "unit": "per share, expiry payoff; max_profit/max_loss null means no limit",
        "tolerance": _TOLERANCE,
        "cases": cases,
    }


def render() -> str:
    """The fixture's file text, as written."""
    return json.dumps(build_fixture(), indent=2) + "\n"


def main() -> int:
    """CLI entry point: rewrite the committed fixture."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(render(), encoding="utf-8")
    logger.info("wrote fixture", extra={"path": str(FIXTURE_PATH), "cases": len(CASES)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

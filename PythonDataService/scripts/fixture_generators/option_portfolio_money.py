"""Generate OSM-002 without importing the canonical requirement implementation.

Formula: sum over underlyings of the maximum expiry's exercise-loss requirement.
Reference: Alpaca, Options Level 3 Trading, maintenance-margin steps 1–5,
  https://docs.alpaca.markets/docs/options-level-3-trading, read 2026-10-08.
Canonical implementation: app/options/requirement.py (not imported here).
Validated against: tests/fixtures/test_option_portfolio_money_fixture.py;
  each hand-written expectation is checked by OSM-001's independent Fraction scan.
"""

from __future__ import annotations

import json
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from scripts.fixture_generators.option_structure_money import _leg, _scanned_low, _whole


def _held(
    position: str, strike: str, *, expiry: str = "261218", ratio: int = 1, root: str = "SPY", right: str = "call",
) -> dict[str, Any]:
    return _leg(f"{root}-{expiry}-{right}-{strike}-{position}", position, right, strike, expiry=expiry, ratio=ratio) | {"root": root}


CASES: list[dict[str, Any]] = [
    {
        "case": "independently_bounded_expiries_take_the_maximum",
        "legs": [
            _held("short", "4"), _held("long", "9"),
            _held("short", "4", expiry="270115", ratio=2), _held("long", "14", expiry="270115", ratio=2),
        ],
        "expected": {"broker_requirement_cents": 200_000},
    },
    {
        "case": "six_calls_sum_three_credit_spreads",
        "legs": [leg for strike, cover in (("4", "9"), ("10", "15"), ("16", "21")) for leg in (
            _held("short", strike), _held("long", cover),
        )],
        "expected": {"broker_requirement_cents": 150_000},
    },
    {
        "case": "six_calls_allow_same_expiry_offsets",
        "legs": [
            _held("long", "1"), _held("short", "2"), _held("short", "9"), _held("long", "10"),
            _held("long", "11"), _held("short", "12"),
        ],
        "expected": {"broker_requirement_cents": 0},
    },
    {
        "case": "opposed_spreads_cannot_offset_across_expiries",
        "legs": [
            _held("short", "4"), _held("long", "9"),
            _held("long", "4", expiry="270115"), _held("short", "9", expiry="270115"),
        ],
        "expected": {"broker_requirement_cents": 50_000},
    },
    {
        "case": "independent_underlyings_add_requirements",
        "legs": [leg for root in ("SPY", "QQQ") for leg in (_held("short", "4", root=root), _held("long", "9", root=root))],
        "expected": {"broker_requirement_cents": 100_000},
    },
    {
        "case": "later_call_does_not_cover_unbounded_earlier_call",
        "legs": [_held("short", "4"), _held("long", "9", expiry="270115")],
        "expected": {"refusal": "OPTION_STRUCTURE_LOSS_UNBOUNDED"},
    },
    {
        "case": "residual_sold_calls_remain_unbounded",
        "legs": [_held("short", "4", ratio=2), _held("long", "9")],
        "expected": {"refusal": "OPTION_STRUCTURE_LOSS_UNBOUNDED"},
    },
    {
        "case": "sold_put_is_at_its_worst_at_zero",
        "legs": [_held("short", "4", ratio=2, right="put")],
        "expected": {"broker_requirement_cents": 80_000},
    },
    {
        "case": "recorded_multiplier_scales_the_requirement",
        "multiplier": 10,
        "legs": [_held("short", "4"), _held("long", "9")],
        "expected": {"broker_requirement_cents": 5_000},
    },
    {"case": "nothing_held", "legs": [], "expected": {"broker_requirement_cents": 0}},
]


def _scanned(case: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for leg in case["legs"]:
        grouped[leg["root"], leg["expiry"]].append(leg)
    requirements: dict[str, list[int]] = defaultdict(list)
    for (root, _expiry), legs in grouped.items():
        low = _scanned_low(legs)
        if low is None:
            return {"refusal": "OPTION_STRUCTURE_LOSS_UNBOUNDED"}
        requirements[root].append(_whole(max(Fraction(0), -low) * case.get("multiplier", 100) * 100))
    return {"broker_requirement_cents": sum(max(expiries) for expiries in requirements.values())}


def main() -> None:
    for case in CASES:
        actual = _scanned(case)
        if actual != case["expected"]:
            raise SystemExit(f"{case['case']}: written {case['expected']}, scanned {actual}")
    destination = Path(__file__).parents[2] / "tests/fixtures/golden/option-structures/OSM-002/v1"
    destination.mkdir(parents=True, exist_ok=True)
    inputs = {"cases": [{key: value for key, value in case.items() if key != "expected"} for case in CASES]}
    outputs = {"cases": [{"case": case["case"], **case["expected"]} for case in CASES]}
    for name, payload in (("input.json", inputs), ("output.json", outputs)):
        (destination / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

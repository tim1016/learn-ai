"""Generate OSM-001 without importing the canonical option-structure money implementation.

Every expected figure is written out by hand below. Before the files are
written, each is checked against a brute-force scan of the structure's
exercise payoff at every thousandth of a dollar from zero to past the highest
strike, so a slip of the pen cannot become a fixture.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

MULTIPLIER = 100
EXPIRY = "261218"
LATER_EXPIRY = "270115"


def _leg(leg_id: str, position: str, right: str, strike: str, *, ratio: int = 1, expiry: str = EXPIRY) -> dict[str, Any]:
    return {"leg_id": leg_id, "position": position, "right": right, "strike": strike, "ratio": ratio, "expiry": expiry}


# ``quotes`` are [bid, ask] in dollars per share. Money is whole cents for the
# units requested. ``refusal`` names why no money is worked out at all.
CASES: list[dict[str, Any]] = [
    {
        "case": "alpaca_documented_four_calls_offset_to_nothing",
        "source": "Alpaca, Options Level 3 Trading: long $100 call, short $110 call, long $200 call, short $190 call; "
        "universal spread rule total $0",
        "legs": [
            _leg("a", "long", "call", "100"),
            _leg("b", "short", "call", "110"),
            _leg("c", "long", "call", "200"),
            _leg("d", "short", "call", "190"),
        ],
        "expected": {"broker_requirement_cents": 0},
    },
    {
        "case": "alpaca_documented_call_credit_spread",
        "source": "Alpaca, Options Level 3 Trading: long $200 call paid $10, short $190 call received $15; "
        "maintenance margin $1,000, net credit $500, total cost basis $500",
        "legs": [_leg("bought", "long", "call", "200"), _leg("sold", "short", "call", "190")],
        "leg_prices": {"bought": "10", "sold": "15"},
        "expected": {
            "broker_requirement_cents": 100_000,
            "net_entry_premium_cents": -50_000,
            "commitment_before_fees_cents": 50_000,
            "loss_at_expiry_cents": 50_000,
        },
    },
    {
        "case": "call_spread_bought_for_1_94",
        "source": "PRD #2913 worked case: premium +$194.00, requirement $0.00, commitment $194.00, loss up to $194.00",
        "legs": [_leg("bought", "long", "call", "450"), _leg("sold", "short", "call", "455")],
        "quotes": {"bought": ["4.06", "4.10"], "sold": ["2.16", "2.20"]},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 19_400,
            "commitment_before_fees_cents": 19_400,
            "loss_at_expiry_cents": 19_400,
            "round_trip_cost_cents": 800,
        },
    },
    {
        "case": "five_wide_call_spread_sold_for_3_07",
        "source": "PRD #2913 worked case: premium -$307.00, requirement $500.00, commitment $193.00, loss up to $193.00",
        "legs": [_leg("sold", "short", "call", "450"), _leg("bought", "long", "call", "455")],
        "quotes": {"sold": ["5.20", "5.26"], "bought": ["2.09", "2.13"]},
        "expected": {
            "broker_requirement_cents": 50_000,
            "net_entry_premium_cents": -30_700,
            "commitment_before_fees_cents": 19_300,
            "loss_at_expiry_cents": 19_300,
            "round_trip_cost_cents": 1_000,
        },
    },
    {
        "case": "bought_call",
        "legs": [_leg("call", "long", "call", "450")],
        "quotes": {"call": ["6.40", "6.46"]},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 64_600,
            "commitment_before_fees_cents": 64_600,
            "loss_at_expiry_cents": 64_600,
            "round_trip_cost_cents": 600,
        },
    },
    {
        "case": "bought_put_three_units",
        "units": 3,
        "legs": [_leg("put", "long", "put", "440")],
        "quotes": {"put": ["3.10", "3.15"]},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 94_500,
            "commitment_before_fees_cents": 94_500,
            "loss_at_expiry_cents": 94_500,
            "round_trip_cost_cents": 1_500,
        },
    },
    {
        "case": "long_straddle",
        "legs": [_leg("call", "long", "call", "450"), _leg("put", "long", "put", "450")],
        "quotes": {"call": ["6.40", "6.46"], "put": ["5.90", "5.97"]},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 124_300,
            "commitment_before_fees_cents": 124_300,
            "loss_at_expiry_cents": 124_300,
            "round_trip_cost_cents": 1_300,
        },
    },
    {
        "case": "long_strangle",
        "legs": [_leg("put", "long", "put", "440"), _leg("call", "long", "call", "460")],
        "quotes": {"put": ["3.10", "3.15"], "call": ["2.80", "2.86"]},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 60_100,
            "commitment_before_fees_cents": 60_100,
            "loss_at_expiry_cents": 60_100,
            "round_trip_cost_cents": 1_100,
        },
    },
    {
        "case": "bought_call_and_put_in_the_money_keep_an_exercise_value",
        "source": "hand-derived: a 440 call and a 460 put are together worth at least $20 a share at expiry, "
        "so the loss estimate is the premium less $2,000",
        "legs": [_leg("call", "long", "call", "440"), _leg("put", "long", "put", "460")],
        "leg_prices": {"call": "12.30", "put": "11.95"},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 242_500,
            "commitment_before_fees_cents": 242_500,
            "loss_at_expiry_cents": 42_500,
        },
    },
    {
        "case": "iron_condor_two_units",
        "units": 2,
        "legs": [
            _leg("sold_put", "short", "put", "440"),
            _leg("bought_put", "long", "put", "435"),
            _leg("sold_call", "short", "call", "460"),
            _leg("bought_call", "long", "call", "465"),
        ],
        "quotes": {
            "sold_put": ["3.10", "3.15"],
            "bought_put": ["2.20", "2.24"],
            "sold_call": ["2.80", "2.86"],
            "bought_call": ["1.86", "1.90"],
        },
        "expected": {
            "broker_requirement_cents": 100_000,
            "net_entry_premium_cents": -35_200,
            "commitment_before_fees_cents": 64_800,
            "loss_at_expiry_cents": 64_800,
            "round_trip_cost_cents": 3_800,
        },
    },
    {
        "case": "iron_butterfly",
        "legs": [
            _leg("sold_put", "short", "put", "450"),
            _leg("bought_put", "long", "put", "445"),
            _leg("sold_call", "short", "call", "450"),
            _leg("bought_call", "long", "call", "455"),
        ],
        "leg_prices": {"sold_put": "5.90", "bought_put": "3.80", "sold_call": "6.40", "bought_call": "4.10"},
        "expected": {
            "broker_requirement_cents": 50_000,
            "net_entry_premium_cents": -44_000,
            "commitment_before_fees_cents": 6_000,
            "loss_at_expiry_cents": 6_000,
        },
    },
    {
        "case": "call_butterfly",
        "legs": [
            _leg("low", "long", "call", "445"),
            _leg("body", "short", "call", "450", ratio=2),
            _leg("high", "long", "call", "455"),
        ],
        "leg_prices": {"low": "9.70", "body": "6.40", "high": "4.10"},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 10_000,
            "commitment_before_fees_cents": 10_000,
            "loss_at_expiry_cents": 10_000,
        },
    },
    {
        "case": "put_butterfly",
        "legs": [
            _leg("high", "long", "put", "455"),
            _leg("body", "short", "put", "450", ratio=2),
            _leg("low", "long", "put", "445"),
        ],
        "leg_prices": {"high": "9.10", "body": "5.90", "low": "3.80"},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 11_000,
            "commitment_before_fees_cents": 11_000,
            "loss_at_expiry_cents": 11_000,
        },
    },
    {
        "case": "broken_wing_call_butterfly_loses_past_its_body",
        "source": "hand-derived: wings 5 and 10 wide; above 460 the payoff rests at -$5 a share",
        "legs": [
            _leg("low", "long", "call", "445"),
            _leg("body", "short", "call", "450", ratio=2),
            _leg("high", "long", "call", "460"),
        ],
        "leg_prices": {"low": "9.70", "body": "6.40", "high": "2.86"},
        "expected": {
            "broker_requirement_cents": 50_000,
            "net_entry_premium_cents": -2_400,
            "commitment_before_fees_cents": 47_600,
            "loss_at_expiry_cents": 47_600,
        },
    },
    {
        "case": "half_dollar_strikes",
        "legs": [_leg("sold", "short", "put", "452.5"), _leg("bought", "long", "put", "450")],
        "leg_prices": {"sold": "7.05", "bought": "5.90"},
        "expected": {
            "broker_requirement_cents": 25_000,
            "net_entry_premium_cents": -11_500,
            "commitment_before_fees_cents": 13_500,
            "loss_at_expiry_cents": 13_500,
        },
    },
    {
        "case": "eighth_dollar_strikes_come_to_whole_cents",
        "legs": [_leg("sold", "short", "call", "2.125"), _leg("bought", "long", "call", "2.5")],
        "leg_prices": {"sold": "0.31", "bought": "0.12"},
        "expected": {
            "broker_requirement_cents": 3_750,
            "net_entry_premium_cents": -1_900,
            "commitment_before_fees_cents": 1_850,
            "loss_at_expiry_cents": 1_850,
        },
    },
    {
        "case": "sold_put_alone_is_at_its_worst_at_zero",
        "source": "hand-derived: nothing at or above the strike loses; at a price of zero the put is worth its strike",
        "legs": [_leg("sold", "short", "put", "450")],
        "leg_prices": {"sold": "5.90"},
        "expected": {
            "broker_requirement_cents": 4_500_000,
            "net_entry_premium_cents": -59_000,
            "commitment_before_fees_cents": 4_441_000,
            "loss_at_expiry_cents": 4_441_000,
        },
    },
    {
        "case": "put_ratio_spread_is_at_its_worst_at_zero_not_at_a_strike",
        "source": "hand-derived: one 450 put bought, two 440 puts sold. Payoff is +$10 at 440 and $0 at 450, "
        "and falls to -$430 a share at zero",
        "legs": [_leg("bought", "long", "put", "450"), _leg("sold", "short", "put", "440", ratio=2)],
        "expected": {"broker_requirement_cents": 4_300_000},
    },
    {
        "case": "call_ratio_spread_keeps_losing_above_its_last_strike",
        "source": "hand-derived: one 450 call bought, two 455 calls sold. Above 455 the payoff falls a dollar "
        "for every dollar the price rises",
        "legs": [_leg("bought", "long", "call", "450"), _leg("sold", "short", "call", "455", ratio=2)],
        "expected": {"refusal": "OPTION_STRUCTURE_LOSS_UNBOUNDED"},
    },
    {
        "case": "sold_call_alone_keeps_losing",
        "legs": [_leg("sold", "short", "call", "450")],
        "expected": {"refusal": "OPTION_STRUCTURE_LOSS_UNBOUNDED"},
    },
    {
        "case": "bought_legs_with_two_expiries_have_no_loss_estimate",
        "legs": [
            _leg("near", "long", "call", "450"),
            _leg("far", "long", "call", "450", expiry=LATER_EXPIRY),
        ],
        "leg_prices": {"near": "6.46", "far": "9.80"},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 162_600,
            "commitment_before_fees_cents": 162_600,
            "loss_at_expiry_cents": None,
        },
    },
    {
        "case": "sold_leg_covered_by_a_later_expiry_is_not_worked_out",
        "source": "PRD #2913: Alpaca's documents do not say how its per-expiry rule treats this",
        "legs": [
            _leg("near", "short", "call", "450"),
            _leg("far", "long", "call", "450", expiry=LATER_EXPIRY),
        ],
        "expected": {"refusal": "OPTION_STRUCTURE_MIXED_EXPIRY_WITH_SOLD_LEG"},
    },
    {
        "case": "credit_larger_than_the_width_is_refused",
        "source": "hand-derived: a $5-wide spread cannot honestly be sold for $5.10",
        "legs": [_leg("sold", "short", "call", "450"), _leg("bought", "long", "call", "455")],
        "leg_prices": {"sold": "5.30", "bought": "0.20"},
        "expected": {"refusal": "OPTION_STRUCTURE_COMMITMENT_NOT_POSITIVE"},
    },
    {
        "case": "narrow_legs_hide_a_costly_spread",
        "source": "PRD #2913 worked case: mids $5.00 and $4.90, each four cents wide; in for $0.14, out for $0.06, "
        "so $8 of a $14 commitment is the round trip",
        "legs": [_leg("bought", "long", "call", "450"), _leg("sold", "short", "call", "450.5")],
        "quotes": {"bought": ["4.98", "5.02"], "sold": ["4.88", "4.92"]},
        "expected": {
            "broker_requirement_cents": 0,
            "net_entry_premium_cents": 1_400,
            "commitment_before_fees_cents": 1_400,
            "loss_at_expiry_cents": 1_400,
            "round_trip_cost_cents": 800,
        },
    },
]


def _payoff(legs: list[dict[str, Any]], price: Fraction) -> Fraction:
    total = Fraction(0)
    for leg in legs:
        strike = Fraction(leg["strike"])
        exercise_value = price - strike if leg["right"] == "call" else strike - price
        sign = 1 if leg["position"] == "long" else -1
        total += sign * leg["ratio"] * max(exercise_value, Fraction(0))
    return total


def _scanned_low(legs: list[dict[str, Any]]) -> Fraction | None:
    """The lowest payoff per share, by trying every thousandth of a dollar; ``None`` when it never stops falling."""
    top = max(Fraction(leg["strike"]) for leg in legs)
    if _payoff(legs, top + 1000) < _payoff(legs, top + 1):
        return None
    steps = int(top * 1000) + 2000
    return min(_payoff(legs, Fraction(step, 1000)) for step in range(steps + 1))


def _entry_prices(case: dict[str, Any]) -> dict[str, Fraction] | None:
    if "leg_prices" in case:
        return {leg_id: Fraction(price) for leg_id, price in case["leg_prices"].items()}
    if "quotes" in case:
        by_leg = {leg["leg_id"]: leg for leg in case["legs"]}
        return {
            leg_id: Fraction(ask if by_leg[leg_id]["position"] == "long" else bid)
            for leg_id, (bid, ask) in case["quotes"].items()
        }
    return None


def _scanned(case: dict[str, Any]) -> dict[str, Any]:
    """The expected figures by brute force, to check the hand-written ones against."""
    legs, units = case["legs"], case.get("units", 1)
    one_expiry = len({leg["expiry"] for leg in legs}) == 1
    sold = any(leg["position"] == "short" for leg in legs)
    low = _scanned_low(legs)
    if low is None:
        return {"refusal": "OPTION_STRUCTURE_LOSS_UNBOUNDED"}
    if sold and not one_expiry:
        return {"refusal": "OPTION_STRUCTURE_MIXED_EXPIRY_WITH_SOLD_LEG"}
    in_cents = units * MULTIPLIER * 100
    scanned: dict[str, Any] = {"broker_requirement_cents": int(max(Fraction(0), -low) * in_cents)}
    prices = _entry_prices(case)
    if prices is None:
        return scanned
    premium = sum(
        (1 if leg["position"] == "long" else -1) * leg["ratio"] * prices[leg["leg_id"]] for leg in legs
    ) * in_cents
    if scanned["broker_requirement_cents"] + premium <= 0:
        return {"refusal": "OPTION_STRUCTURE_COMMITMENT_NOT_POSITIVE"}
    scanned["net_entry_premium_cents"] = int(premium)
    scanned["commitment_before_fees_cents"] = int(scanned["broker_requirement_cents"] + premium)
    scanned["loss_at_expiry_cents"] = int(max(Fraction(0), premium - low * in_cents)) if one_expiry else None
    if "quotes" in case:
        by_leg = {leg["leg_id"]: leg for leg in legs}
        scanned["round_trip_cost_cents"] = int(
            sum(by_leg[leg_id]["ratio"] * (Fraction(ask) - Fraction(bid)) for leg_id, (bid, ask) in case["quotes"].items())
            * in_cents
        )
    return scanned


def main() -> None:
    for case in CASES:
        scanned = _scanned(case)
        if scanned != case["expected"]:
            raise SystemExit(f"{case['case']}: written {case['expected']}, scanned {scanned}")
    destination = Path(__file__).parents[2] / "tests/fixtures/golden/option-structures/OSM-001/v1"
    destination.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "multiplier": MULTIPLIER,
        "cases": [{key: value for key, value in case.items() if key != "expected"} for case in CASES],
    }
    output_payload = {"cases": [{"case": case["case"], **case["expected"]} for case in CASES]}
    for filename, payload in (("input.json", input_payload), ("output.json", output_payload)):
        (destination / filename).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

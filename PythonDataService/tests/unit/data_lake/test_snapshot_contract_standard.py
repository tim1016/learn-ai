"""Whether a contract of the vendor's option-chain snapshot is standard, by the rule a stored listing applies.

Hand-worked: ``O:SPY250613C00375000`` is the SPY call struck at 375 expiring 2025-06-13, whose 16:00 New York
anchor is 20:00 UTC = 1749844800000 ms. An adjusted contract carries a digit in its root (``O:SPY1250613C00375000``).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.data_lake.option_listing import snapshot_contract_is_standard

EXPIRY_MS = 1_749_844_800_000

STANDARD: dict[str, Any] = {
    "underlying": "SPY",
    "ticker": "O:SPY250613C00375000",
    "contract_type": "call",
    "strike": 375.0,
    "expiry_ms": EXPIRY_MS,
    "exercise_style": "american",
    "shares_per_contract": 100,
}


def standard_with(**changes: Any) -> dict[str, Any]:
    return {**STANDARD, **changes}


def test_a_symbol_that_names_an_american_100_share_contract_of_the_underlying_is_standard():
    assert snapshot_contract_is_standard(**STANDARD) is True
    assert snapshot_contract_is_standard(**standard_with(exercise_style="American", contract_type="Call")) is True


def test_a_float_share_count_the_vendor_sends_is_read_as_the_whole_number_it_names():
    assert snapshot_contract_is_standard(**standard_with(shares_per_contract=100.0)) is True


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"ticker": "O:SPY1250613C00375000"}, "an adjusted root carries a digit"),
        ({"shares_per_contract": 150}, "a deliverable of other than 100 shares"),
        ({"shares_per_contract": 100.5}, "a share count that is no whole number"),
        ({"exercise_style": "european"}, "not American"),
        ({"ticker": "O:QQQ250613C00375000"}, "a symbol that names another underlying"),
        ({"ticker": "O:SPY250613P00375000"}, "a symbol that names the other right"),
        ({"strike": 380.0}, "a strike the symbol does not name"),
        ({"strike": 375.0004}, "a strike finer than a thousandth"),
        ({"expiry_ms": EXPIRY_MS + 7 * 86_400_000}, "an expiry the symbol does not name"),
    ],
)
def test_a_contract_is_not_standard_when_any_part_of_the_rule_fails(changes: dict[str, Any], why: str):
    assert snapshot_contract_is_standard(**standard_with(**changes)) is False, why


def test_a_class_share_underlying_is_standard_under_its_occ_root():
    contract = standard_with(underlying="BRK.B", ticker="O:BRKB250613C00375000")

    assert snapshot_contract_is_standard(**contract) is True


@pytest.mark.parametrize(
    "missing",
    ["ticker", "contract_type", "strike", "expiry_ms", "exercise_style", "shares_per_contract"],
)
def test_a_snapshot_that_left_out_a_fact_the_rule_needs_is_unknown_not_non_standard(missing: str):
    assert snapshot_contract_is_standard(**standard_with(**{missing: None})) is None

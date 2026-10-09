"""Golden FEE-002: the option fee model reproduces the hand-computed oracle exactly."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.option_fees import (
    OptionFillFees,
    fees_for_option_fill,
    option_fee_provision_cents,
    settle_option_session,
)
from app.broker.contract.models import OrderSide

FIXTURE_DIR = Path(__file__).parent / "golden" / "broker-fees" / "FEE-002" / "v1"
_TYPES = ("sec", "taf", "cat", "orf", "occ")


def _load() -> tuple[dict, dict]:
    fixture_input = json.loads((FIXTURE_DIR / "input.json").read_text(encoding="utf-8"))
    fixture_output = json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))
    return fixture_input, fixture_output


def _decimal_or_none(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _price(case: dict) -> OptionFillFees:
    return fees_for_option_fill(
        trade_date_ms=case["trade_date_ms"],
        side=OrderSide(case["side"]),
        contracts=Decimal(case["contracts"]),
        premium=Decimal(case["premium"]),
        multiplier=case["multiplier"],
    )


def test_fee_002_per_fill_accruals_match_oracle_exactly() -> None:
    fixture_input, fixture_output = _load()

    for case in fixture_input["fills"]:
        fees = _price(case)
        expected = fixture_output["fills"][case["case"]]
        # Exact Decimal equality — atol=0, rtol=0 (manifest FEE-002). A null is an unpinned rate.
        for fee_type in _TYPES:
            assert getattr(fees, fee_type) == _decimal_or_none(expected[fee_type]), (case["case"], fee_type)


def test_fee_002_day_settlements_match_oracle_exactly() -> None:
    fixture_input, fixture_output = _load()
    by_case = {case["case"]: case for case in fixture_input["fills"]}

    for name, cases in fixture_input["sessions"].items():
        settled = settle_option_session([_price(by_case[case]) for case in cases])
        expected = fixture_output["sessions"][name]
        assert {fee_type: getattr(settled, fee_type) for fee_type in _TYPES} == {
            fee_type: Decimal(expected[fee_type]) for fee_type in _TYPES
        }, name
        assert settled.total == Decimal(expected["total"]), name


def test_fee_002_the_same_sale_costs_a_cent_less_once_the_taf_is_paused() -> None:
    """The dated-rate case: 2026-09-30 against 2026-10-01, the first day FINRA's TAF is paused."""
    _, fixture_output = _load()
    before = Decimal(fixture_output["sessions"]["one_sale_last_day_before_taf_pause"]["total"])
    after = Decimal(fixture_output["sessions"]["one_sale_first_day_of_taf_pause"]["total"])
    assert (before, after) == (Decimal("0.11"), Decimal("0.10"))


def test_fee_002_entry_provisions_match_oracle_in_whole_cents() -> None:
    fixture_input, fixture_output = _load()
    by_case = {case["case"]: case for case in fixture_input["fills"]}

    for name, cases in fixture_input["provisions"].items():
        cents = option_fee_provision_cents([_price(by_case[case]) for case in cases])
        assert isinstance(cents, int)
        assert cents == fixture_output["provision_cents"][name], name

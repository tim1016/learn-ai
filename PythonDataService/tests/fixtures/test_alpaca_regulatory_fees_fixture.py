"""Golden FEE-001: the canonical fee model reproduces the hand-computed oracle exactly."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.regulatory_fees import FillFees, fees_for_fill, settle_session
from app.broker.contract.models import OrderSide
from app.utils.session_anchors import et_date_at_ms

FIXTURE_DIR = Path(__file__).parent / "golden" / "broker-fees" / "FEE-001" / "v1"


def _load() -> tuple[dict, dict]:
    fixture_input = json.loads((FIXTURE_DIR / "input.json").read_text(encoding="utf-8"))
    fixture_output = json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))
    return fixture_input, fixture_output


def _decimal_or_none(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _price(case: dict) -> FillFees:
    return fees_for_fill(
        trade_date=et_date_at_ms(case["trade_date_ms"]),
        side=OrderSide(case["side"]),
        quantity=Decimal(case["quantity"]),
        fill_price=Decimal(case["fill_price"]),
    )


def test_fee_001_per_fill_accruals_match_oracle_exactly() -> None:
    fixture_input, fixture_output = _load()

    for case in fixture_input["fills"]:
        fees = _price(case)
        expected = fixture_output["fills"][case["case"]]
        # Exact Decimal equality — atol=0, rtol=0 (manifest FEE-001).
        assert fees.sec == _decimal_or_none(expected["sec"]), case["case"]
        assert fees.taf == _decimal_or_none(expected["taf"]), case["case"]
        assert fees.cat == _decimal_or_none(expected["cat"]), case["case"]


def test_fee_001_session_settlement_matches_oracle_exactly() -> None:
    fixture_input, fixture_output = _load()
    by_case = {case["case"]: case for case in fixture_input["fills"]}

    settled = settle_session([_price(by_case[name]) for name in fixture_input["session"]["cases"]])

    expected = fixture_output["session"]
    assert (settled.sec, settled.taf, settled.cat) == (
        Decimal(expected["sec"]),
        Decimal(expected["taf"]),
        Decimal(expected["cat"]),
    )
    assert settled.total == Decimal(expected["total"]) == Decimal("44.83")

"""Golden-fixture parity test for ``IbkrEquityCommissionModel``.

The fixture under ``tests/fixtures/golden/ibkr-commission-tiered/`` encodes
IBKR's published US-equity fixed-tier fee schedule independently of the
model implementation (the expected fees are derived from the schedule, not
from the model). Comparisons use ``Decimal`` exact equality — the schedule
is exact rational arithmetic, so the bit-exact equivalence level applies
(``.claude/rules/numerical-rigor.md``).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.research.parity.ibkr_commission import IbkrEquityCommissionModel

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "golden"
    / "ibkr-commission-tiered"
)


def _load_cases() -> list[dict]:
    return json.loads((_FIXTURE_DIR / "cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["description"])
def test_commission_matches_published_schedule(case: dict) -> None:
    model = IbkrEquityCommissionModel()
    actual = model.fee(
        quantity=int(case["quantity"]),
        fill_price=Decimal(str(case["fill_price"])),
    )
    assert actual == Decimal(str(case["expected_fee"]))

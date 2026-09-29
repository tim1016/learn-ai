"""Golden parity for Alpaca account-day P&L (PNL-001)."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.broker.alpaca.clerk.live_envelope import AccountObservation  # noqa: E402
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_at  # noqa: E402
from app.broker.contract.models import BrokerActivity  # noqa: E402

FIXTURE_ID = "PNL-001"


def test_account_day_pnl_matches_the_hand_computed_alpaca_fixture() -> None:
    fixture = registry.get(FIXTURE_ID)
    inputs = json.loads(registry.resolve(FIXTURE_ID, "input").read_text(encoding="utf-8"))
    outputs = json.loads(registry.resolve(FIXTURE_ID, "output").read_text(encoding="utf-8"))
    expected_by_case = {case["case"]: case for case in outputs["cases"]}

    for case in inputs["cases"]:
        observation = AccountObservation(
            observed_at_ms=inputs["as_of_ms"],
            broker_cash_usd=float(case["current_equity_usd"]),
            cash_available_usd=float(case["current_equity_usd"]),
            equity_usd=float(case["current_equity_usd"]),
            last_equity_usd=float(case["prior_close_equity_usd"]),
            position_count=None,
        )
        cash_flows = [
            BrokerActivity(
                broker="alpaca",
                activity_id=flow["activity_id"],
                activity_type=flow["activity_type"],
                category="non_trade_activity",
                symbol=None,
                side=None,
                quantity=None,
                price=None,
                net_amount=float(flow["net_amount_usd"]),
                occurred_at_ms=flow["occurred_at_ms"],
                observed_at_ms=inputs["as_of_ms"],
            )
            for flow in case["cash_flows"]
        ]

        actual = day_pnl_at(
            observation=observation,
            cash_flows=cash_flows,
            now_ms=inputs["as_of_ms"],
        )
        expected = expected_by_case[case["case"]]

        assert actual.known is expected["known"]
        assert actual.day_start_ms == expected["day_start_ms"]
        assert actual.day_end_ms == expected["day_end_ms"]
        assert actual.cash_flow_count == expected["cash_flow_count"]
        for field in (
            "current_equity_usd",
            "prior_close_equity_usd",
            "net_cash_flow_usd",
            "total_usd",
        ):
            assert getattr(actual, field) == pytest.approx(
                float(expected[field]),
                abs=fixture.tolerance.atol,
                rel=fixture.tolerance.rtol,
            )
        # The owner's figure (#2586) is the reference's exact Decimal, bit-exact.
        assert actual.display_total_usd == Decimal(expected["total_usd"]), case["case"]

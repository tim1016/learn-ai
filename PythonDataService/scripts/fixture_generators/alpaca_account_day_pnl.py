"""Generate PNL-001 without importing the canonical day-P&L implementation."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

AS_OF_MS = 1_788_883_200_000
PRIOR_CLOSE_MS = 1_788_552_000_000

CASES: list[dict[str, Any]] = [
    {
        "case": "market_loss_without_cash_flow",
        "current_equity_usd": "95000.00",
        "prior_close_equity_usd": "100000.00",
        "cash_flows": [],
    },
    {
        "case": "deposit_is_not_profit",
        "current_equity_usd": "110000.00",
        "prior_close_equity_usd": "100000.00",
        "cash_flows": [
            {
                "activity_id": "deposit-1",
                "activity_type": "CSD",
                "net_amount_usd": "10000.00",
                "occurred_at_ms": PRIOR_CLOSE_MS + 1,
            }
        ],
    },
    {
        "case": "withdrawal_is_not_loss",
        "current_equity_usd": "90000.00",
        "prior_close_equity_usd": "100000.00",
        "cash_flows": [
            {
                "activity_id": "withdrawal-1",
                "activity_type": "CSW",
                "net_amount_usd": "-10000.00",
                "occurred_at_ms": PRIOR_CLOSE_MS + 1,
            }
        ],
    },
    {
        "case": "mixed_flows_preserve_market_loss",
        "current_equity_usd": "102345.67",
        "prior_close_equity_usd": "100000.00",
        "cash_flows": [
            {
                "activity_id": "deposit-2",
                "activity_type": "CSD",
                "net_amount_usd": "7500.00",
                "occurred_at_ms": PRIOR_CLOSE_MS + 1,
            },
            {
                "activity_id": "withdrawal-2",
                "activity_type": "CSW",
                "net_amount_usd": "-1250.00",
                "occurred_at_ms": PRIOR_CLOSE_MS + 2,
            },
        ],
    },
]


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    current = Decimal(case["current_equity_usd"])
    prior = Decimal(case["prior_close_equity_usd"])
    net_cash_flow = sum(
        (Decimal(flow["net_amount_usd"]) for flow in case["cash_flows"]),
        start=Decimal("0"),
    )
    return {
        "case": case["case"],
        "known": True,
        "day_start_ms": PRIOR_CLOSE_MS,
        "day_end_ms": AS_OF_MS,
        "current_equity_usd": format(current, "f"),
        "prior_close_equity_usd": format(prior, "f"),
        "cash_flow_count": len(case["cash_flows"]),
        "net_cash_flow_usd": format(net_cash_flow, "f"),
        "total_usd": format(current - prior - net_cash_flow, "f"),
    }


def main() -> None:
    destination = (
        Path(__file__).parents[2]
        / "tests/fixtures/golden/broker-risk/PNL-001/v1"
    )
    destination.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "as_of_ms": AS_OF_MS,
        "prior_close_ms": PRIOR_CLOSE_MS,
        "cases": CASES,
    }
    output_payload = {"cases": [_expected(case) for case in CASES]}
    for filename, payload in (
        ("input.json", input_payload),
        ("output.json", output_payload),
    ):
        (destination / filename).write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

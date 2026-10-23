"""Write hand-calculated research backtest cases for the internal policy."""

from __future__ import annotations

import json
from pathlib import Path


_OUTPUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "PythonDataService/tests/fixtures/golden/research-primitives/RP-006/v1"
)


def main() -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    inputs = {
        "cases": [
            {
                "name": "entry_cost_before_recovery",
                "signal": [1.0, 1.0, 1.0],
                "forward_returns": [0.0, 0.005, 0.005],
                "cost_bps": 100.0,
                "bars_per_day": 1,
            },
            {
                "name": "reversal_and_flatten",
                "signal": [1.0, -1.0, -1.0, 0.0],
                "forward_returns": [0.0, 0.02, 0.01, -0.03],
                "cost_bps": 100.0,
                "bars_per_day": 1,
            },
        ]
    }
    # All output values are computed by hand from the policy's one-bar lag,
    # absolute position-change costs, and initial zero cumulative balance.
    outputs = {
        "cases": [
            {
                "name": "entry_cost_before_recovery",
                "positions": [1.0, 1.0, 1.0],
                "cumulative_returns": [-0.01, -0.005, 0.0],
                "net_total_return": 0.0,
                "gross_total_return": 0.01,
                "max_drawdown": 0.01,
                "annualized_turnover": 84.0,
                "total_trades": 1,
            },
            {
                "name": "reversal_and_flatten",
                "positions": [1.0, -1.0, -1.0, 0.0],
                "cumulative_returns": [-0.01, -0.01, -0.02, 0.0],
                "net_total_return": 0.0,
                "gross_total_return": 0.04,
                "max_drawdown": 0.02,
                "annualized_turnover": 252.0,
                "total_trades": 3,
            },
        ]
    }
    for name, payload in (("input.json", inputs), ("output.json", outputs)):
        (_OUTPUT_DIR / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

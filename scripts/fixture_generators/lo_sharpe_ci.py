"""Reproduce the published Lo (2002) Table 1 Sharpe SE fixture.

The table prints three decimal places. Its values below are transcribed
from the paper, not calculated by the canonical implementation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


_PUBLISHED_CASES = ((1.5, 60, 0.188), (3.0, 60, 0.303))
_OUTPUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "PythonDataService/tests/fixtures/golden/research-primitives/RP-005/v1"
)


def main() -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = []
    expected = []
    for sharpe, sample_size, published_se in _PUBLISHED_CASES:
        # Equal groups have sample standard deviation exactly one in real
        # arithmetic, so the input series has the requested sample Sharpe.
        offset = math.sqrt((sample_size - 1) / sample_size)
        cases.append(
            {
                "sharpe": sharpe,
                "sample_size": sample_size,
                "bars_per_year": 1,
                "returns": [sharpe - offset] * (sample_size // 2)
                + [sharpe + offset] * (sample_size // 2),
            }
        )
        expected.append({"published_standard_error": published_se})

    for name, payload in (("input.json", {"cases": cases}), ("output.json", {"cases": expected})):
        (_OUTPUT_DIR / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

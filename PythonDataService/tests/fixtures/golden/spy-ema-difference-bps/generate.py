"""Materialize the independently derived DifferenceBps oracle rows."""

from __future__ import annotations

import json
from pathlib import Path

REFERENCE_ROWS = [
    {"left": "500.20", "right": "500.00", "difference_bps": "4.0000"},
    {"left": "499.80", "right": "500.00", "difference_bps": "-4.0000"},
    {"left": "125.056789", "right": "125.000000", "difference_bps": "4.5431200"},
    {"left": "100.00", "right": "100.00", "difference_bps": "0"},
    {"left": "1.00", "right": "0.00", "error": "division_by_zero"},
]


def main() -> None:
    fixture_dir = Path(__file__).resolve().parent
    inputs = [{"left": row["left"], "right": row["right"]} for row in REFERENCE_ROWS]
    (fixture_dir / "input.json").write_text(
        json.dumps(inputs, indent=2) + "\n",
        encoding="utf-8",
    )
    (fixture_dir / "output.json").write_text(
        json.dumps(REFERENCE_ROWS, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

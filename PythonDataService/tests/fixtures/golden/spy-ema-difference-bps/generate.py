"""Generate the independent Decimal reference output for DifferenceBps."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path


def main() -> None:
    fixture_dir = Path(__file__).resolve().parent
    rows = json.loads((fixture_dir / "input.json").read_text(encoding="utf-8"))
    output = [
        {
            "left": row["left"],
            "right": row["right"],
            "difference_bps": str(
                (Decimal(row["left"]) - Decimal(row["right"]))
                / Decimal(row["right"])
                * Decimal(10_000)
            ),
        }
        for row in rows
    ]
    (fixture_dir / "output.json").write_text(
        json.dumps(output, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

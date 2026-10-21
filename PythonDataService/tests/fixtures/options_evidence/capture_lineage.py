"""Capture bounded retained procedure receipts, never replay or expose final (#3400)."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.research import options_research_lineage as lineage
from app.research.options_golden_search import repository
from app.research.persistence.db import with_connection


async def capture(study_id: str) -> dict:
    row = await with_connection(repository.get, study_id)
    windows = lineage.admitted_windows(row)
    retained = await with_connection(lineage.receipts, study_id, windows)
    trials = await with_connection(lineage.trial_rows, study_id, windows, 0, 50)
    return {"study_id": study_id, **retained, "trials": trials}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--study-id", default="os_d7b4752086147e437a3a0a56ecc980f33d3c6055")
    args = parser.parse_args()
    with args.destination.open("x") as target:
        target.write(json.dumps(asyncio.run(capture(args.study_id)), separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()

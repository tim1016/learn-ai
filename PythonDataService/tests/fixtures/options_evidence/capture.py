"""Capture the retained read fixtures described in attribution.md; no replay commands."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.request import urlopen

from app.research.options_golden_search import repository
from app.research.persistence.db import with_connection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    routes = {"run76": "/api/research/options-backtest/runs/76", "run69": "/api/research/options-backtest/runs/69",
        "golden76": "/api/research/golden-option-setups/ogs_dbe79f85399048f4a4decc3c86e1f3ff"}
    for name, route in routes.items():
        with urlopen(args.base_url + route, timeout=30) as response:
            value = json.load(response)
        if name == "golden76":
            value["proof"] = {}
        (args.destination / f"{name}.json").write_text(json.dumps(value, separators=(",", ":")) + "\n")
    value = asyncio.run(with_connection(repository.get, "os_d7b4752086147e437a3a0a56ecc980f33d3c6055"))
    (args.destination / "study-record.json").write_text(json.dumps(value, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()

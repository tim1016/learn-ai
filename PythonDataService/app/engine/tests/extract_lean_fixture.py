"""One-shot helper to extract the LEAN SPY trade log into a CSV fixture.

Run once (or whenever the LEAN reference output changes):

    python -m app.engine.tests.extract_lean_fixture \
        --lean-log /sessions/ecstatic-hopeful-volta/mnt/Lean/Launcher/bin/Debug/SpyEmaCrossoverAlgorithm-log.txt \
        --output app/engine/tests/fixtures/spy_lean_trades.csv
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

TRADE_ROW_RE = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "  # log timestamp prefix
    r"(?P<entry>\d{4}-\d{2}-\d{2} \d{2}:\d{2}),"
    r"(?P<entry_price>-?\d+\.\d+),"
    r"(?P<exit>\d{4}-\d{2}-\d{2} \d{2}:\d{2}),"
    r"(?P<exit_price>-?\d+\.\d+),"
    r"(?P<ema5>-?\d+\.\d+),"
    r"(?P<ema10>-?\d+\.\d+),"
    r"(?P<rsi>-?\d+\.\d+),"
    r"(?P<pnl_pts>-?\d+\.\d+),"
    r"(?P<pnl_pct>-?\d+\.\d+),"
    r"(?P<result>WIN|LOSS)"
)


def extract(lean_log: Path, output: Path) -> int:
    rows: list[dict[str, str]] = []
    with lean_log.open("r", encoding="utf-8") as f:
        for line in f:
            m = TRADE_ROW_RE.search(line)
            if m:
                rows.append(m.groupdict())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "trade_no",
                "entry",
                "entry_price",
                "exit",
                "exit_price",
                "ema5",
                "ema10",
                "rsi",
                "pnl_pts",
                "pnl_pct",
                "result",
            ],
        )
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            writer.writerow({"trade_no": i, **row})
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lean-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = extract(args.lean_log, args.output)
    print(f"Wrote {count} trades to {args.output}")


if __name__ == "__main__":
    main()

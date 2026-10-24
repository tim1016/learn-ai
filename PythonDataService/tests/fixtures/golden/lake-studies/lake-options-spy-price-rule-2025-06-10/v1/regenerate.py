"""Recover the already-approved daily payloads from #3524's saved comment, with no vendor request.

The expected values are hand-worked from the recorded minute bars and the daily payloads in that comment.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SOURCE = "https://github.com/tim1016/learn-ai/issues/3524#issuecomment-6387331949"
MINUTE_FIXTURE = "options-backtest-spy-2025-06-10"
EXPECTED = {
    "call": {
        "regular_close": "5.57", "regular_volume": 31657, "daily_vwap": "4.4091",
        "daily_close": "5.14", "daily_volume": 31899, "close_minus_daily_vwap": "1.1609",
        "after_close_bars": 12, "after_close_volume": 242,
    },
    "put": {
        "regular_close": "2.08", "regular_volume": 28954, "daily_vwap": "2.7747",
        "daily_close": "2.39", "daily_volume": 30078, "close_minus_daily_vwap": "-0.6947",
        "after_close_bars": 14, "after_close_volume": 1124,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comment_markdown", type=Path)
    parser.add_argument("output", type=Path, help="A new or empty directory")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be empty; never overwrite a golden fixture")
    text = args.comment_markdown.read_text(encoding="utf-8")
    bodies = {}
    records = []
    for kind, right in (("call", "C"), ("put", "P")):
        filename = f"O_SPY250613{right}00600000_day_2025-06-10.json"
        match = re.search(rf"`{re.escape(filename)}`:\s*```json\n([^`]+)\n```", text)
        if match is None:
            raise ValueError(f"approved comment has no body for {filename}")
        body = match.group(1)
        parsed = json.loads(body)
        if parsed["ticker"] != f"O:SPY250613{right}00600000" or parsed["adjusted"] is not False:
            raise ValueError(f"unexpected contract or adjustment mode for {filename}")
        bodies[filename] = body + "\n"
        records.append({"kind": kind, "file": filename, "minute_fixture": MINUTE_FIXTURE})
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, body in bodies.items():
        (args.output / filename).write_text(body, encoding="utf-8")
    for filename, value in {
        "recording.json": {"source": SOURCE, "vendor_fetched_on": "2026-10-24", "records": records},
        "expected.json": EXPECTED,
    }.items():
        (args.output / filename).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

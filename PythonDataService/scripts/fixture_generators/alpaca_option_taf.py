"""Generate the FEE-003 golden fixture: the OPT TAF rows Alpaca Paper posted (#3306).

The oracle is the broker. Nothing is computed: each day is a posted row and the contracts its text says it was
charged on. Three days are read from the committed recordings
(``tests/fixtures/alpaca/option_paper_recordings``); the fourth, 2026-10-16, is the run record of 2026-10-21 in
``docs/runbooks/qualify-option-orders-on-paper.md``, because its rows were read offline and no recording of them
is committed. The script never imports the option fee model the fixture checks. It writes ``input.json`` and
``output.json`` and records their hashes, and the hand-written ``attribution.md``'s, in the manifest. Run from
``PythonDataService/``:

    .venv/bin/python -m scripts.fixture_generators.alpaca_option_taf
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.lean_sidecar.trading_calendar import session_open_ms_utc
from tests.fixtures.golden_support.hashing import compute_hashes

logger = logging.getLogger(__name__)

FIXTURE_ID = "FEE-003"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "broker-fees" / FIXTURE_ID / "v1"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"
RECORDINGS = ROOT / "tests" / "fixtures" / "alpaca" / "option_paper_recordings"

_ROW_TEXT = re.compile(r"OPT TAF fee for proceed of (\d+) contracts \((\d+) trades\) on (\d{4}-\d{2}-\d{2}) by \S+")
_RECORDINGS_SOURCE = "tests/fixtures/alpaca/option_paper_recordings"
_RUNBOOK_SOURCE = "docs/runbooks/qualify-option-orders-on-paper.md, run record 2026-10-21"

# A day whose posted row is in no committed recording: the runbook's record of 2026-10-16 states "$0.09 (one row)"
# for the day's sold contracts. Its sells are in this capture, whose feed ends that day.
_RUNBOOK_DAYS: dict[date, tuple[str, Decimal]] = {
    date(2026, 10, 16): ("3225-C4-buyback-retry-window-missed-20261016", Decimal("0.09"))
}
_NEW_YORK = ZoneInfo("America/New_York")


def _recorded_days() -> dict[date, tuple[int, int, Decimal]]:
    """Each trade date's posted OPT TAF row: sold contracts, trades and posted dollars, as every capture holds it."""
    seen: dict[date, set[tuple[int, int, Decimal]]] = {}
    for path in sorted(RECORDINGS.glob("*/activities.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            body = json.loads(json.loads(line)["raw_body"])
            if not isinstance(body, list):
                continue
            for row in body:
                if row["activity_type"] != "FEE" or row.get("activity_sub_type") != "TAF":
                    continue
                stated = _ROW_TEXT.fullmatch(row["description"])
                assert stated is not None, row["description"]
                day = date.fromisoformat(row["date"])
                assert stated[3] == day.isoformat(), row["description"]
                seen.setdefault(day, set()).add((int(stated[1]), int(stated[2]), -Decimal(row["net_amount"])))
    assert all(len(rows) == 1 for rows in seen.values()), f"a day's captures disagree: {seen}"
    return {day: next(iter(rows)) for day, rows in sorted(seen.items())}


def _sells_in_capture(step: str, day: date) -> int:
    """The executions of ``day`` that sold (a sale or a short sale), one contract each, in the capture's own fills."""
    sold: set[str] = set()
    for line in (RECORDINGS / step / "activities.jsonl").read_text(encoding="utf-8").splitlines():
        body = json.loads(json.loads(line)["raw_body"])
        if not isinstance(body, list):
            continue
        for row in body:
            if row["activity_type"] != "FILL" or row["side"] not in ("sell", "sell_short"):
                continue
            if datetime.fromisoformat(row["transaction_time"]).astimezone(_NEW_YORK).date() != day:
                continue
            assert Decimal(row["qty"]) == 1, f"{step}: only one-contract executions are expected"
            sold.add(row["id"])
    return len(sold)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    recorded = _recorded_days()
    assert not recorded.keys() & _RUNBOOK_DAYS.keys(), "a day is in the recordings: read it from there"
    days = sorted(
        [(day, row, _RECORDINGS_SOURCE) for day, row in recorded.items()]
        + [
            (day, (_sells_in_capture(step, day),) * 2 + (posted,), _RUNBOOK_SOURCE)
            for day, (step, posted) in _RUNBOOK_DAYS.items()
        ]
    )

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(
        FIXTURE_DIR / "input.json",
        {
            "days": [
                {
                    "trade_date_ms": session_open_ms_utc(day),
                    "sold_contracts": sold,
                    "trades": trades,
                    "source": source,
                }
                for day, (sold, trades, _), source in days
            ]
        },
    )
    _write_json(
        FIXTURE_DIR / "output.json",
        {"posted_taf": [{"trade_date_ms": session_open_ms_utc(day), "taf": str(posted)} for day, (*_, posted), _ in days]},
    )

    names = ["input.json", "output.json", "attribution.md"]
    content_hashes, file_hashes = compute_hashes(FIXTURE_DIR, names)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entry = {
        "id": FIXTURE_ID,
        "name": "Alpaca Paper posted option TAF rows",
        "category": "broker-fees",
        "canonical_module": "PythonDataService/app/broker/alpaca/option_fees.py",
        "canonical_callable": "settle_option_session",
        "reference": {
            "kind": "vendor_observed",
            "oracle": "the OPT TAF FEE activities Alpaca Paper posted for 2026-10-13 to 2026-10-16, and the contracts each row's text says it was charged on",
            "citation": "Alpaca Paper account activity feed, non-trade FEE activities of sub type TAF: 2026-10-13, -14 and -15 from tests/fixtures/alpaca/option_paper_recordings; 2026-10-16 from the run record of 2026-10-21 in docs/runbooks/qualify-option-orders-on-paper.md. See the fixture attribution.",
        },
        "market_input": {
            "source": "vendored",
            "vendor": "Alpaca Paper",
            "generated_by": "PythonDataService/scripts/fixture_generators/alpaca_option_taf.py",
        },
        "units": None,
        "tolerance": {
            "atol": 0.0,
            "rtol": 0.0,
            "note": "The posted amounts are exact cents and the model's day TAF is an exact Decimal rounded up to the cent; compared for Decimal equality.",
        },
        "active_version": 1,
        "versions": {
            "1": {
                "input": "input.json",
                "output": "output.json",
                "attribution": "attribution.md",
                "content_sha256": {name: content_hashes[name] for name in names[:2]},
                "file_sha256": file_hashes,
            }
        },
        "status": "active",
        "validated_by": ["tests/fixtures/test_alpaca_option_taf_fixture.py"],
    }
    fixtures = [item for item in manifest["fixtures"] if item["id"] != FIXTURE_ID]
    fixtures.insert(next(i for i, item in enumerate(fixtures) if item["id"] == "FEE-002") + 1, entry)
    manifest["fixtures"] = fixtures
    _write_json(MANIFEST_PATH, manifest)
    for name in names:
        logger.info("%s file_sha256=%s", name, file_hashes[name])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()

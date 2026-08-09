"""Generate the ENG-006 realized-equity staircase golden fixture.

The oracle is hand-computed decimal arithmetic over a deliberately tiny
closed-trade ledger. It never imports the canonical implementation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURE_ID = "ENG-006"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "engine-results" / FIXTURE_ID / "v1"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hashes(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    if path.suffix == ".json":
        normalized = json.dumps(
            json.loads(raw.decode("utf-8")),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest(), file_hash
    return file_hash, file_hash


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    input_path = FIXTURE_DIR / "input.json"
    output_path = FIXTURE_DIR / "output.json"
    attribution_path = FIXTURE_DIR / "attribution.md"

    _write_json(
        input_path,
        {
            "cases": [
                {
                    "name": "equal_exit_order",
                    "initial_cash": "100000.00",
                    "start_ms_utc": 1700000000000,
                    "end_ms_utc": 1700000005000,
                    "trades": [
                        {"trade_number": 2, "exit_ms_utc": 1700000002000, "pnl": "-75.50"},
                        {"trade_number": 1, "exit_ms_utc": 1700000002000, "pnl": "250.25"},
                        {"trade_number": 3, "exit_ms_utc": 1700000004000, "pnl": "0.00"},
                    ],
                },
                {
                    "name": "boundary_folding",
                    "initial_cash": "100.00",
                    "start_ms_utc": 1700000010000,
                    "end_ms_utc": 1700000013000,
                    "trades": [
                        {"trade_number": 1, "exit_ms_utc": 1700000010000, "pnl": "1.25"},
                        {"trade_number": 2, "exit_ms_utc": 1700000012000, "pnl": "-0.25"},
                        {"trade_number": 3, "exit_ms_utc": 1700000013000, "pnl": "1.00"},
                        {"trade_number": 4, "exit_ms_utc": 1700000013000, "pnl": "-0.50"},
                    ],
                },
            ]
        },
    )
    _write_json(
        output_path,
        {
            "cases": [
                {
                    "name": "equal_exit_order",
                    "points": [
                        {"t": 1700000000000, "e": 100000.0},
                        {"t": 1700000002000, "e": 100174.75},
                        {"t": 1700000004000, "e": 100174.75},
                        {"t": 1700000005000, "e": 100174.75},
                    ],
                },
                {
                    "name": "boundary_folding",
                    "points": [
                        {"t": 1700000010000, "e": 101.25},
                        {"t": 1700000012000, "e": 101.0},
                        {"t": 1700000013000, "e": 101.5},
                    ],
                },
            ]
        },
    )
    attribution_path.write_text(
        "# ENG-006 — realized-equity staircase\n\n"
        "## Source\n\n"
        "The inputs are synthetic closed-trade ledger records. The methodology is the "
        "persisted `BacktestTrade.PnL` accounting contract: start with initial cash and "
        "add each net closed-trade P&L at the trade's `exit_ms_utc`.\n\n"
        "## Independent numerical oracle\n\n"
        "`reference_kind=hand_computed`. Expected points were calculated with exact decimal "
        "arithmetic without importing `build_realized_equity_envelope`. Equal timestamps are "
        "summed into one post-exit point. An exit at the start or end boundary owns that "
        "timestamp, so there is no duplicate anchor.\n\n"
        "## Cases\n\n"
        "- `equal_exit_order`: `$100000.00 + $250.25 - $75.50 = $100174.75`; the zero-P&L "
        "exit and terminal anchor retain that value.\n"
        "- `boundary_folding`: start `$100.00 + $1.25 = $101.25`; middle `$101.25 - $0.25 "
        "= $101.00`; end `$101.00 + $1.00 - $0.50 = $101.50`.\n\n"
        "## Tolerance\n\n"
        "`atol=1e-6, rtol=0`: persisted contract values cross a float transport boundary, "
        "while the oracle calculations themselves are exact decimal arithmetic.\n\n"
        "## Regeneration\n\n"
        "`python PythonDataService/scripts/fixture_generators/realized_equity_staircase.py`\n",
        encoding="utf-8",
    )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["fixtures"] = [item for item in manifest["fixtures"] if item["id"] != FIXTURE_ID]
    content_hashes = {}
    file_hashes = {}
    for path in (input_path, output_path):
        content_hash, file_hash = _hashes(path)
        content_hashes[path.name] = content_hash
        file_hashes[path.name] = file_hash
    file_hashes[attribution_path.name] = hashlib.sha256(attribution_path.read_bytes()).hexdigest()
    manifest["fixtures"].append(
        {
            "id": FIXTURE_ID,
            "name": "Persisted realized-equity staircase",
            "category": "engine-results",
            "canonical_module": "PythonDataService/app/engine/results/equity_downsample.py",
            "canonical_callable": "build_realized_equity_envelope",
            "reference": {
                "kind": "hand_computed",
                "oracle": "exact decimal accumulation of the synthetic closed-trade ledger without importing canonical code",
                "citation": "Persisted BacktestTrade.PnL accounting contract; see docs/references/realized-equity-staircase-v1.md.",
            },
            "market_input": {
                "source": "synthetic",
                "vendor": None,
                "generated_by": "PythonDataService/scripts/fixture_generators/realized_equity_staircase.py",
            },
            "units": None,
            "tolerance": {
                "atol": 1e-6,
                "rtol": 0.0,
                "note": "Exact Decimal oracle transported through float display points; fixed P&L tolerance required by the realized-equity contract.",
            },
            "active_version": 1,
            "versions": {
                "1": {
                    "input": "input.json",
                    "output": "output.json",
                    "attribution": "attribution.md",
                    "content_sha256": content_hashes,
                    "file_sha256": file_hashes,
                }
            },
            "status": "active",
        }
    )
    _write_json(MANIFEST_PATH, manifest)


if __name__ == "__main__":
    main()

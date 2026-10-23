"""Freeze a saved Paper activity capture and its independent decimal oracle (#3311).

Run from PythonDataService: python -m scripts.fixture_generators.outside_crypto_fees CAPTURE.json
The source contains the exhausted activity walk, positions, account cash/equity,
and capture time. This script makes no network call and imports no reconciliation
implementation. Broker numeric strings are preserved; account/order IDs are sanitized.

Formula: net BTC = signed FILL quantities + signed CFEE quantities.
Reference: https://docs.alpaca.markets/docs/crypto-fees and the saved broker response.
Canonical implementation: independent fixture oracle, not runtime math.
Validated against: tests/broker/alpaca/clerk/test_outside_reconciliation.py.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.broker.capture.sanitize import sanitize_payload
from tests.fixtures.golden_support.hashing import compute_hashes

ROOT = Path(__file__).parents[2] / "tests/fixtures/golden"
DESTINATION = ROOT / "broker-fees/CFEE-001/v1"


def generate(source: Path) -> None:
    data = json.loads(source.read_text())
    if not data["history_complete"]:
        raise ValueError("A complete broker activity walk is required")
    data.setdefault("captured_at_ms", source.stat().st_mtime_ns // 1_000_000)
    rows = data["activities"]
    fills = sum((Decimal(row["qty"]) * (1 if row["side"] == "buy" else -1)
                 for row in rows if row["activity_type"] == "FILL"), Decimal(0))
    fees = sum((Decimal(row["qty"]) for row in rows if row["activity_type"] == "CFEE" and "qty" in row), Decimal(0))
    output = {"fill_net_quantity": str(fills), "fee_quantity": str(fees), "net_quantity": str(fills + fees),
              "cash": data["account"]["cash"], "equity": data["account"]["equity"], "positions": data["positions"]}
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name, payload in (("input.json", sanitize_payload(data)), ("output.json", output)):
        (DESTINATION / name).write_text(json.dumps(payload, indent=2) + "\n")
    content, files = compute_hashes(DESTINATION, ["input.json", "output.json", "attribution.md"])
    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = {
        "id": "CFEE-001", "name": "Paper BTC outside fills reconciled by recorded quantity fees",
        "category": "broker-fees", "canonical_module": "PythonDataService/app/broker/alpaca/clerk/outside_reconciliation.py",
        "canonical_callable": "reconcile_outside_quantities",
        "reference": {"kind": "external_reference", "oracle": "Alpaca Paper activity and position API capture; exact decimal sum",
                      "citation": "https://docs.alpaca.markets/docs/crypto-fees; Paper capture 2026-10-22; issue #3311"},
        "market_input": {"source": "vendored", "vendor": "Alpaca Paper",
                         "generated_by": "PythonDataService/scripts/fixture_generators/outside_crypto_fees.py"},
        "units": None, "tolerance": {"atol": 0.0, "rtol": 0.0, "note": "Exact decimal quantity equality; never a dust tolerance."},
        "active_version": 1, "versions": {"1": {"input": "input.json", "output": "output.json", "attribution": "attribution.md",
                                                 "content_sha256": content, "file_sha256": files}},
        "status": "active", "validated_by": ["tests/broker/alpaca/clerk/test_outside_reconciliation.py"],
    }
    manifest["fixtures"] = [row for row in manifest["fixtures"] if row["id"] != entry["id"]] + [entry]
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    generate(parser.parse_args().capture)

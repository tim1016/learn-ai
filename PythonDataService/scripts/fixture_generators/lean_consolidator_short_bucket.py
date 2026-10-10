"""Register the LEAN short-bucket consolidation fixture's manifest entry.

The fixture itself is DERIVED, not generated: its input minutes and its
expected emitted bar are the captured stdout of running LEAN's own
``TradeBarConsolidator`` in the pinned sandbox image — see
``tests/fixtures/golden/lean-consolidator-short-bucket/v1/attribution.md``
and the harness beside this script. This script only writes the manifest
entry with the hashes of the committed files, so a future regeneration
(fresh LEAN run) is one ``python scripts/fixture_generators/lean_consolidator_short_bucket.py``
away from being pinned.

Run from the ``PythonDataService`` directory:

  PYTHONPATH=. python scripts/fixture_generators/lean_consolidator_short_bucket.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.fixtures.golden_support.hashing import compute_hashes

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "lean-consolidator-short-bucket" / "v1"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"
FIXTURE_ID = "lean-consolidator-short-bucket"

_FILES = ["input.json", "output.json", "attribution.md"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    content_hashes, file_hashes = compute_hashes(FIXTURE_DIR, ["input.json", "output.json"])
    file_hashes["attribution.md"] = _sha256(FIXTURE_DIR / "attribution.md")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["fixtures"] = [f for f in manifest["fixtures"] if f["id"] != FIXTURE_ID]
    manifest["fixtures"].append(
        {
            "id": FIXTURE_ID,
            "name": "LEAN consolidator: a bucket missing its last minute still ends at the period boundary",
            "category": "engine-parity",
            "canonical_module": "PythonDataService/app/engine/consolidators/trade_bar_consolidator.py",
            "canonical_callable": "TradeBarConsolidator",
            "reference": {
                "kind": "cross_engine",
                "oracle": (
                    "LEAN's own TradeBarConsolidator, executed in the pinned sandbox image "
                    "quantconnect/lean@sha256:4934c22c2b080a688f25b571746603e01533c5e581499d8457e5624a132ba77b; "
                    "cross-checked against the vendored commit 7986ed0a's TradeBar.cs "
                    "(EndTime => Time + Period) and TradeBarConsolidator.cs (AggregateBar never "
                    "rewrites Time/Period)."
                ),
                "citation": "references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/attribution.md",
            },
            "market_input": {
                "source": "synthetic",
                "vendor": None,
                "generated_by": "scripts/fixture_generators/lean_consolidator_short_bucket/harness.cs",
            },
            "units": None,
            "tolerance": {
                "atol": 0.0,
                "rtol": 0.0,
                "note": "Exact: Decimal prices and integer volume aggregated identically; instants compared as ms.",
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
            "validated_by": ["tests/engine/test_lean_consolidator_short_bucket.py"],
        }
    )
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Registered {FIXTURE_ID} in the golden manifest.")


if __name__ == "__main__":
    main()

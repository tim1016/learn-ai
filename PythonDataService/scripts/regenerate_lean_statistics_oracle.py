"""Regenerate the immutable LEAN statistics Oracle fixture.

The source workspace must be the retained pinned-runtime run identified by the
hard-coded artifact hashes below. Refusing every other input makes accidental
fixture rotation impossible; update these receipts only during an explicit
Oracle review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

EXPECTED = {
    "workspace/output/MyAlgorithm.json": "fd10d7f5793b324d9f70a3c4ecfb1b3e4bf08d431272d4f765861224cd7a69d4",
    "workspace/output/MyAlgorithm-summary.json": "8c0507842028cdf664f32540db7a11002fe649c4caafe3d7078b930d85cbaf6d",
    "workspace/data/alternative/interest-rate/usa/interest-rate.csv": (
        "1d0e6f2ab20e61a4330e8a38735bc73cde4034e7293a45d23b5ebdc6467f7899"
    ),
}
DESTINATION = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden" / "lean-statistics-oracle-v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regenerate(source_root: Path) -> None:
    receipts: dict[str, dict[str, int | str]] = {}
    for relative_path, expected_sha256 in EXPECTED.items():
        source = source_root / relative_path
        actual_sha256 = _sha256_file(source)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"Oracle source mismatch for {relative_path}: {actual_sha256} != {expected_sha256}"
            )
        destination = DESTINATION / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        receipts[relative_path] = {
            "sha256": actual_sha256,
            "size_bytes": source.stat().st_size,
        }

    manifest = {
        "fixture_id": "lean-statistics-oracle-v1",
        "oracle": "QuantConnect LEAN native backtest result",
        "lean_version": 17748,
        "lean_source_commit": "261366a7e26ae942df858ab20df4fef8fa07de67",
        "lean_image_digest": "sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771",
        "comparison_contract_id": "us-equity-raw-ibkr-v1",
        "strategy": "ema_crossover_signal",
        "symbol": "SPY",
        "start_ms_utc": 1_723_075_200_000,
        "end_ms_utc": 1_786_147_199_000,
        "artifacts": receipts,
        "regeneration": "scripts/regenerate_lean_statistics_oracle.py",
    }
    (DESTINATION / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    regenerate(args.source_root.resolve())


if __name__ == "__main__":
    main()

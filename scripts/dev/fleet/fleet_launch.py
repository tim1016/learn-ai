"""Skip-aware, ramped fleet launcher with sweep-to-converge.

Usage:
    python3 fleet_launch.py <manifest.json> [--results out.jsonl] [--cadence 5]
        [--max-sweeps 6]

Manifest: JSON list of {"sid": ..., "strategy_key": ..., "symbol": ...,
"override": bool}. Bots whose sid already appears on the broker roster are
skipped (a deployed bot has a durable binding regardless of run state), so
re-running the launcher after partial failures converges without
double-deploys. 409s during channel warm-up are expected; the sweep retries
them (2026-08-25 lore).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _api

logger = logging.getLogger("fleet_launch")

OVERRIDE_REASON = (
    "User-authorized 2026-08-26 fleet stress campaign: evidence-only program "
    "on paper account PA3KWXU1C4C3, safe_canary 1-share sizing."
)


def deployed_sids() -> set[str]:
    return {b["strategy_instance_id"] for b in _api.list_roster()}


def launch(manifest_path: str, results_path: str, cadence_s: float, max_sweeps: int) -> int:
    manifest = json.loads(Path(manifest_path).read_text())
    for sweep in range(1, max_sweeps + 1):
        existing = deployed_sids()
        pending = [m for m in manifest if m["sid"] not in existing]
        if not pending:
            logger.info("converged: all %d bots deployed", len(manifest))
            return 0
        logger.info("sweep %d: %d pending of %d", sweep, len(pending), len(manifest))
        for entry in pending:
            status, payload, latency = _api.deploy(
                entry["sid"],
                entry["strategy_key"],
                entry["symbol"],
                evidence_override_reason=OVERRIDE_REASON if entry.get("override") else None,
            )
            detail = payload.get("detail") if isinstance(payload, dict) else None
            _api.jsonl_append(
                results_path,
                {
                    "sweep": sweep,
                    "sid": entry["sid"],
                    "strategy_key": entry["strategy_key"],
                    "symbol": entry["symbol"],
                    "status": status,
                    "latency_s": round(latency, 2),
                    "detail": detail if status != 201 else None,
                },
            )
            if status == 201:
                logger.info("deployed %s (%.1fs)", entry["sid"], latency)
            else:
                logger.warning(
                    "deploy %s -> %d (%.1fs): %s",
                    entry["sid"], status, latency, str(detail)[:160],
                )
            time.sleep(cadence_s)
        time.sleep(min(30.0, 5.0 * sweep))
    remaining = [m["sid"] for m in manifest if m["sid"] not in deployed_sids()]
    logger.error("NOT converged after %d sweeps; remaining: %s", max_sweeps, remaining)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--results", default="fleet_launch_results.jsonl")
    parser.add_argument("--cadence", type=float, default=5.0)
    parser.add_argument("--max-sweeps", type=int, default=6)
    args = parser.parse_args()
    _api.setup_logging()
    return launch(args.manifest, args.results, args.cadence, args.max_sweeps)


if __name__ == "__main__":
    raise SystemExit(main())

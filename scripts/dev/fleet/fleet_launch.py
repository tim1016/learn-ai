"""Skip-aware, ramped fleet launcher with sweep-to-converge.

Usage:
    python3 fleet_launch.py <manifest.json> [--results out.jsonl] [--cadence 5]
        [--max-sweeps 6]

Manifest: JSON list of {"sid": ..., "strategy_key": ..., "symbol": ...,
"override": bool}. Bots already on the broker roster under the manifest's
own strategy key and symbol are skipped (a deployed bot has a durable
binding regardless of run state), so re-running the launcher after partial
failures converges without double-deploys. A roster row that reuses the sid
under a *different* identity is a manifest conflict, not a deployment: it
fails the run rather than being silently counted as converged. 409s during
channel warm-up are expected; the sweep retries them (2026-08-25 lore).
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


class ManifestConflictError(RuntimeError):
    """A roster row already holds this sid under a different identity."""


def override_reason() -> str:
    """Authorize the override against the account actually being deployed to.

    ``_api.ACCOUNT_ID`` is environment-overridable, so a hardcoded account in
    this sentence would write durable deployment evidence claiming
    authorization for an account the operator never selected.
    """
    return (
        "User-authorized 2026-08-26 fleet stress campaign: evidence-only program "
        f"on paper account {_api.ACCOUNT_ID}, safe_canary 1-share sizing."
    )


def deployed_identities() -> dict[str, tuple[str, str]]:
    """Map each rostered sid to its immutable (strategy_key, symbol)."""
    return {
        b["strategy_instance_id"]: (b.get("strategy_key"), b.get("symbol"))
        for b in _api.list_roster()
    }


def _pending_entries(manifest: list[dict], existing: dict[str, tuple[str, str]]) -> list[dict]:
    """Return the manifest entries still to deploy, refusing sid collisions.

    Membership alone is not proof of deployment: a sid registered under
    another strategy or symbol would be dropped from ``pending`` and the
    launcher would report convergence without ever submitting the deployment
    that exposes the mismatch.
    """
    pending: list[dict] = []
    for entry in manifest:
        rostered = existing.get(entry["sid"])
        if rostered is None:
            pending.append(entry)
            continue
        if rostered != (entry["strategy_key"], entry["symbol"]):
            raise ManifestConflictError(
                f"{entry['sid']} is already deployed as {rostered[0]}/{rostered[1]}, "
                f"but the manifest declares {entry['strategy_key']}/{entry['symbol']}"
            )
    return pending


def launch(manifest_path: str, results_path: str, cadence_s: float, max_sweeps: int) -> int:
    manifest = json.loads(Path(manifest_path).read_text())
    for sweep in range(1, max_sweeps + 1):
        existing = deployed_identities()
        pending = _pending_entries(manifest, existing)
        if not pending:
            logger.info("converged: all %d bots deployed", len(manifest))
            return 0
        logger.info("sweep %d: %d pending of %d", sweep, len(pending), len(manifest))
        for entry in pending:
            status, payload, latency = _api.deploy(
                entry["sid"],
                entry["strategy_key"],
                entry["symbol"],
                evidence_override_reason=override_reason() if entry.get("override") else None,
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
    remaining = [entry["sid"] for entry in _pending_entries(manifest, deployed_identities())]
    logger.error("NOT converged after %d sweeps; remaining: %s", max_sweeps, remaining)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--results", default="fleet_launch_results.jsonl")
    parser.add_argument("--cadence", type=float, default=5.0)
    parser.add_argument("--max-sweeps", type=int, default=6)
    args = parser.parse_args()
    if args.cadence < 0:
        parser.error("--cadence must not be negative")
    if args.max_sweeps < 1:
        parser.error("--max-sweeps must be at least 1")
    _api.setup_logging()
    try:
        return launch(args.manifest, args.results, args.cadence, args.max_sweeps)
    except ManifestConflictError as exc:
        logger.error("manifest conflicts with the live roster: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

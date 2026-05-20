"""One-shot CLI: backfill historical on-disk LEAN runs into Postgres.

For each ``<run_id>/`` subdirectory under ``--artifacts-root``, reads the
LEAN ``manifest.json`` for parameters + windowing, builds a persist payload
from ``normalized/result.json`` via the existing
``lean_sidecar_persistence`` pipeline, and POSTs it to the .NET endpoint
at ``/api/backtest-runs/persist-lean``.

Idempotent: the .NET endpoint dedupes on ``(Source='lean-sidecar', LeanRunId)``
so re-running the backfill is safe — workspaces already persisted are
returned unchanged with their existing ``StrategyExecution.Id``.

Usage:

    podman exec polygon-data-service python -m app.scripts.backfill_lean_runs \\
        --artifacts-root /app/artifacts/lean-sidecar \\
        --backend-url http://backend:8080

Skip rules (workspace skipped, not aborted):

  * Missing ``manifest.json`` or ``normalized/result.json``.
  * Manifest can't be parsed as JSON.
  * Manifest is missing required ``parameters`` fields (symbol, starting_cash).
  * Effective-algorithm window timestamps are missing.

Any other failure is logged with the workspace's run_id and the loop
continues with the next workspace. Persistence failures (HTTP/network)
likewise log and continue — the script returns the count of successful
persists so the caller can compare against the directory count.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.services.lean_sidecar_persistence import (
    build_persist_payload,
    persist_via_dotnet,
)

logger = logging.getLogger(__name__)


def _algorithm_name_from_manifest(manifest: dict[str, Any]) -> str:
    """Mirror ``lean_sidecar_persistence._algorithm_name_for_run`` for the
    backfill path, reconstructing the right name from manifest notes.

    The manifest's ``notes`` field carries flat ``key=value`` strings (set by
    the staging layer). Two we care about:

      * ``algorithm_source_kind=user_source`` → caller-supplied source →
        name is "user_provided".
      * ``trusted_template=<name>`` → trusted template path → name is the
        template name verbatim (e.g. "ema_crossover").

    Falls back to "user_provided" when neither marker is present (defensive).
    """
    notes = manifest.get("notes", [])
    template: str | None = None
    user_source = False
    for note in notes:
        if not isinstance(note, str):
            continue
        if note == "algorithm_source_kind=user_source":
            user_source = True
        elif note.startswith("trusted_template="):
            template = note.split("=", 1)[1].strip()

    if user_source:
        return "user_provided"
    return template or "user_provided"


def _build_payload_for_workspace(workspace: Path) -> dict[str, Any] | None:
    """Load manifest + build payload for a single workspace.

    Returns ``None`` when the workspace is incomplete (missing manifest,
    missing fields, or normalized result missing) — caller logs and skips.
    """
    manifest_path = workspace / "manifest.json"
    result_path = workspace / "normalized" / "result.json"

    if not manifest_path.exists():
        logger.info("Skipping %s: no manifest.json", workspace.name)
        return None
    if not result_path.exists():
        logger.info("Skipping %s: no normalized/result.json", workspace.name)
        return None

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        logger.warning("Skipping %s: manifest.json is not valid JSON: %s", workspace.name, exc)
        return None

    params = manifest.get("parameters") or {}
    symbol = params.get("symbol")
    starting_cash = params.get("starting_cash")
    if not symbol or starting_cash is None:
        logger.warning(
            "Skipping %s: manifest.parameters missing required fields (symbol=%r, starting_cash=%r)",
            workspace.name,
            symbol,
            starting_cash,
        )
        return None

    window = manifest.get("effective_algorithm_window_ms") or {}
    start_ms = window.get("start_ms")
    end_ms = window.get("end_ms")
    if start_ms is None or end_ms is None:
        logger.warning(
            "Skipping %s: manifest.effective_algorithm_window_ms missing (start_ms=%r, end_ms=%r)",
            workspace.name,
            start_ms,
            end_ms,
        )
        return None

    algorithm_name = _algorithm_name_from_manifest(manifest)

    return build_persist_payload(
        workspace_path=workspace,
        run_id=workspace.name,
        starting_cash=float(starting_cash),
        symbol=str(symbol),
        algorithm_name=algorithm_name,
        start_date_ms=int(start_ms),
        end_date_ms=int(end_ms),
        # PR B P1 fix — forward the manifest dict so the persist payload
        # carries the true ``brokerage_policy`` and ``data_policy`` for
        # backfilled rows. Without this, legacy LEAN runs would be
        # silently labeled with NULL (and pre-fix, ``algorithm_default``)
        # even when they actually ran under Interactive Brokers.
        manifest=manifest,
    )


async def backfill_directory(
    artifacts_root: Path,
    backend_url: str,
    *,
    timeout_seconds: float = 30.0,
) -> list[int]:
    """Backfill every workspace under ``artifacts_root``. Returns list of
    persisted ``StrategyExecution.Id`` integers (only successful persists).

    Async because ``persist_via_dotnet`` is async. The directory walk itself
    is sync; we serialize the POSTs to keep the load on the backend
    predictable (and because backfill is a one-shot maintenance task, not a
    hot path).
    """
    if not artifacts_root.exists():
        raise FileNotFoundError(f"artifacts_root does not exist: {artifacts_root}")

    persisted_ids: list[int] = []
    workspaces = sorted(p for p in artifacts_root.iterdir() if p.is_dir())
    logger.info("Found %d workspaces under %s", len(workspaces), artifacts_root)

    for workspace in workspaces:
        try:
            payload = _build_payload_for_workspace(workspace)
        except Exception as exc:
            logger.exception("Failed to build payload for %s: %s", workspace.name, exc)
            continue

        if payload is None:
            continue

        try:
            persisted_id = await persist_via_dotnet(payload, base_url=backend_url, timeout_seconds=timeout_seconds)
        except Exception as exc:
            logger.exception("persist_via_dotnet failed for %s: %s", workspace.name, exc)
            continue

        if persisted_id is None:
            logger.warning("persist_via_dotnet returned None for %s — skipping", workspace.name)
            continue

        logger.info("Backfilled %s → StrategyExecution.Id=%s", workspace.name, persisted_id)
        persisted_ids.append(persisted_id)

    logger.info(
        "Backfill complete: %d/%d workspaces persisted into %s",
        len(persisted_ids),
        len(workspaces),
        backend_url,
    )
    return persisted_ids


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Backfill historical on-disk LEAN sidecar runs into Postgres via the .NET persist endpoint.",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("/app/artifacts/lean-sidecar"),
        help="Root containing per-run workspace directories (default: /app/artifacts/lean-sidecar)",
    )
    parser.add_argument(
        "--backend-url",
        type=str,
        default=os.environ.get("BACKEND_URL", "http://backend:8080"),
        help="Base URL of the .NET backend (default: $BACKEND_URL or http://backend:8080)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="HTTP timeout per persist call (default: 30s)",
    )
    args = parser.parse_args()

    persisted_ids = asyncio.run(
        backfill_directory(
            artifacts_root=args.artifacts_root,
            backend_url=args.backend_url,
            timeout_seconds=args.timeout_seconds,
        )
    )
    logger.info("Done. %d run(s) persisted.", len(persisted_ids))


if __name__ == "__main__":
    main()

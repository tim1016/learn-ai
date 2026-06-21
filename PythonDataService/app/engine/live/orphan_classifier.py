"""PRD #619-B B6 — runtime candidate classification on daemon boot.

When the daemon comes up, it walks ``artifacts/live_runs/<run_id>/``
looking for ``engine_runtime.json`` files left behind by children
that were running before the daemon was restarted. The classifier
reports — but does NOT conclude — what the daemon is looking at:

- ``ORPHANED_CONTROL_PLANE`` — the sidecar is recent and its
  ``expected_daemon_boot_id`` is **not** this daemon's ``boot_id``.
  The child likely believes a previous daemon owns it and may still
  be trading; new starts for this instance must be blocked until
  process identity is verified.
- ``EXITED_UNMANAGED`` — the sidecar is stale (older than the
  freshness threshold) so the producer task stopped writing some
  time ago. The process may have exited cleanly or crashed; the
  daemon must still verify process death separately.
- ``FRESH_OWNED_BY_THIS_BOOT`` — the sidecar is recent **and** its
  ``expected_daemon_boot_id`` matches this daemon. Either the daemon
  restarted instantly without losing the child, or a phantom; the
  caller decides.
- ``NO_SIDECAR`` — directory exists but no readable runtime
  sidecar; the run never started or the sidecar was rotated out.

**A sidecar alone never proves a process is alive.** Process identity
verification is a separate step the daemon performs before reclassifying.
The classifier exists so the daemon has a structured list of
candidates to *investigate*, not a free pass to *kill*.

PRD authority principle #6: "No adoption, period — for now. Verified
adoption is its own future ADR." The classifier is the
investigation-list builder; adoption / reclamation is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.engine.live.engine_runtime import (
    ENGINE_RUNTIME_FILENAME,
    EngineRuntimeSnapshot,
    read_engine_runtime_snapshot,
)


# A sidecar is considered "stale" past this threshold. The publisher
# writes at 1Hz (steady-state cadence) plus immediate flushes on
# safety transitions; 30s gives the publisher ~30 missed ticks before
# the daemon raises a flag. Configurable per call.
DEFAULT_STALE_THRESHOLD_MS = 30_000


CandidateState = Literal[
    "ORPHANED_CONTROL_PLANE",
    "EXITED_UNMANAGED",
    "FRESH_OWNED_BY_THIS_BOOT",
    "NO_SIDECAR",
]


@dataclass(frozen=True)
class RuntimeCandidate:
    """One classified candidate the daemon may need to investigate.

    ``state`` is the classifier's verdict. ``reason`` documents the
    specific signal in operator-readable form; the daemon surfaces it
    on the cockpit's ``ORPHANED_CONTROL_PLANE`` line.

    ``sidecar`` is the parsed ``EngineRuntimeSnapshot`` (or ``None``
    when the sidecar was missing/malformed) so callers can read pid,
    process_start_identity, broker block, etc. without re-reading.
    """

    run_id: str
    run_dir: Path
    state: CandidateState
    sidecar: EngineRuntimeSnapshot | None
    sidecar_age_ms: int | None
    reason: str


def classify_runtime_candidates_on_boot(
    live_runs_root: Path,
    *,
    this_boot_id: str,
    now_ms: int,
    stale_threshold_ms: int = DEFAULT_STALE_THRESHOLD_MS,
) -> list[RuntimeCandidate]:
    """Walk ``live_runs/`` and classify every candidate.

    ``live_runs_root`` is the root the host daemon manages
    (``<artifacts>/live_runs``). Each immediate child directory is a
    ``run_id``; the classifier consults that run's
    ``engine_runtime.json`` to decide.

    The classifier is a **read-only** operation — no files are
    modified, no processes are signalled. The daemon orchestrates
    follow-up (process identity verification, blocking new starts,
    flagging the cockpit) based on the returned list.

    Returns an empty list if ``live_runs_root`` does not exist.
    """
    if not live_runs_root.exists():
        return []

    out: list[RuntimeCandidate] = []
    for run_dir in sorted(p for p in live_runs_root.iterdir() if p.is_dir()):
        run_id = run_dir.name
        sidecar_path = run_dir / ENGINE_RUNTIME_FILENAME
        sidecar = read_engine_runtime_snapshot(sidecar_path)

        if sidecar is None:
            out.append(
                RuntimeCandidate(
                    run_id=run_id,
                    run_dir=run_dir,
                    state="NO_SIDECAR",
                    sidecar=None,
                    sidecar_age_ms=None,
                    reason=f"no readable engine_runtime.json at {sidecar_path}",
                )
            )
            continue

        age_ms = now_ms - sidecar.written_at_ms
        is_stale = age_ms > stale_threshold_ms

        if is_stale:
            out.append(
                RuntimeCandidate(
                    run_id=run_id,
                    run_dir=run_dir,
                    state="EXITED_UNMANAGED",
                    sidecar=sidecar,
                    sidecar_age_ms=age_ms,
                    reason=(
                        f"sidecar age {age_ms}ms > stale threshold "
                        f"{stale_threshold_ms}ms; producer task stopped writing"
                    ),
                )
            )
            continue

        # Sidecar is fresh; check ownership.
        if sidecar.expected_daemon_boot_id != this_boot_id:
            out.append(
                RuntimeCandidate(
                    run_id=run_id,
                    run_dir=run_dir,
                    state="ORPHANED_CONTROL_PLANE",
                    sidecar=sidecar,
                    sidecar_age_ms=age_ms,
                    reason=(
                        f"fresh sidecar owned by daemon boot_id "
                        f"{sidecar.expected_daemon_boot_id!r}, not this boot "
                        f"({this_boot_id!r}); process may still be running"
                    ),
                )
            )
            continue

        out.append(
            RuntimeCandidate(
                run_id=run_id,
                run_dir=run_dir,
                state="FRESH_OWNED_BY_THIS_BOOT",
                sidecar=sidecar,
                sidecar_age_ms=age_ms,
                reason="sidecar fresh and owned by this daemon boot_id",
            )
        )

    return out

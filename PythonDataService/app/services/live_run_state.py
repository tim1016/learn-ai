"""Infer RunState from run-directory file metadata.

The ``infer_state()`` function is a pure function over the filesystem: it reads
flags, a sidecar JSON, and live.log, then returns a ``RunState`` enum value.

**idle is NOT returned here.**  The spec defines ``idle`` at the list level
("no run_status.json for any run within the last 24 h, or all recent runs
have ended_at_ms set").  ``infer_state()`` operates on a single run_dir; when
nothing is conclusive for that directory it returns ``unknown``.  The list
endpoint (``/api/live-runs``) is responsible for mapping "all runs are done or
absent" → ``idle``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pyarrow.parquet as pq

from app.schemas.live_runs import ExitReason, RunState, RunStatusSidecar

# ---------------------------------------------------------------------------
# Thresholds (seconds) — module constants so tests can monkeypatch if needed
# ---------------------------------------------------------------------------
WAITING_FOR_BARS_WINDOW_S: int = 60
STALE_THRESHOLD_S: int = 90
STALE_MAX_S: int = 300
IDLE_THRESHOLD_S: int = 86_400


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mtime_ms(path: Path) -> int | None:
    """Return mtime as ms UTC, or None if path does not exist."""
    try:
        return int(path.stat().st_mtime * 1000)
    except FileNotFoundError:
        return None


def _parquet_row_count(path: Path) -> int:
    """Return row count from Parquet footer metadata. O(1) — no data read."""
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return 0


def _read_sidecar(run_dir: Path) -> RunStatusSidecar | None:
    """Read and parse run_status.json. Returns None if missing or corrupt."""
    path = run_dir / "run_status.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunStatusSidecar.model_validate(data)
    except Exception:
        return None


def _log_has_bar(run_dir: Path) -> bool:
    """Return True if live.log contains at least one ``[BAR]`` line."""
    log_path = run_dir / "live.log"
    if not log_path.exists():
        return False
    try:
        return "[BAR]" in log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _log_contains(run_dir: Path, needle: str) -> bool:
    """Return True if live.log contains the given substring."""
    log_path = run_dir / "live.log"
    if not log_path.exists():
        return False
    try:
        return needle in log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_state(run_dir: Path, now_ms: int | None = None) -> RunState:
    """Infer ``RunState`` from run-directory file metadata and sidecar.

    Priority order (first match wins):

    1. ``poisoned``          — ``poisoned.flag`` exists
    2. ``halted``            — ``halt.flag`` exists
    3. ``complete``          — sidecar present, ended_at_ms set, exit_reason in
                               {normal, force_flat_complete}
    4. ``stopped``           — sidecar present, ended_at_ms set, other exit_reason
    5. ``waiting_for_bars``  — sidecar active, no [BAR] line yet, started within 60 s
    6. ``warming_up``        — sidecar active, [BAR] lines exist, live.log mtime ≤ 90 s,
                               decisions.parquet has 0 rows
    7. ``running``           — sidecar active, [BAR] lines exist, live.log mtime ≤ 90 s,
                               decisions.parquet has ≥ 1 row
    8. ``stale``             — sidecar active but live.log silent for 90 s – 5 min
                               (also covers no-bars + started > 60 s ago)
    9. ``complete`` (legacy) — no sidecar, live.log has "[START] run completed cleanly"
    10. ``unknown``          — defensive fallback

    Args:
        run_dir: Path to the run directory (must exist).
        now_ms:  Current time as int64 ms UTC. Defaults to ``time.time() * 1000``.

    Returns:
        A ``RunState`` enum value.
    """
    if now_ms is None:
        now_ms = _now_ms()
    now_s = now_ms / 1000.0

    # ------------------------------------------------------------------
    # 1–2: Flag files (highest priority)
    # ------------------------------------------------------------------
    if (run_dir / "poisoned.flag").exists():
        return RunState.poisoned
    if (run_dir / "halt.flag").exists():
        return RunState.halted

    # ------------------------------------------------------------------
    # 3–8: Sidecar-primary path
    # ------------------------------------------------------------------
    sidecar = _read_sidecar(run_dir)

    if sidecar is not None:
        # 3–4: Terminal states — run has ended
        if sidecar.ended_at_ms is not None:
            if sidecar.exit_reason in {ExitReason.normal, ExitReason.force_flat_complete}:
                return RunState.complete
            # keyboard_interrupt, signal, exception, fatal_halt, etc.
            return RunState.stopped

        # 5–8: Active run (ended_at_ms not set)
        started_s = sidecar.started_at_ms / 1000.0
        log_mtime = _mtime_ms(run_dir / "live.log")
        log_age_s = (now_s - log_mtime / 1000.0) if log_mtime is not None else float("inf")

        if not _log_has_bar(run_dir):
            # 5: No [BAR] lines yet
            if (now_s - started_s) <= WAITING_FOR_BARS_WINDOW_S:
                return RunState.waiting_for_bars
            # Started more than 60 s ago with no bars — treat as stale
            return RunState.stale

        # [BAR] lines exist; use live.log mtime as proxy for last bar activity
        if log_age_s <= STALE_THRESHOLD_S:
            decisions_path = run_dir / "decisions.parquet"
            row_count = _parquet_row_count(decisions_path) if decisions_path.exists() else 0
            # 6 or 7: warming_up vs running
            return RunState.running if row_count >= 1 else RunState.warming_up

        if log_age_s <= STALE_MAX_S:
            # 8: Log has gone quiet for 90 s – 5 min
            return RunState.stale

        # log silent > 5 min with active sidecar — still stale (edge case)
        return RunState.stale

    # ------------------------------------------------------------------
    # 9–10: Legacy path — no sidecar
    # ------------------------------------------------------------------
    if _log_contains(run_dir, "[START] run completed cleanly"):
        return RunState.complete

    return RunState.unknown

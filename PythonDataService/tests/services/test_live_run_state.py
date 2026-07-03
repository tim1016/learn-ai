"""Unit tests for app.services.live_run_state.infer_state.

Each test corresponds to one RunState branch in the priority-ordered match:
  1. poisoned     — poisoned.flag present
  2. halted       — halt.flag present (no poisoned)
  3. complete     — sidecar + ended_at_ms + exit_reason in {normal, force_flat_complete}
  4. stopped      — sidecar + ended_at_ms + other exit_reason
  5. waiting_for_bars — sidecar active, no [BAR] yet, started within 60 s
  6. warming_up   — sidecar active, [BAR] in log, log recent, 0 decisions
  7. running      — same but decisions_rows >= 1
  8. stale        — sidecar active but log silent > 90 s (also no bars + > 60 s)
  9. complete (legacy) — no sidecar, log contains "[START] run completed cleanly"
  10. unknown     — fallback
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.schemas.live_runs import ExitReason, RunState, RunStatusSidecar
from app.services.live_run_state import infer_state

# ---------------------------------------------------------------------------
# Helper: build a run directory from named options
# ---------------------------------------------------------------------------


def _make_sidecar(
    *,
    started_offset_s: float = 0,
    ended: bool = False,
    exit_reason: ExitReason | None = None,
) -> RunStatusSidecar:
    """Build a RunStatusSidecar relative to current time."""
    now = int(time.time() * 1000)
    started = now - int(started_offset_s * 1000)
    ended_at = now if ended else None
    return RunStatusSidecar(
        run_id="test-run",
        started_at_ms=started,
        last_update_ms=now,
        ended_at_ms=ended_at,
        exit_code=0 if ended else None,
        exit_reason=exit_reason if ended else None,
        host_pid=1234,
    )


def make_run_dir(
    tmp_path: Path,
    *,
    sidecar: RunStatusSidecar | None = None,
    halt: bool = False,
    poisoned: bool = False,
    log_content: str = "",
    decisions_rows: int = 0,
    log_mtime_offset: int = 0,
) -> Path:
    """Create a run directory with the specified file structure.

    Args:
        tmp_path: Pytest-provided temp base.
        sidecar: If set, written to run_status.json.
        halt: If True, creates halt.flag.
        poisoned: If True, creates poisoned.flag.
        log_content: Written to live.log if non-empty.
        decisions_rows: Number of rows for decisions.parquet (0 = no file).
        log_mtime_offset: Seconds to subtract from mtime of live.log
                          (positive = file appears older).
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    if sidecar is not None:
        (run_dir / "run_status.json").write_text(json.dumps(sidecar.model_dump()), encoding="utf-8")

    if halt:
        (run_dir / "halt.flag").write_text('{"reason": "test"}', encoding="utf-8")

    if poisoned:
        (run_dir / "poisoned.flag").write_text('{"trigger": "OUTSIDE_MUTATION"}', encoding="utf-8")

    if log_content:
        log_path = run_dir / "live.log"
        log_path.write_text(log_content, encoding="utf-8")
        if log_mtime_offset != 0:
            target_mtime = time.time() - log_mtime_offset
            os.utime(log_path, (target_mtime, target_mtime))

    if decisions_rows > 0:
        t = pa.table({"signal": ["ENTER"] * decisions_rows})
        pq.write_table(t, run_dir / "decisions.parquet")

    return run_dir


# ---------------------------------------------------------------------------
# Tests: one per branch
# ---------------------------------------------------------------------------


def test_infer_state_poisoned(tmp_path: Path):
    run_dir = make_run_dir(tmp_path, poisoned=True)
    assert infer_state(run_dir) == RunState.poisoned


def test_infer_state_poisoned_takes_priority_over_halted(tmp_path: Path):
    """poisoned beats halted (priority 1 > 2)."""
    run_dir = make_run_dir(tmp_path, poisoned=True, halt=True)
    assert infer_state(run_dir) == RunState.poisoned


def test_infer_state_halted(tmp_path: Path):
    run_dir = make_run_dir(tmp_path, halt=True)
    assert infer_state(run_dir) == RunState.halted


def test_infer_state_complete_normal(tmp_path: Path):
    sidecar = _make_sidecar(ended=True, exit_reason=ExitReason.normal)
    run_dir = make_run_dir(tmp_path, sidecar=sidecar)
    assert infer_state(run_dir) == RunState.complete


def test_infer_state_complete_force_flat(tmp_path: Path):
    sidecar = _make_sidecar(ended=True, exit_reason=ExitReason.force_flat_complete)
    run_dir = make_run_dir(tmp_path, sidecar=sidecar)
    assert infer_state(run_dir) == RunState.complete


def test_infer_state_stopped_keyboard_interrupt(tmp_path: Path):
    sidecar = _make_sidecar(ended=True, exit_reason=ExitReason.keyboard_interrupt)
    run_dir = make_run_dir(tmp_path, sidecar=sidecar)
    assert infer_state(run_dir) == RunState.stopped


def test_infer_state_stopped_exception(tmp_path: Path):
    sidecar = _make_sidecar(ended=True, exit_reason=ExitReason.exception)
    run_dir = make_run_dir(tmp_path, sidecar=sidecar)
    assert infer_state(run_dir) == RunState.stopped


@pytest.mark.parametrize(
    "reason",
    [ExitReason.signal, ExitReason.max_orders_exceeded, ExitReason.fatal_halt, ExitReason.recovery_flatten],
)
def test_infer_state_stopped_various_reasons(tmp_path: Path, reason: ExitReason):
    sidecar = _make_sidecar(ended=True, exit_reason=reason)
    run_dir = make_run_dir(tmp_path, sidecar=sidecar)
    assert infer_state(run_dir) == RunState.stopped


def test_infer_state_waiting_for_bars(tmp_path: Path):
    """Active sidecar, no [BAR] in log, started within 60 s → waiting_for_bars."""
    sidecar = _make_sidecar(started_offset_s=5)  # started 5 s ago
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content="INFO startup complete\nINFO pre_flight OK\n",
    )
    assert infer_state(run_dir) == RunState.waiting_for_bars


def test_infer_state_waiting_for_bars_no_log(tmp_path: Path):
    """Active sidecar, no log file at all, started within 60 s → waiting_for_bars."""
    sidecar = _make_sidecar(started_offset_s=2)
    run_dir = make_run_dir(tmp_path, sidecar=sidecar)
    assert infer_state(run_dir) == RunState.waiting_for_bars


def test_infer_state_warming_up(tmp_path: Path):
    """Active sidecar, [BAR] in log, log recent, 0 decisions → warming_up."""
    sidecar = _make_sidecar(started_offset_s=30)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content=bar_line,
        decisions_rows=0,
        # log_mtime_offset=0 → just-written (fresh)
    )
    now_ms = int(time.time() * 1000)
    assert infer_state(run_dir, now_ms=now_ms) == RunState.warming_up


def test_infer_state_running(tmp_path: Path):
    """Active sidecar, [BAR] in log, log recent, decisions >= 1 → running."""
    sidecar = _make_sidecar(started_offset_s=30)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content=bar_line,
        decisions_rows=1,
    )
    now_ms = int(time.time() * 1000)
    assert infer_state(run_dir, now_ms=now_ms) == RunState.running


def test_infer_state_running_with_segmented_decision_dataset(tmp_path: Path) -> None:
    sidecar = _make_sidecar(started_offset_s=30)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content=bar_line,
        decisions_rows=0,
    )
    dataset_dir = run_dir / "decisions.parquet"
    dataset_dir.mkdir()
    pq.write_table(pa.table({"signal": ["ENTER"]}), dataset_dir / "part-000001.parquet")

    now_ms = int(time.time() * 1000)
    assert infer_state(run_dir, now_ms=now_ms) == RunState.running


def test_infer_state_stale_log_old(tmp_path: Path):
    """Active sidecar, [BAR] in log, but log mtime > 90 s → stale."""
    sidecar = _make_sidecar(started_offset_s=120)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    # Write log, then set mtime to 120 s ago
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content=bar_line,
        log_mtime_offset=120,  # 120 s old
    )
    now_ms = int(time.time() * 1000)
    assert infer_state(run_dir, now_ms=now_ms) == RunState.stale


def test_infer_state_stale_no_bars_and_old(tmp_path: Path):
    """Active sidecar, no [BAR] lines, started > 60 s ago → stale."""
    sidecar = _make_sidecar(started_offset_s=90)
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content="INFO startup\n",
        log_mtime_offset=91,
    )
    now_ms = int(time.time() * 1000)
    assert infer_state(run_dir, now_ms=now_ms) == RunState.stale


def test_infer_state_legacy_complete(tmp_path: Path):
    """No sidecar, log contains '[START] run completed cleanly' → complete."""
    run_dir = make_run_dir(
        tmp_path,
        log_content="INFO [START] run completed cleanly\n",
    )
    assert infer_state(run_dir) == RunState.complete


def test_infer_state_legacy_unknown(tmp_path: Path):
    """No sidecar, no clean completion in log → unknown."""
    run_dir = make_run_dir(
        tmp_path,
        log_content="INFO startup\nERROR something went wrong\n",
    )
    assert infer_state(run_dir) == RunState.unknown


def test_infer_state_unknown_empty_dir(tmp_path: Path):
    """Empty run dir, no sidecar, no flags → unknown."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert infer_state(run_dir) == RunState.unknown


def test_infer_state_now_ms_parameter(tmp_path: Path):
    """The now_ms parameter drives time-based decisions."""
    sidecar = _make_sidecar(started_offset_s=0)
    # Write a sidecar with started_at_ms = now (just started)
    bar_line = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set\n"
    run_dir = make_run_dir(
        tmp_path,
        sidecar=sidecar,
        log_content=bar_line,
        decisions_rows=0,
    )

    # With now_ms = now → log is fresh → warming_up
    now_ms = int(time.time() * 1000)
    assert infer_state(run_dir, now_ms=now_ms) == RunState.warming_up

    # With now_ms = 2 minutes later → log appears old → stale
    future_ms = now_ms + 120_000
    assert infer_state(run_dir, now_ms=future_ms) == RunState.stale

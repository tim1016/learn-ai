"""Tests for app.engine.live.run_status.

Covers:
- _atomic_write_json: writes via tmp file, renames, content correct
- write_run_status: creates run_status.json in run_dir
- Schema version round-trip: schema_version=1 survives model_dump → model_validate
- All 8 ExitReason literals are valid enum members
- RunStatusSidecar model_dump / model_validate round-trip for each ExitReason
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.engine.live.run_status import _atomic_write_json, write_run_status
from app.schemas.live_runs import ExitReason, RunStatusSidecar

# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_json_creates_file(tmp_path: Path):
    target = tmp_path / "output.json"
    payload = {"key": "value", "number": 42}
    _atomic_write_json(target, payload)
    assert target.exists()


def test_atomic_write_json_no_tmp_file_left(tmp_path: Path):
    target = tmp_path / "output.json"
    _atomic_write_json(target, {"x": 1})
    tmp = target.with_suffix(".tmp")
    assert not tmp.exists()


def test_atomic_write_json_content_correct(tmp_path: Path):
    target = tmp_path / "output.json"
    payload = {"schema_version": 1, "run_id": "abc123"}
    _atomic_write_json(target, payload)

    read_back = json.loads(target.read_text(encoding="utf-8"))
    assert read_back["schema_version"] == 1
    assert read_back["run_id"] == "abc123"


def test_atomic_write_json_overwrites_existing(tmp_path: Path):
    target = tmp_path / "output.json"
    _atomic_write_json(target, {"v": 1})
    _atomic_write_json(target, {"v": 2})

    read_back = json.loads(target.read_text(encoding="utf-8"))
    assert read_back["v"] == 2


# ---------------------------------------------------------------------------
# write_run_status
# ---------------------------------------------------------------------------


def _make_sidecar(run_id: str = "run-abc", exit_reason: ExitReason | None = None) -> RunStatusSidecar:
    now = int(time.time() * 1000)
    return RunStatusSidecar(
        run_id=run_id,
        started_at_ms=now - 5000,
        last_update_ms=now,
        ended_at_ms=now if exit_reason is not None else None,
        exit_code=0 if exit_reason is not None else None,
        exit_reason=exit_reason,
        host_pid=12345,
    )


def test_write_run_status_creates_file(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sidecar = _make_sidecar()
    write_run_status(run_dir, sidecar)

    expected = run_dir / "run_status.json"
    assert expected.exists()


def test_write_run_status_correct_fields(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sidecar = _make_sidecar("my-run-42")
    write_run_status(run_dir, sidecar)

    data = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
    assert data["run_id"] == "my-run-42"
    assert data["schema_version"] == 1
    assert data["host_pid"] == 12345


def test_write_run_status_schema_version_round_trip(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sidecar = _make_sidecar()
    write_run_status(run_dir, sidecar)

    raw = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
    restored = RunStatusSidecar.model_validate(raw)
    assert restored.schema_version == 1


# ---------------------------------------------------------------------------
# ExitReason — completeness
# ---------------------------------------------------------------------------


def test_exit_reason_has_nine_values():
    # 9th value: ``poisoned`` — the cold-start refusal of a poisoned run,
    # recorded so the console explains "fresh run_id required" (distinct from
    # the live engine's intra-day ``fatal_halt`` trip).
    assert len(ExitReason) == 9
    assert ExitReason.poisoned.value == "poisoned"


@pytest.mark.parametrize(
    "reason",
    [
        "normal",
        "force_flat_complete",
        "keyboard_interrupt",
        "signal",
        "max_orders_exceeded",
        "fatal_halt",
        "recovery_flatten",
        "exception",
        "poisoned",
    ],
)
def test_exit_reason_literal_valid(reason: str):
    er = ExitReason(reason)
    assert er.value == reason


# ---------------------------------------------------------------------------
# RunStatusSidecar round-trip for every ExitReason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(ExitReason))
def test_run_status_sidecar_round_trip(reason: ExitReason):
    now = int(time.time() * 1000)
    sidecar = RunStatusSidecar(
        run_id="test-run",
        started_at_ms=now - 10_000,
        last_update_ms=now,
        ended_at_ms=now,
        exit_code=0,
        exit_reason=reason,
        host_pid=99,
    )
    dumped = sidecar.model_dump()
    restored = RunStatusSidecar.model_validate(dumped)

    assert restored.run_id == sidecar.run_id
    assert restored.exit_reason == reason
    assert restored.schema_version == 1


def test_run_status_sidecar_active_run_round_trip():
    """Active run (no ended_at_ms) round-trips cleanly."""
    now = int(time.time() * 1000)
    sidecar = RunStatusSidecar(
        run_id="active-run",
        started_at_ms=now - 1000,
        last_update_ms=now,
        ended_at_ms=None,
        exit_code=None,
        exit_reason=None,
        host_pid=777,
    )
    restored = RunStatusSidecar.model_validate(sidecar.model_dump())
    assert restored.ended_at_ms is None
    assert restored.exit_reason is None

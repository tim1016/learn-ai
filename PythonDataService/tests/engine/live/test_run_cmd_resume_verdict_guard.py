"""Phase 7B Resume guard #1 (PRD §7B / VCR-0010) — cmd_resume consults
the persisted ``verdict_snapshot.json`` written by the engine's bar-loop
verdict observer.

Refuses to flip ``desired_state`` to RUNNING when the latest run's
snapshot carries a verdict other than ``"paper-only"``. Operator can
pass ``--force`` after confirming the broker session is back on a
paper account.

The engine's snapshot write is tested separately in
``test_live_engine_verdict_observer.py``; these tests cover the CLI
side.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.engine.live.desired_state import DesiredState
from app.engine.live.run import _scan_verdict_snapshot, cmd_resume


def _seed_snapshot(run_dir: Path, *, verdict: str | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"observed_at_ms_utc": 1718553600123}
    if verdict is not None:
        payload["verdict"] = verdict
    (run_dir / "verdict_snapshot.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _seed_run_dir(artifacts_root: Path, instance_id: str = "test-instance") -> Path:
    """Seed ``<artifacts_root>/live_runs/<run_id>/`` with a run_ledger.json
    naming this instance, plus an empty intent_events.jsonl so the
    Phase 5D guard does not fire.

    Matches the layout ``_latest_run_dir_for_instance`` scans.
    """
    run_dir = artifacts_root / "live_runs" / f"{instance_id}-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps({"strategy_instance_id": instance_id}), encoding="utf-8"
    )
    (run_dir / "intent_events.jsonl").write_text("", encoding="utf-8")
    return run_dir


def _desired_state_path(artifacts_root: Path, instance_id: str) -> Path:
    """Path layout used by ``stable_desired_state_path``:
    ``<artifacts_root>/live_state/<instance_id>/desired_state.json``.
    """
    return artifacts_root / "live_state" / instance_id / "desired_state.json"


def _args(
    artifacts_root: Path,
    instance_id: str,
    *,
    force: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        artifacts_root=artifacts_root,
        strategy_instance_id=instance_id,
        force=force,
        reason=None,
        updated_by="operator",
    )


# ──────────────────────────── _scan_verdict_snapshot ─────────────────


def test_scan_verdict_snapshot_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") is None


def test_scan_verdict_snapshot_returns_none_on_paper_only(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, verdict="paper-only")
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") is None


@pytest.mark.parametrize("verdict", ["unsafe", "unknown", "live", "real-money"])
def test_scan_verdict_snapshot_returns_verdict_string_for_non_paper(
    tmp_path: Path, verdict: str
) -> None:
    _seed_snapshot(tmp_path, verdict=verdict)
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") == verdict


def test_scan_verdict_snapshot_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    """Fail-open on corruption: a torn write or malformed JSON must not
    jail the operator out of Resume. The engine bar loop is the
    secondary defense and overwrites the snapshot on the next check."""
    (tmp_path / "verdict_snapshot.json").write_text("{not json}", encoding="utf-8")
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") is None


def test_scan_verdict_snapshot_returns_none_on_missing_verdict_field(
    tmp_path: Path,
) -> None:
    (tmp_path / "verdict_snapshot.json").write_text(
        json.dumps({"observed_at_ms_utc": 0}), encoding="utf-8"
    )
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") is None


# ──────────────────────────── cmd_resume integration ─────────────────


def test_cmd_resume_refuses_on_non_paper_only_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    instance = "verdict-guard-instance"
    run_dir = _seed_run_dir(tmp_path, instance)
    _seed_snapshot(run_dir, verdict="unsafe")
    desired_state_path = _desired_state_path(tmp_path, instance)

    rc = cmd_resume(_args(tmp_path, instance, force=False))

    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "'unsafe'" in err
    assert "verdict_snapshot.json" in err
    # desired_state.json must NOT have been written.
    assert not desired_state_path.exists()


def test_cmd_resume_paper_only_verdict_proceeds(tmp_path: Path) -> None:
    instance = "happy-resume-instance"
    run_dir = _seed_run_dir(tmp_path, instance)
    _seed_snapshot(run_dir, verdict="paper-only")
    desired_state_path = _desired_state_path(tmp_path, instance)

    rc = cmd_resume(_args(tmp_path, instance, force=False))

    assert rc == 0
    # desired_state.json was written by _cmd_set_desired_state.
    written = json.loads(desired_state_path.read_text())
    assert written["desired_state"] == DesiredState.RUNNING.value


def test_cmd_resume_force_overrides_non_paper_verdict(tmp_path: Path) -> None:
    instance = "force-resume-instance"
    run_dir = _seed_run_dir(tmp_path, instance)
    _seed_snapshot(run_dir, verdict="unsafe")
    desired_state_path = _desired_state_path(tmp_path, instance)

    rc = cmd_resume(_args(tmp_path, instance, force=True))

    assert rc == 0
    written = json.loads(desired_state_path.read_text())
    assert written["desired_state"] == DesiredState.RUNNING.value


def test_cmd_resume_no_snapshot_no_wal_proceeds(tmp_path: Path) -> None:
    """Backward compatibility: a run dir without verdict_snapshot.json
    (older runs that predate Phase 7B) and without unresolved uncertains
    in the WAL must let Resume through. The Resume guard is opt-in via
    file presence — the engine writes the snapshot once it observes a
    verdict, and absence is treated as "no observation yet"."""
    instance = "legacy-resume-instance"
    _seed_run_dir(tmp_path, instance)
    # No snapshot, empty WAL — Resume must succeed.

    rc = cmd_resume(_args(tmp_path, instance, force=False))

    assert rc == 0
    assert _desired_state_path(tmp_path, instance).exists()


def test_cmd_resume_no_run_dir_proceeds(tmp_path: Path) -> None:
    """Fresh instance with no prior run dir: nothing to consult, so the
    guard does not fire."""
    instance = "fresh-instance"

    rc = cmd_resume(_args(tmp_path, instance, force=False))

    assert rc == 0
    assert _desired_state_path(tmp_path, instance).exists()

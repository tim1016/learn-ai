"""PRD #616 — CLI ``cmd_resume`` consumes the shared ``ResumeGuardState``
resolver.

The CLI used to scan ``verdict_snapshot.json`` and ``intent_events.jsonl``
directly with ``--force`` as a bypass.  PRD #616 rewired it to the shared
resolver and deleted ``--force`` so the cockpit's structural-safety claim
("guarded Resume is structurally safe") holds across every entry point.

The legacy ``_scan_verdict_snapshot`` / ``_scan_wal_for_unresolved_uncertains``
helpers remain in the module as private references for the old write path;
the shared resolver tests (tests/services/test_resume_guards.py) cover the
new fold layer directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.engine.live.account_artifacts import AccountFreezeEvidence, write_account_freeze
from app.engine.live.desired_state import DesiredState
from app.engine.live.run import _scan_verdict_snapshot, build_parser, cmd_resume


def _seed_snapshot(run_dir: Path, *, verdict: str | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"observed_at_ms_utc": 1718553600123}
    if verdict is not None:
        payload["verdict"] = verdict
    (run_dir / "verdict_snapshot.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_run_dir(
    artifacts_root: Path,
    instance_id: str = "test-instance",
    *,
    account_id: str | None = None,
) -> Path:
    """Seed ``<artifacts_root>/live_runs/<run_id>/`` with a run_ledger.json
    naming this instance, plus an empty intent_events.jsonl so the
    uncertain-intent guard does not fire.

    PRD #619-A: also seed ``run_status.json`` with the durable child/run
    capability evidence (``submit_mode_at_start`` + ``readonly_at_start``)
    so the new submission-capability gate is SATISFIED. Tests that want
    to exercise capability-UNKNOWN can delete or rewrite the file.
    """
    run_dir = artifacts_root / "live_runs" / f"{instance_id}-run-1"
    run_dir.mkdir(parents=True)
    ledger: dict[str, object] = {"strategy_instance_id": instance_id}
    if account_id is not None:
        ledger["account_id"] = account_id
    (run_dir / "run_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (run_dir / "intent_events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": f"{instance_id}-run-1",
                "started_at_ms": 1_700_000_000_000,
                "last_update_ms": 1_700_000_000_000,
                "host_pid": 1,
                "submit_mode_at_start": "live_paper",
                "readonly_at_start": False,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _desired_state_path(artifacts_root: Path, instance_id: str) -> Path:
    return artifacts_root / "live_state" / instance_id / "desired_state.json"


def _args(artifacts_root: Path, instance_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        artifacts_root=artifacts_root,
        strategy_instance_id=instance_id,
        reason=None,
        updated_by="operator",
    )


# ──────────────────────────── _scan_verdict_snapshot (legacy helper) ───


def test_legacy_scan_verdict_snapshot_returns_none_when_file_missing(tmp_path: Path) -> None:
    # PRD #616 — the helper is retained for back-compat callers but
    # the CLI no longer consults it; the shared resolver replaces it.
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") is None


def test_legacy_scan_verdict_snapshot_returns_none_on_paper_only(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, verdict="paper-only")
    assert _scan_verdict_snapshot(tmp_path / "verdict_snapshot.json") is None


# ──────────────────────────── cmd_resume — shared resolver ─────────────


def test_cmd_resume_refuses_on_non_paper_only_verdict(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    instance = "verdict-guard-instance"
    run_dir = _seed_run_dir(tmp_path, instance)
    _seed_snapshot(run_dir, verdict="unsafe")
    desired_state_path = _desired_state_path(tmp_path, instance)

    rc = cmd_resume(_args(tmp_path, instance))

    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "BROKER_SAFETY_UNSAFE" in err
    assert "unsafe" in err
    # desired_state.json must NOT have been written.
    assert not desired_state_path.exists()


def test_cmd_resume_refuses_on_unresolved_uncertain_intent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal

    instance = "uncertain-instance"
    run_dir = _seed_run_dir(tmp_path, instance)
    _seed_snapshot(run_dir, verdict="paper-only")
    (run_dir / "intent_events.jsonl").unlink()
    IntentWal(run_dir / "intent_events.jsonl").append(
        event_type=IntentEventType.ACK_FAILED_UNCERTAIN,
        intent_id="intent-a",
        bot_order_namespace="ns",
        order_ref="ns:intent-a",
        ts_ms=1_700_000_000_000,
    )

    rc = cmd_resume(_args(tmp_path, instance))

    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "UNRESOLVED_UNCERTAIN_INTENT" in err
    assert not _desired_state_path(tmp_path, instance).exists()


def test_cmd_resume_paper_only_verdict_proceeds(tmp_path: Path) -> None:
    instance = "happy-resume-instance"
    run_dir = _seed_run_dir(tmp_path, instance)
    _seed_snapshot(run_dir, verdict="paper-only")
    desired_state_path = _desired_state_path(tmp_path, instance)

    rc = cmd_resume(_args(tmp_path, instance))

    assert rc == 0
    written = json.loads(desired_state_path.read_text())
    assert written["desired_state"] == DesiredState.RUNNING.value


def test_cmd_resume_refuses_active_account_freeze(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    instance = "frozen-account-instance"
    account_id = "DU123456"
    run_dir = _seed_run_dir(tmp_path, instance, account_id=account_id)
    _seed_snapshot(run_dir, verdict="paper-only")
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id=account_id,
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )

    rc = cmd_resume(_args(tmp_path, instance))

    assert rc == 2
    err = capsys.readouterr().err
    assert "ACCOUNT_FREEZE_ACTIVE" in err
    assert "watchdog.flatten_failed" in err
    assert not _desired_state_path(tmp_path, instance).exists()


def test_cmd_resume_refuses_when_verdict_snapshot_missing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # PRD #616 — fail-closed: a missing snapshot is UNKNOWN, not
    # silently allowed.  Older runs that pre-date the snapshot must
    # reconnect / re-establish the verdict before Resume.
    instance = "legacy-resume-instance"
    _seed_run_dir(tmp_path, instance)
    # No snapshot at all.

    rc = cmd_resume(_args(tmp_path, instance))

    assert rc == 2
    err = capsys.readouterr().err
    assert "BROKER_SAFETY_UNKNOWN" in err


def test_cmd_resume_no_run_dir_proceeds(tmp_path: Path) -> None:
    """Fresh instance with no prior run dir: nothing to safeguard yet, so
    the resolver returns an empty guard state (allow_resume=True)."""
    instance = "fresh-instance"

    rc = cmd_resume(_args(tmp_path, instance))

    assert rc == 0
    assert _desired_state_path(tmp_path, instance).exists()


def test_resume_parser_no_longer_accepts_force() -> None:
    # PRD #616 — the ``--force`` flag was deleted from the CLI; an
    # invocation that still passes it must fail at parse time.  This
    # is the structural guarantee that the cockpit's contract holds
    # across entry points.
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "resume",
                "--strategy-instance-id",
                "anything",
                "--force",
            ]
        )

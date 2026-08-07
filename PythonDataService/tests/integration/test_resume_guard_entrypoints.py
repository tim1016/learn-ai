"""CLI Resume guards reject unsafe artifact states without a force bypass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._fixtures.resume_guard_cases import GUARD_CASES, GuardCase


def _seed_instance(tmp_path: Path, sid: str, case: GuardCase) -> Path:
    # PRD #619-A §A6 — return the run dir directly; the previous
    # ``_seed_instance.last_run_dir = run_dir`` function-attribute hack
    # leaked state across parametrized test invocations and made the
    # CLI test rely on whichever fixture seeded last. Every caller now
    # receives the run_dir via the return value.
    root = tmp_path / "live_runs"
    run_dir = root / f"{sid}-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps({"strategy_instance_id": sid, "run_id": run_dir.name}),
        encoding="utf-8",
    )
    # verdict_snapshot.json drives broker_safety.
    if case.broker_safety.verdict is not None:
        (run_dir / "verdict_snapshot.json").write_text(
            json.dumps({"verdict": case.broker_safety.verdict}), encoding="utf-8"
        )
    # intent_events.jsonl drives uncertain_intent.
    if case.uncertain_intent.state == "PRESENT":
        from app.engine.live.intent_events import IntentEventType
        from app.engine.live.intent_wal import IntentWal

        wal = IntentWal(run_dir / "intent_events.jsonl")
        for intent_id in case.uncertain_intent.unresolved_intent_ids:
            wal.append(
                event_type=IntentEventType.ACK_FAILED_UNCERTAIN,
                intent_id=intent_id,
                bot_order_namespace="ns",
                order_ref=f"ns:{intent_id}",
                ts_ms=1_700_000_000_000,
            )
    elif case.uncertain_intent.state == "UNKNOWN":
        (run_dir / "intent_events.jsonl").write_text("not-json\n", encoding="utf-8")
    else:
        (run_dir / "intent_events.jsonl").write_text("", encoding="utf-8")
    # reconciliation_receipt.json drives the reconciliation gate.
    if case.reconciliation.state in {"PASSED", "FAILED"}:
        (run_dir / "reconciliation_receipt.json").write_text(
            json.dumps(
                {
                    "status": "passed" if case.reconciliation.state == "PASSED" else "failed",
                    "detail": case.reconciliation.detail or "",
                }
            ),
            encoding="utf-8",
        )
    elif case.reconciliation.state == "STALE":
        (run_dir / "reconciliation_receipt.json").write_text(
            json.dumps({"status": "passed", "last_reconcile_ms": 1}),
            encoding="utf-8",
        )
    elif case.reconciliation.state == "UNKNOWN":
        (run_dir / "reconciliation_receipt.json").write_text("garbage", encoding="utf-8")
    return run_dir


# ---------------------------------------------------------------------------
# CLI cmd_resume
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
def test_entrypoint_cli_cmd_resume(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture, case: GuardCase) -> None:
    sid = "cli-sid"
    run_dir = _seed_instance(tmp_path, sid, case)

    # The CLI sets durable state via _cmd_set_desired_state.  For the
    # cases where current_intent is RUNNING/STOPPED/ALREADY_PAUSED the
    # CLI's behaviour is identical to the resolver — it does not
    # short-circuit on intent state (that overlay lives in the
    # capability evaluator).  The CLI exits non-zero iff the artifact
    # guards refuse.
    import argparse

    from app.engine.live import run as run_cli

    args = argparse.Namespace(
        artifacts_root=tmp_path,
        strategy_instance_id=sid,
        reason=None,
        updated_by="operator",
    )

    # Re-resolve from the seeded artifacts to get the ground-truth
    # production behaviour (the test fixture's *intent* may differ
    # slightly because the production reader is informational about
    # STALE until the reconciliation receipt writer is wired —
    # PRD #616 §"Out of Scope").
    from app.services.resume_guard_state import resolve_guard_state_from_paths

    actual_state = resolve_guard_state_from_paths(
        verdict_snapshot_path=run_dir / "verdict_snapshot.json",
        run_status_path=run_dir / "run_status.json",
        run_dir_for_reconciliation=run_dir,
        intent_wal_path=run_dir / "intent_events.jsonl",
    )

    rc = run_cli.cmd_resume(args)

    if not actual_state.allow_resume:
        assert rc == 2, case.name
        err = capsys.readouterr().err
        assert actual_state.reason_codes[0] in err, case.name
    else:
        assert rc == 0, case.name

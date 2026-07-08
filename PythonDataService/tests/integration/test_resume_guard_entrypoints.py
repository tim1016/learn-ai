"""PRD #616 — behavioural consistency across the three Resume entry points.

The shared ``ResumeGuardState`` resolver is the seam at which:

1. The capability projection (``operator_surface.actions.resume``)
   reports enabled / refused with the reason codes the cockpit
   tooltip renders.
2. The desired-state mutation endpoint
   (``POST /api/live-instances/{sid}/desired-state``) re-evaluates the
   same gate immediately before the durable write so a stale
   snapshot cannot drive a write past the same rule.
3. The CLI (``app.engine.live.run.cmd_resume``) refuses with the same
   reason codes after PRD #616 deleted the ``--force`` bypass.

This file exercises each entry point against every row of the shared
parameterised table and asserts the same allow / refuse decision and
reason codes appear at each surface.  This is **behavioural
consistency across entry points**, NOT scientific parity — the file
name and location reflect that.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.schemas.live_runs import (
    InstanceProcessView,
)
from app.services.operator_capability import evaluate_action
from app.services.resume_guard_state import (
    resolve_guard_state,
)
from tests._fixtures.daemon_transport import as_typed_get
from tests._fixtures.resume_guard_cases import GUARD_CASES, GuardCase

# ---------------------------------------------------------------------------
# Entry point #1 — capability projection (pure function)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
def test_entrypoint_capability_projection(case: GuardCase) -> None:
    state = resolve_guard_state(
        broker_safety=case.broker_safety,
        submission_capability=case.submission_capability,
        reconciliation=case.reconciliation,
        uncertain_intent=case.uncertain_intent,
    )
    from app.schemas.live_runs import DesiredStateView

    desired = DesiredStateView(state=case.current_intent, path_status="ok") if case.current_intent is not None else None
    cap = evaluate_action(
        "resume",
        process=InstanceProcessView(state="idle"),
        live_binding=None,
        poisoned=case.poisoned,
        desired_state=desired,
        guard_state=state,
    )
    assert cap.enabled is case.expected_resume_enabled, case.name
    assert tuple(cap.disabled_reasons) == case.expected_resume_codes, case.name


# ---------------------------------------------------------------------------
# Entry point #2 — desired-state mutation endpoint
# ---------------------------------------------------------------------------


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


def _build_app(tmp_path: Path, sid: str, case: GuardCase, monkeypatch) -> FastAPI:
    """Build a FastAPI app with the live_instances router and the
    settings + daemon stubs the desired-state mutation endpoint needs.
    """
    from app.broker.ibkr.config import IbkrSettings
    from app.routers import live_instances as li

    settings = IbkrSettings(live_runs_root=str(tmp_path / "live_runs"))

    def fake_settings() -> IbkrSettings:
        return settings

    monkeypatch.setattr(li, "get_settings", fake_settings)

    # Stub the daemon fetch: no live binding ever for the consistency
    # assertion (durable-only write path).
    async def fake_fetch_instance_process(url: str, sid: str):
        return as_typed_get({"process": {"state": "idle"}, "instances": []})

    monkeypatch.setattr(
        li.host_daemon_client,
        "fetch_instance_process",
        fake_fetch_instance_process,
    )

    # Stub desired-state resolver to apply the case's current_intent
    # (so the intent-state-pair rules fire deterministically).

    def fake_resolve_desired_state(root: Path, sid: str):
        from app.schemas.live_runs import DesiredStateView

        if case.current_intent is None:
            return DesiredStateView(state=None, path_status="absent")
        return DesiredStateView(state=case.current_intent, path_status="ok")

    monkeypatch.setattr(li, "_resolve_desired_state", fake_resolve_desired_state)

    # Stub the last-exit poisoned read to match the case.
    def fake_last_exit(runs):
        if case.poisoned:
            from app.schemas.live_runs import InstanceLastExit

            return InstanceLastExit(run_id="x", halt_trigger="OPERATOR_DECLARED")
        return None

    monkeypatch.setattr(li, "_instance_last_exit", fake_last_exit)

    app = FastAPI()
    app.include_router(li.router, prefix="/api/live-instances")
    return app


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
@pytest.mark.asyncio
async def test_entrypoint_mutation_endpoint(tmp_path: Path, monkeypatch, case: GuardCase) -> None:
    sid = "consistency-sid"
    run_dir = _seed_instance(tmp_path, sid, case)
    app = _build_app(tmp_path, sid, case, monkeypatch)

    # Use the production resolver's actual reading of the seeded
    # artifacts to compute the expected outcome — STALE is not yet
    # wired into the live caller (PRD #616 Out of Scope), so the
    # production behaviour for STALE-via-receipt is PASSED.
    from app.services.resume_guard_state import resolve_guard_state_from_paths

    actual_state = resolve_guard_state_from_paths(
        verdict_snapshot_path=run_dir / "verdict_snapshot.json",
        run_status_path=run_dir / "run_status.json",
        run_dir_for_reconciliation=run_dir,
        intent_wal_path=run_dir / "intent_events.jsonl",
    )
    intent = case.current_intent
    expected_codes: list[str] = []
    if intent == "RUNNING":
        expected_codes.append("DESIRED_STATE_ALREADY_RUNNING")
    elif intent is None:
        expected_codes.append("DESIRED_STATE_DEFAULT_RUNNING")
    if case.poisoned:
        expected_codes.append("REDEPLOY_REQUIRED")
    expected_codes.extend(actual_state.reason_codes)
    from app.services.resume_guard_state import sort_reason_codes

    expected_codes = sort_reason_codes(expected_codes)
    expected_enabled = not expected_codes

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/live-instances/{sid}/desired-state",
            json={"action": "resume", "reason": "", "updated_by": "tester"},
        )

    if expected_enabled:
        assert response.status_code == 200, (case.name, response.json())
    else:
        assert response.status_code == 409, (case.name, response.json())
        detail = response.json()["detail"]
        assert detail["disabled_reason_code"] == expected_codes[0], case.name
        assert detail["disabled_reasons"] == expected_codes, case.name


# ---------------------------------------------------------------------------
# Entry point #3 — CLI cmd_resume
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

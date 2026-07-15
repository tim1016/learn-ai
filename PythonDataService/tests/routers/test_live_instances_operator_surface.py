"""Contract tests for the ``operator_surface`` projection on
``LiveInstanceStatus`` (PRD #607 / Slice 1 / #608).

The projection is the single source of truth for operational verdicts,
risk posture, structured daily-cap usage, action-plan consumption,
broker safety verdict, prior-run classification, host-process state, and
per-action capability + reason codes.  Frontend (and any other consumer)
renders these fields; it does not reason about raw state.

These tests build cumulatively, one cycle at a time, vertical-slice TDD:
each cycle exercises one observable behavior end-to-end through the REST
endpoint.  Per-section unit tests live alongside the projection module
under ``tests/services/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import (
    AccountClerkLease,
    AccountFreezeEvidence,
    AccountOwnerGeneration,
    advance_account_clerk_generation,
    write_account_clerk_lease,
    write_account_freeze,
    write_account_owner_generation,
)
from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
    write_engine_runtime_snapshot,
)
from app.operator.incidents.store import IncidentStore
from app.operator.incidents.watchdog_notices import watchdog_incident
from app.routers import live_instances
from tests._fixtures.daemon_transport import as_typed_get


def _write_ledger(
    root: Path,
    run_id: str,
    sid: str,
    created_at_ms: int,
    spec_path: Path | None = None,
    account_id: str | None = None,
) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    payload: dict = {"run_id": run_id, "strategy_instance_id": sid, "created_at_ms": created_at_ms}
    if account_id is not None:
        payload["account_id"] = account_id
    if spec_path is not None:
        payload["strategy_spec_path"] = str(spec_path)
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.main import app

    return app, root


def _set_daemon(monkeypatch: pytest.MonkeyPatch, *, process: dict | None = None) -> None:
    async def fake_instances(_base_url: str):
        return as_typed_get(None)

    async def fake_process(_base_url: str, _sid: str):
        return as_typed_get(process)

    monkeypatch.setattr(host_daemon_client, "fetch_instances", fake_instances)
    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", fake_process)


def _write_runtime_snapshot(
    root: Path,
    run_id: str,
    sid: str,
    now_ms: int,
    *,
    connection_state: str = "connected",
) -> None:
    write_engine_runtime_snapshot(
        root / run_id,
        EngineRuntimeSnapshot(
            strategy_instance_id=sid,
            run_id=run_id,
            pid=123,
            process_start_identity="child-1",
            expected_daemon_boot_id="boot-1",
            snapshot_seq=1,
            written_at_ms=now_ms,
            command_loop=CommandLoopBlock(
                heartbeat_at_ms=now_ms,
                state="PAUSED",
            ),
            broker=BrokerBlock(
                identity="PAPER_VERIFIED",
                submission_capability="PAPER_ORDERS_ENABLED",
                effective_posture="PAPER_EXECUTION",
                connection_state=connection_state,
                connection_epoch=1,
                connected_account="DU123",
                port_class="paper_port",
                observation_at_ms=now_ms,
                probe_completed_at_ms=now_ms,
                reconnect_attempt=0,
            ),
            bar_loop=BarLoopBlock(
                heartbeat_at_ms=now_ms,
                latest_source_bar_ms=now_ms,
                expected_interval_ms=60_000,
            ),
            control_plane=ControlPlaneBlock(
                lease_observed_at_ms=now_ms,
                observed_daemon_boot_id="boot-1",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Cycle 1 — tracer bullet: operator_surface field with schema_version: 2
# ---------------------------------------------------------------------------


async def test_status_response_includes_operator_surface_schema_version_two(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracer bullet: the new projection field appears on every status
    response (running OR dead instance) and carries ``schema_version: 2``.

    All other fields of the projection are exercised by cumulative cycles
    below.  This first test just asserts the field is *present* and pinned
    so that downstream slices can rely on its existence.
    """

    app, root = app_with_root
    _write_ledger(root, "run-aaa", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    body = response.json()
    assert "operator_surface" in body, "Slice 1 contract: operator_surface field missing"
    assert body["operator_surface"]["schema_version"] == 2


# ---------------------------------------------------------------------------
# Cycle 2 — host_process block: state, notice, copyable_command
# ---------------------------------------------------------------------------


async def test_host_process_block_stopped_when_daemon_idle(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the host-daemon process is ``idle`` (reachable but nothing
    running for this instance) the projection authors a non-null
    operator-language ``notice`` so the cockpit can surface that the
    instance must be started from the host runner (ADR-0003 / ADR-0007 —
    host-process lifecycle is operator-owned, not cockpit-owned)."""

    app, root = app_with_root
    _write_ledger(root, "run-bbb", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    body = response.json()
    host = body["operator_surface"]["host_process"]
    # ``idle`` daemon + the test fixture's default desired_state=RUNNING
    # (live_instances router defaults `desired.state = "RUNNING"` when
    # absent) -> WAITING_FOR_HOST.  Distinct from a plain IDLE: the
    # operator has expressed intent and is waiting for the subprocess.
    assert host["state"] in {"IDLE", "WAITING_FOR_HOST"}
    assert isinstance(host["notice"], str) and host["notice"]
    assert host["copyable_command"] is None


async def test_host_process_block_running_when_daemon_bound_to_run(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the host-daemon reports the instance ``running``, the
    projection emits ``RUNNING`` and authors NO notice — the cockpit
    has nothing operational to surface and the notice block stays
    hidden."""

    app, root = app_with_root
    _write_ledger(root, "run-ccc", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-ccc", "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    body = response.json()
    host = body["operator_surface"]["host_process"]
    assert host["state"] == "RUNNING"
    assert host["notice"] is None
    assert host["copyable_command"] is None


# ---------------------------------------------------------------------------
# Cycle 11 — mutation endpoints re-evaluate the shared capability
# evaluator server-side and reject with 409 + reason code
# ---------------------------------------------------------------------------


async def test_flatten_and_pause_returns_409_no_live_binding_when_unbound(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale UI that issues flatten-and-pause against an unbound
    instance must be rejected by the same capability evaluator the
    cockpit reads from the status snapshot.  The Frontend handles the
    409 by reloading status (see #610)."""

    app, root = app_with_root
    _write_ledger(root, "run-ddd", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/flatten-and-pause")

    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["disabled_reason_code"] == "NO_LIVE_BINDING"


async def test_mark_poisoned_returns_409_no_live_binding_when_unbound(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MARK_POISONED through the generic commands endpoint must also
    re-evaluate the shared capability gate and reject when unbound."""

    app, root = app_with_root
    _write_ledger(root, "run-eee", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands",
            json={"verb": "MARK_POISONED"},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["disabled_reason_code"] == "NO_LIVE_BINDING"


# ---------------------------------------------------------------------------
# Cycle 12 — acceptance: running-instance fixture exercises every block
# ---------------------------------------------------------------------------


async def test_running_instance_status_carries_every_operator_surface_block(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the full ``operator_surface`` wire shape on a running
    instance.  Downstream slices (Frontend types + contract fixtures)
    read this shape and would break loudly if a block were renamed,
    dropped, or null-ified."""

    app, root = app_with_root
    _write_ledger(root, "run-fff", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-fff", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]

    assert set(surface) == {
        "schema_version",
        "host_process",
        "prior_run",
        "broker",
        # Workbench Slice 2 — backend-authored execution posture projection.
        "execution",
        "configuration",
        "current_risk",
        "daily_order_cap",
        "account_clerk",
        # Durable Account Truth observation proof for account-scoped safety.
        "account_observation",
        "submit_readiness",
        "trader_guidance",
        "blockage_ladder",
        "run_signal",
        "control_plane",
        "action_plan",
        "actions",
        "confirmations",
        "trading_session",
        # PRD #616 — additive operator-facing projections.
        "readiness_gates",
        # OperatorBlocker disposition contract shared by deploy/control.
        "blockers",
        "runtime_freshness",
        # PRD #619-D4 — broker observation consistency surface.
        "broker_observation_consistency",
        # ADR-0008 §5 / Reconciliation PR 1 — cold-start receipt projection.
        "reconciliation",
        # Operator-notice PR 2 — post-halt incident headline (None unless an
        # unresolved watchdog incident requires reconciliation).
        "incident_headline",
        # Operator-notice PR 5 — broker-activity publisher health surface.
        "broker_activity_health",
        # ADR-0025 / PRD #972 — backend-authored single-banner placement.
        "notice_placement",
    }
    assert surface["schema_version"] == 2
    assert surface["host_process"]["state"] == "RUNNING"
    assert surface["run_signal"] == {
        "state_label": "On",
        "tone": "on",
        "title": "Bot process is running",
        "detail": "The host daemon reports this bot process is running.",
    }
    assert surface["prior_run"]["classification"] == "UNKNOWN"
    # Two independent enums now.
    assert surface["broker"]["safety_verdict"] in {"PAPER_ONLY", "UNSAFE", "UNKNOWN"}
    assert surface["broker"]["connection"] in {"CONNECTED", "DISCONNECTED", "DEGRADED", "UNKNOWN"}
    assert surface["execution"]["posture"] in {
        "PAPER_EXECUTION",
        "READ_ONLY",
        "UNSAFE",
        "UNKNOWN",
    }
    assert surface["configuration"]["verdict"] in {"READY", "ATTENTION", "UNKNOWN"}
    assert surface["current_risk"]["verdict"] in {"READY", "ATTENTION", "UNKNOWN"}
    assert surface["submit_readiness"]["code"] in {
        "safe_to_submit",
        "safe_to_monitor",
        "blocked_before_submit",
        "broker_state_unproven",
        "account_frozen",
        "waiting_for_clerk_generation",
        "submit_outcome_uncertain",
    }
    assert surface["trader_guidance"]["headline"]
    assert surface["trader_guidance"]["primary_remediation"]["kind"] in {
        "invoke_capability",
        "focus_action",
        "redeploy",
        "open_runbook",
        "invoke_endpoint",
        "none",
    }
    assert surface["trader_guidance"]["proof_lines"]
    assert {line["id"] for line in surface["trader_guidance"]["proof_lines"]} == {
        "broker-proof",
        "submit-readiness",
            "account-clerk",
        "reconciliation",
        "runtime-freshness",
    }
    assert surface["daily_order_cap"]["used"] is None
    assert surface["daily_order_cap"]["limit"] is None
    # Trading-session projection is always present; phase + permission
    # are server-authored.
    session = surface["trading_session"]
    assert session["phase"] in {"PRE", "RTH", "POST", "CLOSED", "UNKNOWN"}
    assert session["timezone"] == "America/New_York"
    assert isinstance(session["as_of_ms"], int)
    # PRD #616 — five canonical actions (stop added); every action
    # capability carries the full (priority-ordered) reason list and
    # the head as the single-line tooltip code.
    for name in ("resume", "pause", "stop", "flatten_and_pause", "mark_poisoned"):
        cap = surface["actions"][name]
        assert set(cap.keys()) == {
            "enabled",
            "effect",
            "disabled_reason_code",
            "disabled_reasons",
            "gate_results",
        }
        assert isinstance(cap["gate_results"], list)
        assert cap["effect"] in {"DURABLE_ONLY", "LIVE_ACTUATION"}
        if cap["enabled"]:
            assert cap["disabled_reason_code"] is None
            assert cap["disabled_reasons"] == []
        else:
            assert isinstance(cap["disabled_reasons"], list)
            assert cap["disabled_reasons"]
    # readiness_gates projection is always present (even empty).
    assert isinstance(surface["readiness_gates"], list)
    assert surface["runtime_freshness"]["posture_demoted"] is True
    assert surface["runtime_freshness"]["stale_reason_codes"] == ["ENGINE_RUNTIME_MISSING"]
    for name in ("resume", "flatten_and_pause"):
        assert surface["actions"][name]["enabled"] is False
        assert surface["actions"][name]["disabled_reason_code"] == "POSTURE_DEMOTED"
    for name in ("pause", "stop"):
        assert surface["actions"][name]["enabled"] is True


async def test_running_instance_status_uses_active_account_clerk_not_legacy_owner_generation(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    now_ms = 1_700_000_001_000
    monkeypatch.setattr(live_instances, "_now_ms", lambda: now_ms)
    _write_ledger(root, "run-owner", "spy_ema_paper", 100, account_id="DU123")
    write_account_owner_generation(
        root.parent,
        AccountOwnerGeneration(
            account_id="DU123",
            generation=9,
            phase="draining",
            recorded_at_ms=1_700_000_001_000,
            source="account_owner",
        ),
    )
    clerk = advance_account_clerk_generation(
        root.parent,
        "DU123",
        phase="accepting",
        recorded_at_ms=now_ms,
        source="account_clerk",
    )
    write_account_clerk_lease(
        root.parent,
        AccountClerkLease(
            account_id="DU123",
            generation=clerk.generation,
            pid=123,
            ibkr_client_id=51,
            status="RUNNING",
            started_at_ms=now_ms,
            renewed_at_ms=now_ms,
            valid_until_ms=now_ms + 60_000,
        ),
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-owner", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert "account_owner" not in surface
    assert surface["account_clerk"] == {
        "account_id": "DU123",
        "generation": 1,
        "phase": "accepting",
        "lease_active": True,
        "recorded_at_ms": now_ms,
        "source": "account_clerk",
    }


async def test_running_instance_fresh_runtime_keeps_actions_current(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    now_ms = 1_772_463_600_000  # 2026-03-02 10:00:00 America/New_York
    _write_ledger(root, "run-runtime", "spy_ema_paper", 100)
    write_engine_runtime_snapshot(
        root / "run-runtime",
        EngineRuntimeSnapshot(
            strategy_instance_id="spy_ema_paper",
            run_id="run-runtime",
            pid=123,
            process_start_identity="child-1",
            expected_daemon_boot_id="boot-1",
            snapshot_seq=1,
            written_at_ms=now_ms,
            command_loop=CommandLoopBlock(
                heartbeat_at_ms=now_ms,
                state="PAUSED",
            ),
            broker=BrokerBlock(
                identity="PAPER_VERIFIED",
                submission_capability="PAPER_ORDERS_ENABLED",
                effective_posture="PAPER_EXECUTION",
                connection_state="connected",
                connection_epoch=1,
                connected_account="DU123",
                port_class="paper_port",
                observation_at_ms=now_ms,
                probe_completed_at_ms=now_ms,
                reconnect_attempt=0,
            ),
            bar_loop=BarLoopBlock(
                heartbeat_at_ms=now_ms,
                latest_source_bar_ms=now_ms,
                expected_interval_ms=60_000,
            ),
            control_plane=ControlPlaneBlock(
                lease_observed_at_ms=now_ms,
                observed_daemon_boot_id="boot-1",
            ),
        ),
    )
    _set_daemon(
        monkeypatch,
        process={
            "state": "running",
            "run_id": "run-runtime",
            "pid": 123,
            "started_at_ms": now_ms,
        },
    )
    monkeypatch.setattr(live_instances, "_now_ms", lambda: now_ms)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert surface["broker"]["connection"] == "CONNECTED"
    assert "BROKER_CONNECTION_UNKNOWN" not in surface["submit_readiness"]["blocking_reason_codes"]
    broker_proof = next(line for line in surface["trader_guidance"]["proof_lines"] if line["id"] == "broker-proof")
    assert broker_proof["message"] == "Paper broker is connected."
    assert surface["runtime_freshness"]["posture_demoted"] is False
    assert surface["runtime_freshness"]["stale_reason_codes"] == []
    assert surface["runtime_freshness"]["bar_loop"]["state"] == "FRESH"


async def test_real_surface_freshness_threshold_advances_semantic_version(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, root = app_with_root
    sid = "spy_surface_freshness"
    run_id = "run-surface-freshness"
    now_ms = [1_772_463_600_000]
    _write_ledger(root, run_id, sid, 100)
    _write_runtime_snapshot(root, run_id, sid, now_ms[0])
    _set_daemon(
        monkeypatch,
        process={
            "state": "running",
            "run_id": run_id,
            "pid": 123,
            "started_at_ms": now_ms[0],
        },
    )
    monkeypatch.setattr(live_instances, "_now_ms", lambda: now_ms[0])
    from app.services.surface_hub import SurfaceHub

    hub = SurfaceHub(
        strategy_instance_id=sid,
        assemble=lambda: live_instances._assemble_instance_surface(sid),
    )

    fresh = await hub.refresh()
    now_ms[0] += 121_000
    stale = await hub.refresh()

    assert fresh.operator_surface.runtime_freshness is not None
    assert stale.operator_surface.runtime_freshness is not None
    assert fresh.operator_surface.runtime_freshness.broker.state == "FRESH"
    assert stale.operator_surface.runtime_freshness.broker.state != "FRESH"
    assert fresh.surface_version == 1
    assert stale.surface_version == 2


async def test_status_preserves_recovering_runtime_broker_connection(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    now_ms = 1_700_000_000_000
    _write_ledger(root, "run-recovering", "spy_ema_paper", 100)
    _write_runtime_snapshot(
        root,
        "run-recovering",
        "spy_ema_paper",
        now_ms,
        connection_state="recovering",
    )
    _set_daemon(
        monkeypatch,
        process={
            "state": "running",
            "run_id": "run-recovering",
            "pid": 123,
            "started_at_ms": now_ms,
        },
    )
    monkeypatch.setattr(live_instances, "_now_ms", lambda: now_ms)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert surface["broker"]["connection"] == "DEGRADED"
    assert surface["broker"]["connection_condition"]["code"] == "BROKER_RECOVERING"
    assert "BROKER_RECOVERING" in surface["submit_readiness"]["blocking_reason_codes"]
    assert "BROKER_CONNECTION_DISCONNECTED" not in surface["submit_readiness"]["blocking_reason_codes"]
    broker_attention = next(
        group
        for group in surface["trader_guidance"]["additional_attention_groups"]
        if group["code"] == "broker_connection"
    )
    assert broker_attention["headline"] == "Broker recovering streams"


async def test_status_projects_hard_down_runtime_broker_connection_as_disconnected(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    now_ms = 1_700_000_000_000
    _write_ledger(root, "run-hard-down", "spy_ema_paper", 100)
    _write_runtime_snapshot(
        root,
        "run-hard-down",
        "spy_ema_paper",
        now_ms,
        connection_state="hard_down",
    )
    _set_daemon(
        monkeypatch,
        process={
            "state": "running",
            "run_id": "run-hard-down",
            "pid": 123,
            "started_at_ms": now_ms,
        },
    )
    monkeypatch.setattr(live_instances, "_now_ms", lambda: now_ms)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert surface["broker"]["connection"] == "DISCONNECTED"
    assert "BROKER_HARD_DOWN" in surface["submit_readiness"]["blocking_reason_codes"]


async def test_status_uses_account_freeze_artifact_to_block_start_and_resume(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-freeze", "spy_ema_paper", 100, account_id="DU123456")
    write_account_freeze(
        root.parent,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert surface["host_process"]["start_capability"]["disabled_reason_code"] == "ACCOUNT_FROZEN"
    assert surface["host_process"]["start_capability"]["gate_results"][0]["status"] == "freeze"
    assert surface["actions"]["resume"]["disabled_reason_code"] == "ACCOUNT_FROZEN"
    assert surface["actions"]["resume"]["gate_results"][0]["gate_id"] == "account.unresolved_exposure"


async def test_status_projects_unresolved_watchdog_incident_into_recovery_chart(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_watchdog_recovery"
    run_id = "run-watchdog-recovery"
    incident_started_at_ms = 1_700_000_000_000
    _write_ledger(root, run_id, sid, incident_started_at_ms, account_id="DU123")
    incident = watchdog_incident(reason="LEASE_EXPIRED", started_at_ms=incident_started_at_ms)
    IncidentStore(root / run_id).append(incident)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status?refresh=true")

    assert response.status_code == 200
    body = response.json()
    incident_headline = body["operator_surface"]["incident_headline"]
    recovery_node = next(node for node in body["lifecycle_chart"]["global_graph"]["nodes"] if node["id"] == "recovery")
    incident_node = next(
        node for node in body["lifecycle_chart"]["subgraphs"]["recovery"]["nodes"] if node["id"] == "incident"
    )
    receipts = {receipt["label"]: receipt for receipt in incident_node["receipts"]}

    assert incident_headline["code"] == "watchdog.flatten_failed"
    assert incident_headline["occurred_at_ms"] == incident_started_at_ms
    assert recovery_node["status"] == "blocked"
    assert recovery_node["ts_ms"] == incident_started_at_ms
    assert incident_node["status"] == "blocked"
    assert incident_node["ts_ms"] == incident_started_at_ms
    assert receipts["watchdog.outcome"]["value"] == "watchdog.flatten_failed"
    assert receipts["watchdog.tier"]["value"] == "critical"
    assert receipts["watchdog.runbook"]["value"] == "watchdog-halt"
    assert receipts["watchdog.occurred_at_ms"]["value"] == str(incident_started_at_ms)
    assert receipts["watchdog.occurred_at_ms"]["unit"] == "ms UTC"


# ---------------------------------------------------------------------------
# PRD #616 — LiveInstanceSummary extensions + FleetAccountSummary endpoint
# ---------------------------------------------------------------------------


async def test_live_instance_summary_carries_readiness_verdict(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD #616 — the fleet overview row carries readiness_verdict and
    readiness_as_of_ms so the cockpit outer tab can render the badge
    without fetching every instance's full status."""

    app, root = app_with_root
    _write_ledger(root, "run-rdy", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances")

    assert response.status_code == 200
    rows = response.json()
    assert rows, "expected at least one fleet row"
    row = next(r for r in rows if r["strategy_instance_id"] == "spy_ema_paper")
    assert "readiness_verdict" in row
    assert row["readiness_verdict"] in {"READY", "BLOCKED", "DEGRADED", "UNKNOWN"}
    assert "readiness_as_of_ms" in row
    assert row["blockers"], "non-ready fleet rows must carry host-scoped blockers"
    assert row["blockers"][0]["host"] == "fleet_roster"
    assert row["blockers"][0]["condition"]["id"] == "fleet_member_unreachable"
    assert row["blockers"][0]["disposition"] == "fix_elsewhere"
    assert row["blockers"][0]["primary_move"]["action"] == {
        "kind": "navigate",
        "route": "/broker/bots/spy_ema_paper",
        "fragment": None,
    }


async def test_account_summary_endpoint_returns_composed_dto(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD #616 — ``GET /api/live-instances/account-summary`` returns
    the composed FleetAccountSummary (account identity + contamination)."""

    app, root = app_with_root
    _write_ledger(root, "run-fff", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account-summary")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "account_id",
        "account_identity",
        "account_identity_reason_codes",
        "contamination",
    }
    assert body["account_identity"] in {"CONSISTENT", "CONFLICTING", "UNKNOWN"}
    assert isinstance(body["account_identity_reason_codes"], list)
    # ``contamination`` is the existing FleetContamination shape.
    contam = body["contamination"]
    assert contam["verdict"] in {"clean", "contaminated", "unknown"}
    assert isinstance(contam["policy_blocks_starts"], bool)


async def test_legacy_account_endpoint_still_returns_contamination_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD #616 — the legacy ``/account`` endpoint is preserved for
    back-compat callers; the cockpit consumes ``/account-summary``."""

    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] in {"clean", "contaminated", "unknown"}
    assert "policy_blocks_starts" in body


# ---------------------------------------------------------------------------
# PRD #619-C3 — operator_surface.control_plane end-to-end
# ---------------------------------------------------------------------------


async def test_status_control_plane_is_null_when_no_monitor_installed(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In test mode the lifespan hasn't installed a connectivity monitor,
    so ``get_monitor()`` returns ``None`` and the operator surface omits
    the section (renders as ``null``). The cockpit hides the card."""

    app, root = app_with_root
    sid = "strategy-cp-null"
    _write_ledger(root, "run-cp-null", sid, created_at_ms=1_700_000_000_000)
    _set_daemon(monkeypatch, process={"state": "idle", "run_id": None})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert surface["control_plane"] is None


async def test_status_control_plane_reflects_monitor_state(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the lifespan installs a monitor, the operator surface carries
    its current connectivity state — kind, attempt, last_*_ms, daemon
    boot id, server-authored notice + runbook_slug — verbatim."""

    from app.engine.live.daemon_connectivity_monitor import (
        DaemonConnectivityState,
        set_monitor,
    )

    app, root = app_with_root
    sid = "strategy-cp-state"
    _write_ledger(root, "run-cp-state", sid, created_at_ms=1_700_000_000_000)
    _set_daemon(monkeypatch, process={"state": "idle", "run_id": None})

    fake_state = DaemonConnectivityState(
        kind="UNREACHABLE",
        attempt=5,
        last_transition_ms=1_700_000_000_500,
        last_success_ms=1_699_999_999_000,
        observed_daemon_boot_id="boot-deadbeef",
        last_detail="connect refused",
        last_error_category="connect_error",
        next_probe_in_ms=5_000,
    )

    class _StubMonitor:
        @property
        def state(self):
            return fake_state

    set_monitor(_StubMonitor())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/live-instances/{sid}/status?refresh=true")
    finally:
        set_monitor(None)

    assert response.status_code == 200
    cp = response.json()["operator_surface"]["control_plane"]
    assert cp is not None
    assert cp["state"] == "UNREACHABLE"
    assert cp["attempt"] == 5
    assert cp["last_transition_ms"] == 1_700_000_000_500
    assert cp["last_success_ms"] == 1_699_999_999_000
    assert cp["daemon_boot_id"] == "boot-deadbeef"
    assert cp["notice"]  # backend-authored, non-empty
    assert cp["runbook_slug"] == "daemon-unreachable"


async def test_status_control_plane_connected_omits_notice(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy CONNECTED state must NOT surface a notice or runbook —
    the cockpit hides the incident card."""

    from app.engine.live.daemon_connectivity_monitor import (
        DaemonConnectivityState,
        set_monitor,
    )

    app, root = app_with_root
    sid = "strategy-cp-connected"
    _write_ledger(root, "run-cp-connected", sid, created_at_ms=1_700_000_000_000)
    _set_daemon(monkeypatch, process={"state": "idle", "run_id": None})

    fake_state = DaemonConnectivityState(
        kind="CONNECTED",
        attempt=0,
        last_transition_ms=1_700_000_000_000,
        last_success_ms=1_700_000_000_000,
        observed_daemon_boot_id="boot-A",
    )

    class _StubMonitor:
        @property
        def state(self):
            return fake_state

    set_monitor(_StubMonitor())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/live-instances/{sid}/status?refresh=true")
    finally:
        set_monitor(None)

    assert response.status_code == 200
    cp = response.json()["operator_surface"]["control_plane"]
    assert cp is not None
    assert cp["state"] == "CONNECTED"
    assert cp["notice"] is None
    assert cp["runbook_slug"] is None
    assert cp["daemon_boot_id"] == "boot-A"


# ---------------------------------------------------------------------------
# PRD #619-D4 — broker_observation_consistency
# ---------------------------------------------------------------------------


async def test_status_broker_observation_consistency_is_null_without_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a live binding there is nothing to compare; the cockpit
    hides the card."""

    app, root = app_with_root
    sid = "strategy-no-binding"
    _write_ledger(root, "run-x", sid, created_at_ms=1_700_000_000_000)
    _set_daemon(monkeypatch, process={"state": "idle", "run_id": None})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status?refresh=true")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]
    assert surface["broker_observation_consistency"] is None


async def test_status_broker_observation_consistency_unknown_without_engine_runtime(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a live binding but no engine_runtime artifact yet, the verdict
    reads UNKNOWN with CHILD_OBSERVATION_MISSING — the cockpit shows the
    card so the operator knows the comparison is pending, not absent."""

    app, root = app_with_root
    sid = "strategy-runtime-missing"
    _write_ledger(root, "run-rm", sid, created_at_ms=1_700_000_000_000)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-rm", "pid": 1, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status?refresh=true")

    assert response.status_code == 200
    consistency = response.json()["operator_surface"]["broker_observation_consistency"]
    assert consistency is not None
    assert consistency["verdict"] == "UNKNOWN"
    assert "CHILD_OBSERVATION_MISSING" in consistency["reason_codes"]
    assert consistency["compared_at_ms"] > 0

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
from app.routers import live_instances


def _write_ledger(root: Path, run_id: str, sid: str, created_at_ms: int, spec_path: Path | None = None) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    payload: dict = {"run_id": run_id, "strategy_instance_id": sid, "created_at_ms": created_at_ms}
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
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.main import app

    return app, root


def _set_daemon(monkeypatch: pytest.MonkeyPatch, *, process: dict | None = None) -> None:
    async def fake_instances(_base_url: str) -> dict | None:
        return None

    async def fake_process(_base_url: str, _sid: str) -> dict | None:
        return process

    monkeypatch.setattr(host_daemon_client, "fetch_instances", fake_instances)
    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", fake_process)


# ---------------------------------------------------------------------------
# Cycle 1 — tracer bullet: operator_surface field with schema_version: 1
# ---------------------------------------------------------------------------


async def test_status_response_includes_operator_surface_schema_version_one(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracer bullet: the new projection field appears on every status
    response (running OR dead instance) and carries ``schema_version: 1``.

    All other fields of the projection are exercised by cumulative cycles
    below.  This first test just asserts the field is *present* and pinned
    so that downstream slices can rely on its existence.
    """

    app, root = app_with_root
    _write_ledger(root, "run-aaa", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert "operator_surface" in body, "Slice 1 contract: operator_surface field missing"
    assert body["operator_surface"]["schema_version"] == 1


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
        response = await client.get("/api/live-instances/spy_ema_paper/status")

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
        response = await client.get("/api/live-instances/spy_ema_paper/status")

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
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    surface = response.json()["operator_surface"]

    assert set(surface) == {
        "schema_version",
        "host_process",
        "prior_run",
        "broker",
        "configuration",
        "current_risk",
        "daily_order_cap",
        "action_plan",
        "actions",
        "trading_session",
        # PRD #616 — additive operator-facing projections.
        "readiness_gates",
    }
    assert surface["schema_version"] == 1
    assert surface["host_process"]["state"] == "RUNNING"
    assert surface["prior_run"]["classification"] == "UNKNOWN"
    # Two independent enums now.
    assert surface["broker"]["safety_verdict"] in {"PAPER_ONLY", "UNSAFE", "UNKNOWN"}
    assert surface["broker"]["connection"] in {"CONNECTED", "DISCONNECTED", "UNKNOWN"}
    assert surface["configuration"]["verdict"] in {"READY", "ATTENTION", "UNKNOWN"}
    assert surface["current_risk"]["verdict"] in {"READY", "ATTENTION", "UNKNOWN"}
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
        }
        assert cap["effect"] in {"DURABLE_ONLY", "LIVE_ACTUATION"}
        if cap["enabled"]:
            assert cap["disabled_reason_code"] is None
            assert cap["disabled_reasons"] == []
        else:
            assert isinstance(cap["disabled_reasons"], list)
            assert cap["disabled_reasons"]
    # readiness_gates projection is always present (even empty).
    assert isinstance(surface["readiness_gates"], list)


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

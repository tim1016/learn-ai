"""Contract tests for the instance-addressed operator console API (ADR 0004).

The host daemon is faked at the client seam (no network); liveness is resolved
server-side and the serialized response carries both `live_binding` and
`evidence_binding` so the client cannot confuse them.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.routers import live_instances


def _write_ledger(
    root: Path, run_id: str, sid: str, created_at_ms: int, spec_path: Path | None = None
) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    payload: dict = {"run_id": run_id, "strategy_instance_id": sid, "created_at_ms": created_at_ms}
    if spec_path is not None:
        payload["strategy_spec_path"] = str(spec_path)
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_live_state(root: Path, sid: str, run_id: str, positions: dict[str, int]) -> None:
    live_state_dir = root.parent / "live_state" / sid
    live_state_dir.mkdir(parents=True, exist_ok=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": sid,
                "run_id": run_id,
                "bot_order_namespace": f"{sid}_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": positions,
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        fleet_dirty_blocks_starts=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.main import app

    return app, root


def _set_daemon(
    monkeypatch: pytest.MonkeyPatch, *, instances: dict | None = None, process: dict | None = None
) -> None:
    async def fake_instances(_base_url: str) -> dict | None:
        return instances

    async def fake_process(_base_url: str, _sid: str) -> dict | None:
        return process

    monkeypatch.setattr(host_daemon_client, "fetch_instances", fake_instances)
    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", fake_process)


async def test_instance_status_running_exposes_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-aaa", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-live-aaa", "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "running"
    assert body["process"]["bound_run_id"] == "run-live-aaa"
    assert body["live_binding"]["run_id"] == "run-live-aaa"
    assert body["live_binding"]["source"] == "registry"
    assert body["evidence_binding"]["run_id"] == "run-live-aaa"
    assert body["evidence_binding"]["is_live"] is False
    assert body["desired_state"] is not None


async def test_instance_status_dead_is_evidence_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-old-bbb", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "idle"
    assert body["live_binding"] is None
    assert body["evidence_binding"]["run_id"] == "run-old-bbb"


async def test_instance_status_unreachable_daemon_is_not_guessed(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-x", "spy_ema_paper", 10)
    _set_daemon(monkeypatch, process=None)  # daemon unreachable -> None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "unreachable"
    assert body["live_binding"] is None
    assert body["evidence_binding"]["run_id"] == "run-x"


async def test_list_instances_merges_daemon_and_disk(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema-1", "spy_ema_paper", 100)
    _write_ledger(root, "run-vwap-1", "spy_vwap_shadow", 100)
    _set_daemon(
        monkeypatch,
        instances={
            "instances": [
                {
                    "strategy_instance_id": "spy_ema_paper",
                    "run_id": "run-ema-1",
                    "run_dir": str(root / "run-ema-1"),
                    "process": {"state": "running", "run_id": "run-ema-1"},
                }
            ],
            "fetched_at_ms": 1,
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances")

    assert response.status_code == 200
    rows = {row["strategy_instance_id"]: row for row in response.json()}
    assert set(rows) == {"spy_ema_paper", "spy_vwap_shadow"}
    assert rows["spy_ema_paper"]["process_state"] == "running"
    assert rows["spy_ema_paper"]["bound_run_id"] == "run-ema-1"
    # Disk-only instance: daemon reachable but not managing it -> offline, no bound run.
    assert rows["spy_vwap_shadow"]["process_state"] == "offline"
    assert rows["spy_vwap_shadow"]["bound_run_id"] is None
    assert rows["spy_vwap_shadow"]["latest_run_id"] == "run-vwap-1"


async def test_instance_status_rejects_invalid_id(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/evil$/status")

    assert response.status_code == 400


async def test_status_includes_namespace_attributed_broker_slice(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-brk", "spy_ema_paper", 100)
    live_state_dir = root.parent / "live_state" / "spy_ema_paper"
    live_state_dir.mkdir(parents=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": "spy_ema_paper",
                "run_id": "run-brk",
                "bot_order_namespace": "spy_ema_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": {"SPY": 100},
                "pending_intents": [{"symbol": "SPY"}],
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    broker = response.json()["broker"]
    assert broker["bot_order_namespace"] == "spy_ema_ns"
    assert broker["owned_positions"] == {"SPY": 100}  # engine's own namespace tally
    assert broker["pending_order_count"] == 1


async def test_status_broker_absent_without_sidecar(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-nobrk", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.json()["broker"] is None


async def test_account_fleet_flags_residual_contamination(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema", "spy_ema", 100)
    _write_live_state(root, "spy_ema", "run-ema", {"SPY": 100})

    async def fake_net() -> dict[str, int]:
        return {"SPY": 137}  # 37 unexplained

    monkeypatch.setattr(live_instances, "_fetch_net_positions", fake_net)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    body = response.json()
    assert body["verdict"] == "contaminated"
    assert body["residual"] == {"SPY": 37}
    assert body["explained_total"] == {"SPY": 100}
    assert any(b["strategy_instance_id"] == "spy_ema" for b in body["explained_by_instance"])


async def test_account_fleet_unknown_without_broker(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema", "spy_ema", 100)
    _write_live_state(root, "spy_ema", "run-ema", {"SPY": 100})

    async def fake_net() -> None:
        return None

    monkeypatch.setattr(live_instances, "_fetch_net_positions", fake_net)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    assert response.json()["verdict"] == "unknown"


async def test_instance_commands_returns_bound_run_timeline(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-cmd", "spy_ema_paper", 100)
    commands = root / "run-cmd" / "commands"
    commands.mkdir()
    (commands / "command.1.RECONCILE.pending.json").write_text(
        json.dumps({"seq": 1, "verb": "RECONCILE", "payload": {}}), encoding="utf-8"
    )
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-cmd", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/commands")

    body = response.json()
    assert body["poll_interval_ms"] == 1000  # server-provided
    assert "pending" not in body and "acks" not in body  # canonical entries[] shape
    assert [e["seq"] for e in body["entries"]] == [1]
    assert body["entries"][0]["status"] == "queued"


async def test_instance_commands_empty_without_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/commands")

    assert response.json() == {"entries": [], "poll_interval_ms": 1000}


async def test_issue_one_shot_command_queues_on_bound_run(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-os", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-os", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands", json={"verb": "RECONCILE"}
        )

    assert response.status_code == 200
    assert response.json()["verb"] == "RECONCILE"
    queued = list((root / "run-os" / "commands").glob("command.*.RECONCILE.pending.json"))
    assert len(queued) == 1


async def test_issue_command_rejects_intent_verbs(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-os2", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-os2", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands", json={"verb": "PAUSE"}
        )

    assert response.status_code == 400  # PAUSE is the intent knob, not a one-shot command


async def test_issue_command_without_live_binding_conflicts(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/commands", json={"verb": "FLATTEN"}
        )

    assert response.status_code == 409


async def test_status_transports_engine_readiness_when_live(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-rdy", "spy_ema_paper", 100)
    (root / "run-live-rdy" / "readiness.json").write_text(
        json.dumps(
            {
                "kind": "live_readiness",
                "as_of_ms": 5,
                "source": "engine",
                "verdict": "READY",
                "summary": "ready",
                "gates": [{"name": "desired_state", "status": "pass", "severity": "hard", "detail": "RUNNING"}],
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-rdy", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    readiness = response.json()["readiness"]
    assert readiness["kind"] == "live_readiness"
    assert readiness["source"] == "engine"  # engine-authored, transported verbatim
    assert readiness["verdict"] == "READY"


async def test_status_derives_start_readiness_when_dead(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-dead-rdy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    readiness = response.json()["readiness"]
    assert readiness["kind"] == "start_readiness"
    assert readiness["source"] == "backend_derived"
    assert readiness["live_readiness_available"] is False


async def test_status_includes_spec_derived_decision_column_descriptors(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.engine.strategy.spec import schema as spec_schema

    fixture = Path(spec_schema.__file__).parent / "fixtures" / "spy_ema_crossover.spec.json"
    app, root = app_with_root
    _write_ledger(root, "run-desc", "spy_ema_paper", 100, spec_path=fixture)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    body = response.json()
    cols = {c["name"]: c for c in body["decision_columns"]}
    assert {"ema5", "ema10", "rsi"} <= set(cols)
    assert cols["rsi"]["label"] == "RSI"
    assert cols["ema5"]["label"] == "EMA 5"
    assert cols["ema5"]["format"] == "decimal"
    # No decisions.parquet written -> latest_decision is None, descriptors still resolve.
    assert body["latest_decision"] is None


async def test_set_desired_state_actuates_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-ccc", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-ccc", "pid": 7})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause", "updated_by": "operator", "reason": "risk"},
        )

    assert response.status_code == 200
    body = response.json()
    # 1. durable intent written first
    assert body["durable"]["state"] == "PAUSED"
    # 2. live actuation queued on the bound run
    assert body["actuation"]["actuated"] is True
    assert body["actuation"]["run_id"] == "run-live-ccc"
    assert body["actuation"]["command_seq"] is not None
    queued = list((root / "run-live-ccc" / "commands").glob("command.*.PAUSE.pending.json"))
    assert len(queued) == 1


async def test_set_desired_state_without_live_binding_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)  # no live process

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "stop", "updated_by": "op"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "STOPPED"
    assert body["actuation"]["actuated"] is False
    assert "durable only" in body["actuation"]["detail"]


async def test_set_desired_state_live_but_run_dir_not_visible_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon reports a live process but its run dir is not visible under this
    service's root: never claim a phantom actuation (a command written here
    would never be seen by the engine polling its real dir)."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-ghost", "pid": 5})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "PAUSED"
    assert body["actuation"]["actuated"] is False
    assert "not visible locally" in body["actuation"]["detail"]


async def test_set_desired_state_enqueue_failure_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-ddd", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-ddd", "pid": 8})

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(live_instances.CommandChannel, "write_from_operator", fail_write)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "PAUSED"
    assert body["actuation"]["actuated"] is False
    assert body["actuation"]["run_id"] == "run-live-ddd"
    assert "failed to enqueue live command" in body["actuation"]["detail"]


# ── deploy / create (ADR 0006) ───────────────────────────────────────


def _deploy_body() -> dict:
    return {
        "strategy_spec_path": "PythonDataService/spec.json",
        "qc_audit_copy_path": "references/qc-shadow/A.py",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1700000000000,
        "strategy_instance_id": "spy_ema_paper",
        "live_config": {"symbol": "SPY"},
    }


async def test_deploy_instance_created_returns_201(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 201
    body = response.json()
    assert body["run_id"] == "run-new"
    assert body["created"] is True


async def test_deploy_instance_idempotent_returns_200(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"run_id": "run-existing", "run_dir": "/runs/run-existing", "created": False, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 200
    assert response.json()["created"] is False


async def test_deploy_instance_dirty_tree_propagates_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(409, "Working tree is dirty; commit or stash before deploying.")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    assert "dirty" in response.json()["detail"].lower()


async def test_deploy_instance_daemon_unreachable_propagates_503(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(503, "host daemon unreachable: connection refused")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 503


async def test_deploy_instance_invalid_payload_returns_502(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-invalid deploy payload from the daemon is an upstream contract
    failure → 502, not a 500 that makes the data plane look broken."""
    app, _ = app_with_root

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"unexpected": "shape"}  # missing run_id/run_dir/created

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 502


async def test_qc_audit_copies_invalid_payload_returns_502(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed (non-None) listing from the daemon must not 500 or silently
    read as an empty list — surface it as a gateway error."""
    app, _ = app_with_root

    async def fake_fetch(_base_url: str) -> dict | None:
        return {"scope_root": 123, "entries": "not-a-list"}  # wrong types

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 502


async def test_qc_audit_copies_passthrough(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_fetch(_base_url: str) -> dict | None:
        return {"scope_root": "references/qc-shadow", "entries": ["references/qc-shadow/A.py"]}

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == ["references/qc-shadow/A.py"]


async def test_qc_audit_copies_failclosed_to_empty(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_fetch(_base_url: str) -> dict | None:
        return None  # daemon unreachable

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == []

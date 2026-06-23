"""PRD #619-D3 — integration tests for the Reconcile endpoint.

The pure ``reconcile_mutation_effect`` classifier is exercised in
``tests/services/test_mutation_attempt.py``.  These tests cover the
*router*'s contract: evidence assembly from the daemon /
desired-state / engine_runtime / broker reads, the durable
transition, the typed response shape, and the 404 / 409 boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.routers import live_instances
from app.services.mutation_attempt import MutationAttempt, MutationAttemptRepo
from tests._fixtures.daemon_transport import as_typed_get

_NOW_MS = 1_700_000_002_000
_REQUESTED_AT_MS = 1_700_000_001_000


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    monkeypatch.setattr(live_instances, "_now_ms", lambda: _NOW_MS)
    from app.main import app

    return app, root


def _write_attempt(root: Path, attempt: MutationAttempt) -> None:
    repo_root = root.parent / "mutation_attempts"
    MutationAttemptRepo(repo_root).write(attempt)


def _stub_daemon_process(monkeypatch: pytest.MonkeyPatch, process: dict | None) -> None:
    async def _fake(_base_url: str, _sid: str):
        return as_typed_get(process)

    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", _fake)


def _attempt(
    *,
    action: str = "stop",
    state: str = "OUTCOME_UNKNOWN",
) -> MutationAttempt:
    return MutationAttempt(
        mutation_attempt_id="att-1",
        instance_id="spy_ema_paper",
        run_id=None,
        action=action,  # type: ignore[arg-type]
        requested_at_ms=_REQUESTED_AT_MS,
        last_transition_at_ms=_REQUESTED_AT_MS,
        dispatch_state=state,  # type: ignore[arg-type]
    )


async def test_reconcile_returns_404_when_no_attempt_exists(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _stub_daemon_process(monkeypatch, {"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 404


async def test_reconcile_returns_409_when_attempt_in_flight(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(state="DISPATCHING"))
    _stub_daemon_process(monkeypatch, {"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 409
    assert response.json()["detail"]["dispatch_state"] == "DISPATCHING"


async def test_reconcile_returns_409_when_attempt_already_terminal(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(state="EFFECT_CONFIRMED"))
    _stub_daemon_process(monkeypatch, {"state": "exited"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 409
    assert response.json()["detail"]["dispatch_state"] == "EFFECT_CONFIRMED"


async def test_reconcile_classifies_stop_as_effect_confirmed_when_exited(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(action="stop"))
    _stub_daemon_process(monkeypatch, {"state": "exited"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mutation_attempt_id"] == "att-1"
    assert body["action"] == "stop"
    assert body["outcome"] == "EFFECT_CONFIRMED"
    assert body["dispatch_state"] == "EFFECT_CONFIRMED"
    assert body["reconciled_at_ms"] == _NOW_MS
    assert body["evidence"]["process_state"] == "exited"
    assert body["evidence"]["daemon_reachable"] is True


async def test_reconcile_classifies_stop_as_not_observed_when_running(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(action="stop"))
    _stub_daemon_process(
        monkeypatch,
        {"state": "running", "run_id": "run-1", "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "EFFECT_NOT_OBSERVED"
    assert body["dispatch_state"] == "EFFECT_NOT_OBSERVED"


async def test_reconcile_classifies_as_not_provable_when_daemon_unreachable(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(action="stop"))

    # Simulate UNREACHABLE: typed-get returns the daemon dict as None and the
    # underlying DaemonResult.kind != CONNECTED.
    from app.engine.live.daemon_transport import DaemonResult

    async def _fake(_base_url: str, _sid: str):
        result = DaemonResult(
            kind="UNREACHABLE",
            detail="connection refused",
            error_category="network_error",
            outcome_ambiguous=False,
        )
        return result, None

    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", _fake)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "NOT_PROVABLE"
    assert body["dispatch_state"] == "NOT_PROVABLE"
    assert body["evidence"]["daemon_reachable"] is False


async def test_reconcile_classifies_flatten_with_no_positions(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(action="flatten"))
    _stub_daemon_process(monkeypatch, {"state": "running", "run_id": "run-1"})
    # Wire an empty broker view via the live_state sidecar — broker
    # is derived from that path in instance_broker.
    live_state_dir = root.parent / "live_state" / "spy_ema_paper"
    live_state_dir.mkdir(parents=True, exist_ok=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": "spy_ema_paper",
                "run_id": "run-1",
                "bot_order_namespace": "spy_ema_paper_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": {"SPY": 0},
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/reconcile-mutation"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "EFFECT_CONFIRMED"
    assert body["evidence"]["broker_owned_positions_empty"] is True


async def test_reconcile_persists_advanced_attempt(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_attempt(root, _attempt(action="stop"))
    _stub_daemon_process(monkeypatch, {"state": "exited"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/live-instances/spy_ema_paper/reconcile-mutation")

    repo = MutationAttemptRepo(root.parent / "mutation_attempts")
    persisted = repo.read("att-1")
    assert persisted is not None
    assert persisted.dispatch_state == "EFFECT_CONFIRMED"
    assert persisted.last_transition_at_ms == _NOW_MS
    assert persisted.evidence is not None
    assert persisted.evidence["process_state"] == "exited"

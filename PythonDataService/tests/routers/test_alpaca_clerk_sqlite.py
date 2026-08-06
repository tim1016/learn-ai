"""HTTP-level tests for the SQLite Alpaca Clerk command endpoints (#1376)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.journal import get_clerk_settings, reset_clerk_settings_for_testing
from app.broker.alpaca.clerk.sqlite import process_repositories
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.routers import alpaca_clerk_sqlite
from app.routers.alpaca_clerk_sqlite import router

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_clerk_settings_for_testing()
    assert get_clerk_settings().dir == tmp_path

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    # Close the setup handle so the cached process repository (opened lazily
    # by the router on first request) is the sole open connection+lease.
    repo.close()

    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        process_repositories.reset_for_testing()
        reset_clerk_settings_for_testing()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_start_then_get_returns_the_command_resource(api: FastAPI) -> None:
    async with _client(api) as client:
        start = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert start.status_code == 202
        body = start.json()
        assert body["state"] == "succeeded"
        assert body["action"] == "START"
        assert body["disabled_tooltip"] is None  # terminal — nothing to disable for

        get = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/commands/{body['command_id']}"
        )
        assert get.status_code == 200
        assert get.json() == body


@pytest.mark.asyncio
async def test_start_transport_retry_is_idempotent_over_http(api: FastAPI) -> None:
    async with _client(api) as client:
        first = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        second = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["command_id"] == second.json()["command_id"]


@pytest.mark.asyncio
async def test_start_conflict_returns_typed_409(api: FastAPI) -> None:
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        conflict = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1", "operator_reason": "changes the hash"},
        )
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert detail["reason"] == "durable_conflict"
        assert detail["existing_command"]["action"] == "START"


@pytest.mark.asyncio
async def test_start_with_colon_in_lifecycle_run_id_returns_typed_400(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "a:b"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_identity"


@pytest.mark.asyncio
async def test_start_with_empty_lifecycle_run_id_returns_422(api: FastAPI) -> None:
    """reject_colon() only blocks ':' — an empty string would otherwise mint
    a durable command identity no client could reproduce intentionally.
    Enforced at the Pydantic boundary, not the domain layer."""
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": ""},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_stop_without_active_run_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["reason"] == "no_active_run"


@pytest.mark.asyncio
async def test_stop_after_start_succeeds(api: FastAPI) -> None:
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        stop = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        assert stop.status_code == 202
        assert stop.json()["state"] == "succeeded"
        assert stop.json()["action"] == "STOP"


@pytest.mark.asyncio
async def test_stop_retry_after_run_stopped_replays_the_completed_result_over_http(
    api: FastAPI,
) -> None:
    """The HTTP-level proof of the corrective foundation slice's central
    fix: a lost-response retry of Stop, keyed by the caller-stable
    ``lifecycle_run_id``, returns the original completed command."""
    async with _client(api) as client:
        await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        first = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        retry = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop",
            json={"lifecycle_run_id": "run-1"},
        )
        assert first.status_code == 202
        assert retry.status_code == 202
        assert first.json()["command_id"] == retry.json()["command_id"]


@pytest.mark.asyncio
async def test_start_on_unknown_bot_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/never-registered/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["reason"] == "unknown_strategy_instance"


@pytest.mark.asyncio
async def test_get_unknown_command_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/commands/cmd:does-not-exist"
        )
        assert response.status_code == 404
        assert response.json()["detail"]["reason"] == "command_not_found"


@pytest.mark.asyncio
async def test_uninitialized_account_returns_typed_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_clerk_settings_for_testing()
    app = FastAPI()
    app.include_router(router)
    try:
        async with _client(app) as client:
            response = await client.get(
                "/api/alpaca-clerk-sqlite/accounts/PA-NEVER-INITIALIZED/commands/cmd:x"
            )
        assert response.status_code == 503
        assert response.json()["detail"]["reason"] == "sqlite_authority_not_initialized"
    finally:
        process_repositories.reset_for_testing()
        reset_clerk_settings_for_testing()


@pytest.mark.asyncio
async def test_blocked_repository_call_does_not_stall_an_unrelated_request(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope E: repository calls are dispatched via ``asyncio.to_thread``, so
    a slow Start does not block a concurrent, unrelated GET on the same
    event loop (open-pr-review-2026-08-05.md P2 "Synchronous SQLite/fsync
    blocks the FastAPI event loop")."""
    real_submit_start_run = alpaca_clerk_sqlite.submit_start_run
    entered = threading.Event()

    def slow_submit_start_run(*args, **kwargs):
        entered.set()
        time.sleep(0.2)
        return real_submit_start_run(*args, **kwargs)

    monkeypatch.setattr(alpaca_clerk_sqlite, "submit_start_run", slow_submit_start_run)

    async with _client(api) as client:
        start = asyncio.create_task(
            client.post(
                f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
                json={"lifecycle_run_id": "run-1"},
            )
        )
        # Wait until the worker thread is actually inside the blocking sleep,
        # rather than guessing a fixed handoff delay (flakes under load).
        while not entered.is_set():
            await asyncio.sleep(0.005)
        began = time.monotonic()
        unrelated = await client.get(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/commands/cmd:does-not-exist"
        )
        unrelated_elapsed = time.monotonic() - began

        assert unrelated.status_code == 404
        assert unrelated_elapsed < 0.18  # completed before the slow call's 0.2s sleep ended
        assert (await start).status_code == 202


@pytest.mark.asyncio
async def test_lease_lost_returns_typed_503(api: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseLost

    def raise_lease_lost(*_args, **_kwargs):
        raise ExecutionLeaseLost("simulated lease loss")

    monkeypatch.setattr(alpaca_clerk_sqlite, "submit_start_run", raise_lease_lost)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/start",
            json={"lifecycle_run_id": "run-1"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["reason"] == "sqlite_authority_unavailable"


@pytest.mark.asyncio
async def test_reconcile_now_runs_operator_pass(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeAlpacaPort:
        broker_id = "alpaca"

        async def submit(self, leg, *, client_order_id):  # pragma: no cover - protocol seam
            raise AssertionError("not called")

        async def cancel(self, order_id):  # pragma: no cover - protocol seam
            raise AssertionError("not called")

        async def get_order_by_client_order_id(self, client_order_id):
            return None

    class FakeRegistry:
        def resolve(self, broker_id: str) -> FakeAlpacaPort:
            assert broker_id == "alpaca"
            return FakeAlpacaPort()

    observed: dict[str, object] = {}

    async def fake_reconcile(repo, *, read, trade, trigger):
        observed.update(repo=repo, read=read, trade=trade, trigger=trigger)
        return AccountReconciliationResult(
            verdict="position_drift",
            resolved_count=2,
            drifted_symbols=("SPY",),
        )

    monkeypatch.setattr(alpaca_clerk_sqlite, "get_broker_registry", lambda: FakeRegistry())
    monkeypatch.setattr(alpaca_clerk_sqlite, "reconcile_account", fake_reconcile)

    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/reconcile"
        )

    assert response.status_code == 200
    assert response.json() == {
        "verdict": "position_drift",
        "resolved_count": 2,
        "foreign_order_count": 0,
        "drifted_symbols": ["SPY"],
    }
    assert observed["trigger"] == "OPERATOR_RECONCILE_NOW"
    assert observed["read"] is observed["trade"]

"""HTTP-level tests for the SQLite Alpaca Clerk command endpoints (#1376)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.journal import get_clerk_settings, reset_clerk_settings_for_testing
from app.broker.alpaca.clerk.sqlite import process_repositories
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
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
async def test_stop_without_active_run_returns_typed_404(api: FastAPI) -> None:
    async with _client(api) as client:
        response = await client.post(
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop", json={}
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
            f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT_ID}/bots/{SID}/runs/stop", json={}
        )
        assert stop.status_code == 202
        assert stop.json()["state"] == "succeeded"
        assert stop.json()["action"] == "STOP"


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

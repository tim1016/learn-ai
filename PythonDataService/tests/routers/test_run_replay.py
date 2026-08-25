"""Endpoint tests for the per-run replay receipt (transport only)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.run_replay import router
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from tests.services.test_run_replay_receipt_store import _receipt  # the pending-receipt factory


class _FakeReadPort:
    broker_id = "alpaca"

    def capabilities(self) -> BrokerCapabilities:  # pragma: no cover - registry shape only
        raise NotImplementedError


@pytest.fixture
def api(tmp_path: Path):
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)
    set_bot_task_registry(registry)
    app = FastAPI()
    app.include_router(router)
    yield app, registry
    set_bot_task_registry(None)
    reset_broker_registry_for_testing()


@pytest.mark.asyncio
async def test_get_replay_receipt_absent_is_404(api) -> None:
    app, _registry = api
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/brokers/alpaca/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_replay_receipt_returns_the_persisted_receipt(api) -> None:
    app, registry = api
    receipt = _receipt(status="parity")
    from app.services.run_replay_proof import write_run_replay_receipt

    write_run_replay_receipt(
        registry._replay_proof.instance_dir_for("bot-a"), receipt
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/brokers/alpaca/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "parity"
    assert body["run_id"] == "run-1"
    assert body["generated_at_ms"] == 1_700_000_000_000  # int64 ms UTC on the wire


@pytest.mark.asyncio
async def test_get_replay_receipt_unknown_broker_is_404(api) -> None:
    app, _registry = api
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/brokers/ibkr/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_replay_receipt_without_launch_evidence_is_404(api) -> None:
    app, _registry = api
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/api/brokers/alpaca/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 404

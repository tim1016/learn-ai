"""Transport tests for the dev-only fault-injection arm endpoint (PRD #1354).

The router is flag-gated at import time in ``main.py`` (404 when the seam is
off), so these tests mount it on a fresh app and exercise its own behavior:
arm / disarm / status, refusal translation, and input validation. The seam's
core logic is covered by test_fault_injection.py; the write/frame hooks by
test_client.py / test_trade_updates.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca import fault_injection as fi
from app.routers.alpaca_fault_injection import router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _reset() -> None:
    fi.reset_fault_injection_for_testing()
    yield
    fi.reset_fault_injection_for_testing()


@pytest.fixture
def permit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fi, "get_alpaca_settings", lambda: SimpleNamespace(is_paper=True))


async def test_arm_is_refused_when_not_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", False)
    async with await _client(_app()) as client:
        response = await client.post("/api/brokers/alpaca/fault-injection/arm", json={"kind": "reject_422"})

    assert response.status_code == 403
    assert "not permitted" in response.json()["detail"]


async def test_arm_when_permitted_returns_status(permit: None) -> None:
    async with await _client(_app()) as client:
        response = await client.post(
            "/api/brokers/alpaca/fault-injection/arm",
            json={"kind": "conflict_409", "target": "bot-a", "count": 2},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["permitted"] is True
    assert body["write"] == [
        {"kind": "conflict_409", "target": "bot-a", "remaining": 2, "params": {}}
    ]
    assert body["frame"] == []


async def test_arm_unknown_kind_is_422(permit: None) -> None:
    async with await _client(_app()) as client:
        response = await client.post(
            "/api/brokers/alpaca/fault-injection/arm", json={"kind": "not_a_kind"}
        )

    assert response.status_code == 422


async def test_arm_non_positive_count_is_422(permit: None) -> None:
    async with await _client(_app()) as client:
        response = await client.post(
            "/api/brokers/alpaca/fault-injection/arm", json={"kind": "timeout", "count": 0}
        )

    assert response.status_code == 422


async def test_arm_frame_fault_accepts_params(permit: None) -> None:
    async with await _client(_app()) as client:
        response = await client.post(
            "/api/brokers/alpaca/fault-injection/arm",
            json={"kind": "partial_fill", "target": "coid", "params": {"qty": "3"}},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["frame"] == [
        {"kind": "partial_fill", "target": "coid", "remaining": 1, "params": {"qty": "3"}}
    ]


async def test_disarm_clears_all(permit: None) -> None:
    app = _app()
    async with await _client(app) as client:
        await client.post("/api/brokers/alpaca/fault-injection/arm", json={"kind": "reject_422"})
        await client.post("/api/brokers/alpaca/fault-injection/arm", json={"kind": "halt_stopped"})
        response = await client.post("/api/brokers/alpaca/fault-injection/disarm")

    assert response.status_code == 200
    assert response.json() == {"cleared": 2}


async def test_status_reports_permitted_and_armed(permit: None) -> None:
    app = _app()
    async with await _client(app) as client:
        await client.post(
            "/api/brokers/alpaca/fault-injection/arm",
            json={"kind": "throttle_429", "count": 3},
        )
        response = await client.get("/api/brokers/alpaca/fault-injection/status")

    body = response.json()
    assert body["permitted"] is True
    assert body["write"][0]["kind"] == "throttle_429"
    assert body["write"][0]["remaining"] == 3


async def test_status_reports_not_permitted_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", False)
    async with await _client(_app()) as client:
        response = await client.get("/api/brokers/alpaca/fault-injection/status")

    assert response.status_code == 200
    assert response.json() == {"permitted": False, "write": [], "frame": []}

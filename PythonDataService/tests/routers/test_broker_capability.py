from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import broker_capability
from app.schemas.broker_capability import SessionCapability, SessionDataCapability
from app.services.broker_capability_service import (
    BrokerCapabilityService,
    get_broker_capability_service,
)


def _snapshot(symbol: str = "SPY") -> SessionDataCapability:
    session = SessionCapability(
        window_today_open_ms=1,
        window_today_close_ms=2,
        data="live",
        tradeable="yes",
        order_eligible_outside_rth=True,
        evidence_codes=[],
    )
    return SessionDataCapability(
        symbol=symbol,
        con_id=123,
        account_mode="live",
        account_id="U1234567",
        probed_at_ms=1_783_000_000_000,
        time_zone_id="America/New_York",
        sessions={
            "RTH": session,
            "PRE": session,
            "POST": session,
            "OVERNIGHT": session,
        },
        raw_evidence=[],
    )


class _FakeCapabilityService:
    def __init__(self) -> None:
        self.probed_symbols: list[str] = []
        self.snapshots = [_snapshot()]

    async def probe(self, _client: object, *, symbols: list[str]) -> list[SessionDataCapability]:
        self.probed_symbols = symbols
        return self.snapshots

    def read_latest(self) -> list[SessionDataCapability]:
        return self.snapshots


def _app(service: _FakeCapabilityService) -> FastAPI:
    app = FastAPI()
    app.include_router(broker_capability.router)
    app.dependency_overrides[get_broker_capability_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_probe_endpoint_returns_snapshot_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _FakeCapabilityService()
    monkeypatch.setattr(
        broker_capability,
        "require_connected_client",
        lambda: SimpleNamespace(is_connected=lambda: True),
    )

    async with AsyncClient(transport=ASGITransport(app=_app(service)), base_url="http://test") as ac:
        response = await ac.post("/api/broker/capability/probe", params={"symbols": "spy,qqq,spy"})

    assert response.status_code == 200
    assert service.probed_symbols == ["SPY", "QQQ"]
    body = response.json()
    assert body["snapshots"][0]["symbol"] == "SPY"
    assert body["snapshots"][0]["sessions"]["RTH"]["data"] == "live"


@pytest.mark.asyncio
async def test_read_endpoint_returns_persisted_snapshots() -> None:
    service = _FakeCapabilityService()

    async with AsyncClient(transport=ASGITransport(app=_app(service)), base_url="http://test") as ac:
        response = await ac.get("/api/broker/capability")

    assert response.status_code == 200
    assert response.json()["snapshots"][0]["account_mode"] == "live"


def test_capability_service_persists_latest_and_timestamped_snapshot(tmp_path: Path) -> None:
    service = BrokerCapabilityService(root=tmp_path)

    service.persist(_snapshot("QQQ"))

    latest = tmp_path / "U1234567" / "QQQ" / "latest.json"
    timestamped = tmp_path / "U1234567" / "QQQ" / "1783000000000.json"
    assert latest.exists()
    assert timestamped.exists()
    assert service.read_latest()[0].symbol == "QQQ"


def test_capability_service_reads_only_the_newest_matching_scope(tmp_path: Path) -> None:
    service = BrokerCapabilityService(root=tmp_path)
    older = _snapshot("SPY")
    newer = older.model_copy(update={"probed_at_ms": older.probed_at_ms + 1})
    service.persist(newer)
    service.persist(older)
    service.persist(_snapshot("QQQ"))

    selected = service.read_latest_for(symbol="spy", account_id="U1234567")

    assert selected == newer
    latest = (tmp_path / "U1234567" / "SPY" / "latest.json").read_text(encoding="utf-8")
    assert SessionDataCapability.model_validate_json(latest) == newer
    assert service.read_latest_for(symbol="SPY", account_id="other-account") is None


def test_capability_lookup_isolated_from_unrelated_corrupt_snapshot(tmp_path: Path) -> None:
    service = BrokerCapabilityService(root=tmp_path)
    expected = _snapshot("SPY")
    service.persist(expected)
    corrupt = tmp_path / "unrelated-account" / "QQQ" / "latest.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("not json", encoding="utf-8")

    assert service.read_latest_for(symbol="SPY", account_id="U1234567") == expected
    assert service.read_latest() == [expected]

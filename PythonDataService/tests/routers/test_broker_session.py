from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.broker_session import (
    BrokerSessionEvent,
    BrokerSessionEventPage,
    BrokerSessionMirrorSnapshot,
    BrokerSessionRosterRow,
)
from app.services.broker_session_events import get_broker_session_event_service
from app.services.broker_session_mirror import get_broker_session_mirror_service


class _FakeBrokerSessionMirrorService:
    async def snapshot(self) -> BrokerSessionMirrorSnapshot:
        return BrokerSessionMirrorSnapshot(
            as_of_ms=1_783_120_000_000,
            gateway_port=4002,
            observer_status="online",
            ghost_detection_status="available",
            rows=[
                BrokerSessionRosterRow(
                    row_id="socket:21760:50123:4002:0",
                    identity_type="bot",
                    recency="current",
                    socket_present=True,
                    strategy_instance_id="PrajiTSLADemo",
                    run_id="run-a",
                    pid=21760,
                    as_of_ms=1_783_120_000_000,
                    attention_codes=["REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE"],
                )
            ],
        )


class _FakeBrokerSessionEventService:
    def events(
        self,
        *,
        client_id: int | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> BrokerSessionEventPage:
        assert client_id == 42
        assert after_seq == 0
        assert limit == 10
        return BrokerSessionEventPage(
            rows=[
                BrokerSessionEvent(
                    seq=1,
                    ts_ms=1_783_120_000_000,
                    category="link_connectivity",
                    severity="warning",
                    label="IBKR link interrupted",
                    raw_event_type="IBKR_CODE",
                    client_id=42,
                    ibkr_code=1100,
                )
            ],
        )


async def test_broker_session_snapshot_endpoint_returns_roster() -> None:
    app.dependency_overrides[get_broker_session_mirror_service] = (
        lambda: _FakeBrokerSessionMirrorService()
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/broker/session-mirror")
    finally:
        app.dependency_overrides.pop(get_broker_session_mirror_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["observer_status"] == "online"
    assert body["rows"][0]["strategy_instance_id"] == "PrajiTSLADemo"
    assert body["rows"][0]["attention_codes"] == [
        "REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE"
    ]


async def test_broker_session_events_endpoint_returns_classified_rows() -> None:
    app.dependency_overrides[get_broker_session_event_service] = (
        lambda: _FakeBrokerSessionEventService()
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/broker/session-mirror/events?client_id=42&limit=10"
            )
    finally:
        app.dependency_overrides.pop(get_broker_session_event_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["rows"][0]["category"] == "link_connectivity"
    assert body["rows"][0]["ibkr_code"] == 1100

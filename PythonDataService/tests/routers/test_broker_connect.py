"""Tests for POST /api/broker/connect | /disconnect | /reconnect.

Three contract surfaces are pinned here:

* **Disabled mode.** ``IBKR_BROKER_ENABLED=false`` → all three endpoints
  return 503 without touching the client.
* **Idempotency.** A connect-when-already-connected returns the current
  health without invoking ``client.connect()`` a second time. A
  disconnect-when-not-connected returns a disconnected snapshot.
* **Error translation.** ``ConnectionRefusedDueToSentinelError`` →
  502, ``IbkrClientIdInUseError`` → 409, generic ``BrokerError`` → 502.

Uses httpx.AsyncClient + ASGITransport per repo testing rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClientIdInUseError,
)
from app.broker.ibkr.models import IbkrConnectionHealth
from app.main import app

# ─── helpers ───────────────────────────────────────────────────────────


def _build_health(*, connected: bool, is_paper: bool | None = True) -> IbkrConnectionHealth:
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=1,
        connected=connected,
        account_id="DU1234567" if connected else None,
        is_paper=is_paper if connected else None,
        server_version=178 if connected else None,
        fetched_at_ms=now_ms,
        connection_state="connected" if connected else "disconnected",
        last_transition_ms=now_ms,
    )


class FakeClient:
    """In-process stand-in for ``IbkrClient`` — no ib_async required."""

    def __init__(
        self,
        *,
        starts_connected: bool = False,
        connect_raises: Exception | None = None,
    ) -> None:
        self._connected = starts_connected
        self._connect_raises = connect_raises
        self.connect_calls = 0
        self.disconnect_calls = 0
        # Mirror ``IbkrClient.desired_connected`` so the router's
        # set_desired_connected(True/False) calls don't AttributeError.
        self._desired_connected = False
        self.set_desired_calls: list[bool] = []

    async def connect(self) -> IbkrConnectionHealth:
        self.connect_calls += 1
        if self._connect_raises is not None:
            raise self._connect_raises
        self._connected = True
        return _build_health(connected=True)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    @property
    def desired_connected(self) -> bool:
        return self._desired_connected

    def set_desired_connected(self, value: bool) -> None:
        self._desired_connected = value
        self.set_desired_calls.append(value)

    def health(self) -> IbkrConnectionHealth:
        return _build_health(connected=self._connected)


@pytest.fixture
def reset_settings():
    """Reset cached IBKR settings before and after each test."""
    from app.broker.ibkr import config as ibkr_config

    ibkr_config.reset_settings_for_testing()
    yield
    ibkr_config.reset_settings_for_testing()


@pytest.fixture
def installed_fake_client(monkeypatch, reset_settings):
    """Install a fresh ``FakeClient`` as the process-wide client."""
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.routers import broker as broker_router

    fake = FakeClient()
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    # Make the router's factory return THIS fake when get_client() raises.
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    yield fake
    ibkr_client_module.set_client(None)


# ─── disabled mode ─────────────────────────────────────────────────────


@pytest.fixture
def broker_disabled(monkeypatch, reset_settings):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "false")
    yield


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/broker/connect", id="connect_returns_503_when_disabled"),
        pytest.param("/api/broker/disconnect", id="disconnect_returns_503_when_disabled"),
        pytest.param("/api/broker/reconnect", id="reconnect_returns_503_when_disabled"),
    ],
)
async def test_endpoint_returns_503_when_disabled(broker_disabled, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path)
    assert response.status_code == 503, "a disabled broker refuses the endpoint"


# ─── happy path ────────────────────────────────────────────────────────


async def test_connect_from_disconnected_invokes_connect_once_and_re_arms_the_monitor(installed_fake_client):
    fake = installed_fake_client
    assert not fake.is_connected(), "the client starts disconnected"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/connect")

    assert response.status_code == 200, "connect from disconnected answers 200"
    body = response.json()
    assert body["connected"] is True, "the answer reports connected"
    assert body["account_id"] == "DU1234567", "the answer carries the account id"
    assert fake.connect_calls == 1, "connect is invoked exactly once"
    assert fake.is_connected(), "the client ends connected"
    # Dual of the disconnect: operator's Connect re-arms the monitor.
    assert fake.desired_connected is True, "the operator's connect sets the desired state to connected"
    assert True in fake.set_desired_calls, "the desired state was set to True"


async def test_connect_when_already_connected_is_idempotent(installed_fake_client):
    fake = installed_fake_client
    # Pretend the client is already up.
    fake._connected = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/connect")

    assert response.status_code == 200
    assert response.json()["connected"] is True
    # No second connectAsync should have been issued.
    assert fake.connect_calls == 0


async def test_disconnect_when_connected_calls_disconnect(installed_fake_client):
    fake = installed_fake_client
    fake._connected = True
    fake._desired_connected = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/disconnect")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["condition"]["code"] == "DATA_PLANE_BROKER_DISCONNECTED"
    assert "operator request" in body["condition"]["summary"]
    assert fake.disconnect_calls == 1
    # Codex P1 regression — operator's Disconnect must flip desired
    # state to False so the AutoReconnectMonitor stops auto-reconnecting.
    assert fake.desired_connected is False
    assert False in fake.set_desired_calls


async def test_disconnect_when_not_connected_is_idempotent(installed_fake_client):
    fake = installed_fake_client
    assert not fake.is_connected()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/disconnect")

    assert response.status_code == 200
    assert response.json()["connected"] is False
    # disconnect() is still called on the existing client; it's idempotent in IbkrClient too.
    assert fake.disconnect_calls == 1


async def test_disconnect_returns_synthesized_health_when_no_client(monkeypatch, reset_settings):
    """If get_client() raises NotConnectedError, disconnect returns a synthesised snapshot."""
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module

    ibkr_client_module.set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/disconnect")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["account_id"] is None


async def test_reconnect_disconnects_then_connects(installed_fake_client):
    fake = installed_fake_client
    fake._connected = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/reconnect")

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert fake.disconnect_calls == 1
    assert fake.connect_calls == 1


# ─── error translation ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("make_error", "status_code", "detail_text"),
    [
        pytest.param(
            lambda: ConnectionRefusedDueToSentinelError("paper/live mismatch"),
            502,
            "mismatch",
            id="connect_sentinel_mismatch_returns_502",
        ),
        pytest.param(
            lambda: IbkrClientIdInUseError(client_id=1, host="127.0.0.1", port=4002),
            409,
            "in use",
            id="connect_client_id_in_use_returns_409",
        ),
        pytest.param(
            lambda: BrokerError("Gateway unreachable"),
            502,
            "Gateway unreachable",
            id="connect_generic_broker_error_returns_502",
        ),
        # OSError from connectAsync (socket-level failure) must map to 502.
        pytest.param(
            lambda: OSError("socket closed"),
            502,
            "socket closed",
            id="connect_os_error_returns_502",
        ),
    ],
)
async def test_connect_failure_is_translated_to_its_status(
    monkeypatch, reset_settings, make_error, status_code: int, detail_text: str
):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.routers import broker as broker_router

    fake = FakeClient(connect_raises=make_error())
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/broker/connect")
        assert response.status_code == status_code, "the connect failure maps to its HTTP status"
        assert detail_text in response.json()["detail"], "the detail carries the failure text"
    finally:
        ibkr_client_module.set_client(None)


async def test_disconnect_os_error_returns_502(monkeypatch, reset_settings):
    """OSError from client.disconnect() must map to 502 (Major fix from PR #244 review)."""
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module

    fake = FakeClient(starts_connected=True)

    async def raising_disconnect() -> None:
        raise OSError("socket teardown raced pending write")

    fake.disconnect = raising_disconnect  # type: ignore[assignment]
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/broker/disconnect")
        assert response.status_code == 502
        assert "socket teardown" in response.json()["detail"]
    finally:
        ibkr_client_module.set_client(None)

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

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

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

    broker_router._pending_account_service_release_account_id = None
    fake = FakeClient()
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    # Make the router's factory return THIS fake when get_client() raises.
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    warm_calls: list[tuple[FakeClient, IbkrConnectionHealth]] = []

    async def fake_warm_account_evidence(
        client: FakeClient,
        health: IbkrConnectionHealth,
    ) -> None:
        warm_calls.append((client, health))

    monkeypatch.setattr(
        broker_router,
        "_warm_account_evidence_after_connect",
        fake_warm_account_evidence,
    )
    release_calls: list[str | None] = []

    async def fake_release_account_service(account_id: str | None) -> None:
        release_calls.append(account_id)

    monkeypatch.setattr(
        broker_router,
        "_release_account_service_after_disconnect",
        fake_release_account_service,
    )
    fake.warm_calls = warm_calls  # type: ignore[attr-defined]
    fake.release_calls = release_calls  # type: ignore[attr-defined]
    yield fake
    broker_router._pending_account_service_release_account_id = None
    ibkr_client_module.set_client(None)


# ─── disabled mode ─────────────────────────────────────────────────────


@pytest.fixture
def broker_disabled(monkeypatch, reset_settings):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "false")
    yield


async def test_connect_returns_503_when_disabled(broker_disabled):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/connect")
    assert response.status_code == 503


async def test_disconnect_returns_503_when_disabled(broker_disabled):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/disconnect")
    assert response.status_code == 503


async def test_reconnect_returns_503_when_disabled(broker_disabled):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/reconnect")
    assert response.status_code == 503


# ─── happy path ────────────────────────────────────────────────────────


async def test_connect_from_disconnected_invokes_connect_once(installed_fake_client):
    fake = installed_fake_client
    assert not fake.is_connected()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/connect")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["account_id"] == "DU1234567"
    assert fake.connect_calls == 1
    assert len(fake.warm_calls) == 1  # type: ignore[attr-defined]
    assert fake.warm_calls[0][1].account_id == "DU1234567"  # type: ignore[attr-defined]


async def test_connect_warms_account_evidence_from_raw_connected_health_after_hard_down(
    installed_fake_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = installed_fake_client
    monitor = SimpleNamespace(
        is_hard_down=True,
        is_attempting=False,
        is_recovering=False,
        current_attempt=0,
        successful_reconnect_count=0,
        last_transition_ms=1_700_000_005_000,
        recovery_state="HARD_DOWN",
    )
    from app.routers import broker as broker_router

    monkeypatch.setattr(broker_router, "get_monitor", lambda: monitor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/connect")

    assert response.status_code == 200
    assert response.json()["connection_state"] == "hard_down"
    assert len(fake.warm_calls) == 1  # type: ignore[attr-defined]
    assert fake.warm_calls[0][1].connection_state == "connected"  # type: ignore[attr-defined]


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
    assert len(fake.warm_calls) == 1  # type: ignore[attr-defined]


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
    assert fake.release_calls == ["DU1234567"]  # type: ignore[attr-defined]


async def test_connect_endpoint_sets_desired_connected_true(installed_fake_client):
    """Dual of the above — operator's Connect re-arms the monitor."""
    fake = installed_fake_client
    fake._desired_connected = False

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/broker/connect")

    assert response.status_code == 200
    assert fake.desired_connected is True
    assert True in fake.set_desired_calls


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
    assert len(fake.warm_calls) == 1  # type: ignore[attr-defined]


async def test_warm_account_evidence_timeout_is_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.engine.live import host_daemon_client
    from app.routers import broker as broker_router
    from app.services import account_truth_refresh

    async def never_finishes(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(account_truth_refresh, "refresh_account_truth_now", never_finishes)
    monkeypatch.setattr(host_daemon_client, "ensure_account_clerk", never_finishes)
    monkeypatch.setattr(broker_router, "_ACCOUNT_SERVICE_ATTACH_TIMEOUT_S", 0.001)
    monkeypatch.setattr(broker_router, "_ACCOUNT_EVIDENCE_WARMUP_TIMEOUT_S", 0.001)

    with caplog.at_level(logging.WARNING, logger=broker_router.logger.name):
        await broker_router._warm_account_evidence_after_connect(
            FakeClient(starts_connected=True),  # type: ignore[arg-type]
            _build_health(connected=True),
        )

    assert "broker connect account-evidence warm-up failed" in caplog.text


async def test_warm_account_evidence_attaches_service_before_account_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live import host_daemon_client
    from app.services import account_truth_refresh

    order: list[str] = []

    async def ensure(*_args, **_kwargs) -> dict:
        order.append("ensure")
        return {}

    async def refresh(*_args, **_kwargs):
        order.append("refresh")
        return None

    monkeypatch.setattr(host_daemon_client, "ensure_account_clerk", ensure)
    monkeypatch.setattr(account_truth_refresh, "refresh_account_truth_now", refresh)
    monkeypatch.setattr(
        "app.services.account_reconciliation.AccountReconciliationService.ensure_automatic_reconciliation",
        lambda self, **_kwargs: None,
    )

    from app.routers import broker as broker_router

    await broker_router._warm_account_evidence_after_connect(
        FakeClient(starts_connected=True),  # type: ignore[arg-type]
        _build_health(connected=True),
    )

    assert order == ["ensure", "refresh"]


async def test_warm_account_evidence_refreshes_truth_when_account_service_attach_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live import host_daemon_client
    from app.services import account_truth_refresh

    refreshed: list[str] = []

    async def ensure(*_args, **_kwargs) -> dict:
        raise host_daemon_client.HostDaemonError(503, "daemon unavailable")

    async def refresh(*_args, **_kwargs):
        refreshed.append("truth")
        return None

    monkeypatch.setattr(host_daemon_client, "ensure_account_clerk", ensure)
    monkeypatch.setattr(account_truth_refresh, "refresh_account_truth_now", refresh)
    monkeypatch.setattr(
        "app.services.account_reconciliation.AccountReconciliationService.ensure_automatic_reconciliation",
        lambda self, **_kwargs: None,
    )

    from app.routers import broker as broker_router

    await broker_router._warm_account_evidence_after_connect(
        FakeClient(starts_connected=True),  # type: ignore[arg-type]
        _build_health(connected=True),
    )

    assert refreshed == ["truth"]


async def test_release_account_service_preserves_detached_account_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from app.engine.live import host_daemon_client
    from app.routers import broker as broker_router

    calls: list[str] = []

    async def release(_base_url: str, account_id: str) -> dict:
        calls.append(account_id)
        if len(calls) == 1:
            raise host_daemon_client.HostDaemonError(503, "temporarily unavailable")
        return {}

    monkeypatch.setattr(host_daemon_client, "release_account_clerk", release)
    broker_router._pending_account_service_release_account_id = None

    with pytest.raises(HTTPException) as exc_info:
        await broker_router._release_account_service_after_disconnect("DU1234567")

    assert exc_info.value.status_code == 503
    assert broker_router._pending_account_service_release_account_id == "DU1234567"

    await broker_router._release_account_service_after_disconnect(None)

    assert calls == ["DU1234567", "DU1234567"]
    assert broker_router._pending_account_service_release_account_id is None


# ─── error translation ────────────────────────────────────────────────


async def test_connect_sentinel_mismatch_returns_502(monkeypatch, reset_settings):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.routers import broker as broker_router

    fake = FakeClient(connect_raises=ConnectionRefusedDueToSentinelError("paper/live mismatch"))
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/broker/connect")
        assert response.status_code == 502
        assert "mismatch" in response.json()["detail"]
    finally:
        ibkr_client_module.set_client(None)


async def test_connect_client_id_in_use_returns_409(monkeypatch, reset_settings):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.routers import broker as broker_router

    fake = FakeClient(connect_raises=IbkrClientIdInUseError(client_id=1, host="127.0.0.1", port=4002))
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/broker/connect")
        assert response.status_code == 409
        assert "in use" in response.json()["detail"]
    finally:
        ibkr_client_module.set_client(None)


async def test_connect_generic_broker_error_returns_502(monkeypatch, reset_settings):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.routers import broker as broker_router

    fake = FakeClient(connect_raises=BrokerError("Gateway unreachable"))
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/broker/connect")
        assert response.status_code == 502
        assert "Gateway unreachable" in response.json()["detail"]
    finally:
        ibkr_client_module.set_client(None)


async def test_connect_os_error_returns_502(monkeypatch, reset_settings):
    """OSError from connectAsync (socket-level failure) must map to 502."""
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.routers import broker as broker_router

    fake = FakeClient(connect_raises=OSError("socket closed"))
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/broker/connect")
        assert response.status_code == 502
        assert "socket closed" in response.json()["detail"]
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

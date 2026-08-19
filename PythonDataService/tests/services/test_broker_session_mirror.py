from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engine.live.daemon_transport import DaemonResult
from app.schemas.broker_session import GatewaySocketRow, GatewaySocketsSnapshot
from app.services import broker_session_mirror
from app.services.broker_session_mirror import BrokerSessionMirrorService


class _Events:
    def events_for_rows(self, _rows):
        return {}


class _History:
    def past_closed_rows(self, *, current_rows):
        return []

    def append_snapshot(self, _snapshot):
        raise AssertionError("read-only snapshot must not append history by default")


@pytest.mark.asyncio
async def test_snapshot_uses_socket_evidence_without_bot_registry_or_run_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(live_runner_daemon_url="http://host", port=4002)
    monkeypatch.setattr(broker_session_mirror, "get_settings", lambda: settings)

    async def fetch(_url: str, *, gateway_port: int):
        assert gateway_port == 4002
        return DaemonResult.connected(), GatewaySocketsSnapshot(
            fetched_at_ms=1,
            gateway_port=4002,
            sockets=[GatewaySocketRow(pid=42, command="unknown", remote_port=4002)],
        )

    monkeypatch.setattr(broker_session_mirror.host_daemon_client, "fetch_gateway_sockets", fetch)
    monkeypatch.setattr(broker_session_mirror, "_data_plane_health", lambda: None)
    service = BrokerSessionMirrorService(event_service=_Events(), history_service=_History())

    snapshot = await service.snapshot()

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].identity_type == "ghost"
    assert snapshot.rows[0].strategy_instance_id is None
    assert snapshot.rows[0].run_id is None


@pytest.mark.asyncio
async def test_snapshot_degrades_when_host_socket_probe_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(live_runner_daemon_url="http://host", port=4002)
    monkeypatch.setattr(broker_session_mirror, "get_settings", lambda: settings)

    async def fetch(_url: str, *, gateway_port: int):
        return DaemonResult(kind="UNREACHABLE", detail="offline"), None

    monkeypatch.setattr(broker_session_mirror.host_daemon_client, "fetch_gateway_sockets", fetch)
    monkeypatch.setattr(broker_session_mirror, "_data_plane_health", lambda: None)
    service = BrokerSessionMirrorService(event_service=_Events(), history_service=_History())

    snapshot = await service.snapshot()

    assert snapshot.observer_status == "degraded"
    assert snapshot.rows == []
    assert "offline" in snapshot.degradation_reasons

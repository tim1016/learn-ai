"""The account-cockpit capability probe uses its bounded readiness timeout."""

from __future__ import annotations

from app.engine.live import host_daemon_client
from app.engine.live.host_daemon_client import DaemonResult


async def test_fetch_startability_health_uses_bounded_read_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_classify_http(url, *, method, timeout=host_daemon_client._TIMEOUT):
        captured["url"] = url
        captured["method"] = method
        captured["timeout"] = timeout
        return DaemonResult.connected(), None

    monkeypatch.setattr(host_daemon_client, "_classify_http", fake_classify_http)

    result, health = await host_daemon_client.fetch_startability_health("http://d")

    assert result.kind == "CONNECTED"
    assert health is None
    assert str(captured["url"]).endswith("/health")
    assert captured["method"] == "GET"
    assert captured["timeout"] is host_daemon_client._READINESS_TIMEOUT

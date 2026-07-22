"""``fetch_instance_process`` must use the longer instance-probe timeout, not the 2s
health default.

Root cause C of the concurrent-cohort hardening: the roll-call falls back to a per-bot
``/instances/{id}/process`` probe for idle candidates, and under concurrent load the
single-loop host daemon can exceed the 2s health timeout — silently dropping an
otherwise-ready member from the roll-call at its slot. This regression pins the probe
to the dedicated (longer) timeout so it is not reverted to the health default.
"""

from __future__ import annotations

from typing import Literal

import httpx
import pytest

from app.engine.live import host_daemon_client
from app.engine.live.host_daemon_client import DaemonResult


async def test_fetch_instance_process_uses_instance_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_typed_get_json(
        url: str,
        *,
        timeout: httpx.Timeout = host_daemon_client._TIMEOUT,
    ) -> tuple[DaemonResult, dict[str, str]]:
        captured["url"] = url
        captured["timeout"] = timeout
        return DaemonResult.connected(status=200), {"state": "idle"}

    monkeypatch.setattr(host_daemon_client, "_typed_get_json", fake_typed_get_json)

    _result, payload = await host_daemon_client.fetch_instance_process("http://d", "bot-a")

    assert payload == {"state": "idle"}
    assert str(captured["url"]).endswith("/instances/bot-a/process")
    # The probe must use the dedicated longer timeout, distinct from the 2s health default.
    assert captured["timeout"] is host_daemon_client._INSTANCE_PROBE_TIMEOUT
    assert host_daemon_client._INSTANCE_PROBE_TIMEOUT.read > host_daemon_client._TIMEOUT.read


async def test_fetch_startability_health_uses_instance_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_classify_http(
        url: str,
        *,
        method: Literal["GET", "POST"],
        timeout: httpx.Timeout = host_daemon_client._TIMEOUT,
    ) -> tuple[DaemonResult, None]:
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
    assert captured["timeout"] is host_daemon_client._INSTANCE_PROBE_TIMEOUT

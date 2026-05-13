"""Tests for app.broker.ibkr.client — sentinel logic and lifecycle.

The ``IB`` instance is replaced with an ``unittest.mock.MagicMock`` —
ib_async is a heavy dependency and these tests need to run on hosts
that don't have it installed (light-layer-only CI for unrelated work).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClient,
    _detect_host_gateway,
    _is_paper_account,
    _resolve_host,
)
from app.broker.ibkr.config import IbkrSettings


@pytest.fixture
def settings_paper() -> IbkrSettings:
    return IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)


@pytest.fixture
def settings_live() -> IbkrSettings:
    return IbkrSettings(mode="live", port=4001, connect_attempts=1, _env_file=None)


def _patched_ib_class() -> tuple:
    """Patch ``ib_async.IB`` with a MagicMock that returns a fresh
    AsyncMock-backed ``IB()`` per call.

    NOTE: ``IB.disconnect`` is synchronous in ib_async — pre-mock it as a
    plain ``MagicMock`` (not ``AsyncMock``) so this fixture matches the
    real API. A previous version pre-mocked ``disconnectAsync`` as
    ``AsyncMock``, which silently auto-created the wrong attribute and
    masked the 2026-05-13 disconnect bug.
    """
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock(return_value=None)
    fake_ib.disconnect = MagicMock(return_value=None)
    fake_ib.isConnected = MagicMock(return_value=True)
    fake_ib.client = MagicMock()
    fake_ib.client.serverVersion = MagicMock(return_value=178)
    fake_ib.managedAccounts = MagicMock(return_value=[])
    fake_class = MagicMock(return_value=fake_ib)
    return fake_ib, fake_class


def test_paper_account_sentinel_predicate() -> None:
    assert _is_paper_account("DU1234567")
    assert _is_paper_account("du1234567")
    assert not _is_paper_account("U1234567")
    assert not _is_paper_account("F1234567")


# ── host auto-detection (default-gateway parsing) ───────────────────────


def _write_route_file(tmp_path, body: str) -> str:
    """Helper: write a fake /proc/net/route and return its path."""
    p = tmp_path / "route"
    p.write_text(body)
    return str(p)


def test_detect_host_gateway_parses_default_route_correctly(tmp_path) -> None:
    # Real-shape /proc/net/route. Default route is line 2 below.
    # 0202000A is little-endian for 10.0.2.2 (the canonical Podman host).
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0202000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000010AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    detected = _detect_host_gateway(_write_route_file(tmp_path, body))
    assert detected == "10.0.2.2"


def test_detect_host_gateway_skips_non_default_routes(tmp_path) -> None:
    # No 00000000 destination → no default route → None.
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t000010AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    assert _detect_host_gateway(_write_route_file(tmp_path, body)) is None


def test_detect_host_gateway_requires_rtf_gateway_flag(tmp_path) -> None:
    # 00000000 destination but flags=0x0001 (RTF_UP only, no RTF_GATEWAY) → None.
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0202000A\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    assert _detect_host_gateway(_write_route_file(tmp_path, body)) is None


def test_detect_host_gateway_returns_none_on_missing_file(tmp_path) -> None:
    assert _detect_host_gateway(tmp_path / "does-not-exist") is None


def test_resolve_host_passes_explicit_ip_through() -> None:
    assert _resolve_host("192.168.1.5") == "192.168.1.5"
    assert _resolve_host("host.docker.internal") == "host.docker.internal"
    assert _resolve_host("127.0.0.1") == "127.0.0.1"


def test_resolve_host_resolves_auto_via_route_file(monkeypatch, tmp_path) -> None:
    body = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t01001FAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    route = _write_route_file(tmp_path, body)
    monkeypatch.setattr(
        "app.broker.ibkr.client._detect_host_gateway",
        lambda route_file="/proc/net/route": "172.31.0.1",
    )
    assert _resolve_host("auto") == "172.31.0.1"
    # Sanity-check the underlying helper too — call site passes a route
    # path, so the patched callable must accept the same parameter.
    from app.broker.ibkr.client import _detect_host_gateway as real_detect

    assert real_detect(route) == "172.31.0.1"


def test_resolve_host_falls_back_to_literal_when_detection_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.broker.ibkr.client._detect_host_gateway",
        lambda route_file="/proc/net/route": None,
    )
    assert _resolve_host("auto") == "auto"


@pytest.mark.asyncio
async def test_connect_succeeds_when_paper_account_returned(
    settings_paper: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = ["DU1234567"]

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        health = await client.connect()

    assert client.connected_account == "DU1234567"
    assert health.is_paper is True
    assert health.connected is True
    fake_ib.connectAsync.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_aborts_when_paper_mode_gets_live_account(
    settings_paper: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = ["U7654321"]

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        with pytest.raises(ConnectionRefusedDueToSentinelError):
            await client.connect()

    fake_ib.disconnect.assert_called()
    assert client.connected_account is None


@pytest.mark.asyncio
async def test_connect_aborts_when_live_mode_gets_paper_account(
    settings_live: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = ["DU1234567"]

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_live)
        with pytest.raises(ConnectionRefusedDueToSentinelError):
            await client.connect()


@pytest.mark.asyncio
async def test_connect_raises_when_no_managed_accounts(
    settings_paper: IbkrSettings,
) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.managedAccounts.return_value = []

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        with pytest.raises(BrokerError):
            await client.connect()


@pytest.mark.asyncio
async def test_connect_retries_then_fails(settings_paper: IbkrSettings) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.connectAsync.side_effect = OSError("Gateway unreachable")

    settings_three = IbkrSettings(mode="paper", port=4002, connect_attempts=3, _env_file=None)

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_three)
        with pytest.raises(BrokerError):
            await client.connect()

    assert fake_ib.connectAsync.await_count == 3


@pytest.mark.asyncio
async def test_disconnect_is_idempotent(settings_paper: IbkrSettings) -> None:
    fake_ib, fake_class = _patched_ib_class()
    fake_ib.isConnected.return_value = False  # already disconnected

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        await client.disconnect()
        await client.disconnect()

    fake_ib.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_calls_sync_ib_disconnect_when_connected(
    settings_paper: IbkrSettings,
) -> None:
    # Surfaced 2026-05-13 by the paper-Gateway smoke run: ib_async.IB
    # exposes a synchronous .disconnect(), not .disconnectAsync(). The
    # other tests in this file use an unspec'd MagicMock that silently
    # auto-creates .disconnectAsync, hiding the mismatch. spec=IB
    # restricts attribute access to real IB methods.
    import ib_async

    fake_ib = MagicMock(spec=ib_async.IB)
    fake_ib.isConnected.return_value = True
    fake_ib.disconnect.return_value = "Disconnected"
    fake_class = MagicMock(return_value=fake_ib)

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings_paper)
        client._connected_account = "DU1234567"
        await client.disconnect()

    fake_ib.disconnect.assert_called_once()
    assert client.connected_account is None

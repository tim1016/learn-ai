"""IBKR option reads reach the selected Alpaca clerk in a fleet deployment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException

from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
from app.broker.fleet.provider import OperationReadiness, OperationStream
from app.broker.ibkr.client import NotConnectedError
from app.broker.ibkr.models import IbkrChainSnapshot, IbkrSurfaceExpiry, IbkrSurfaceSnapshot
from app.config import fleet_settings, settings
from app.options.expiry import option_expiry_anchor_ms
from app.routers import broker
from app.security.data_plane_control import require_data_plane_control_secret_always
from app.utils.error_handlers import install_fleet_control_error_handler
from tests.broker.fleet import test_b_scoped_contracts as scoped_contracts

_SERVICE_ROOT = Path(__file__).resolve().parents[3]
_PUBLIC_PREFIX = "/api/brokers/{broker}/clerks/{clerk_id}/market-data/ibkr"
_AGENT_PREFIX = "/api/broker"
_READ_PATHS = (
    "/health",
    "/expirations/{symbol}",
    "/strikes/{symbol}",
    "/option-chain/{symbol}",
    "/option-surface/{symbol}",
)
_EXPIRY = option_expiry_anchor_ms(date(2026, 10, 16))
_ROLE_ROUTES_PROBE = """
import json
import os
import sys

os.environ.setdefault("POLYGON_API_KEY", "contract-schema-placeholder")
os.environ["ALPACA_FAULT_INJECTION_ENABLED"] = "false"
os.environ["FLEET_ROLE"] = sys.argv[1]
os.environ.setdefault("FLEET_CONTROL_DIR", "contract-schema-fleet-role-probe")
if sys.argv[1] == "clerk_agent":
    os.environ["FLEET_MAX_INFLIGHT_REQUESTS"] = "4"
    os.environ["FLEET_MAX_INFLIGHT_STREAMS"] = "4"

from app.main import app

sys.stdout.write(json.dumps({path: sorted(methods) for path, methods in app.openapi()["paths"].items()}))
"""


def _role_routes(role: str) -> dict[str, list[str]]:
    result = subprocess.run(
        [sys.executable, "-c", _ROLE_ROUTES_PROBE, role],
        cwd=_SERVICE_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.slow
def test_coordinator_and_clerk_agent_serve_only_their_market_data_side() -> None:
    coordinator = _role_routes("fleet_coordinator")
    clerk = _role_routes("clerk_agent")

    for suffix in _READ_PATHS:
        assert "get" in coordinator[_PUBLIC_PREFIX + suffix]
        assert "get" in clerk[_AGENT_PREFIX + suffix]
        assert _AGENT_PREFIX + suffix not in coordinator
        assert _PUBLIC_PREFIX + suffix not in clerk

    for suffix in ("/connect", "/disconnect", "/reconnect"):
        assert _AGENT_PREFIX + suffix not in clerk
        assert _AGENT_PREFIX + suffix not in coordinator


def test_market_data_reads_do_not_require_a_confirmed_alpaca_account() -> None:
    operations = {
        operation.path_template: operation for operation in ALPACA_OPERATIONS
    }
    for suffix in _READ_PATHS:
        operation = operations["/market-data/ibkr" + suffix]
        assert operation.agent_path_template == _AGENT_PREFIX + suffix
        assert operation.readiness is OperationReadiness.CONFIGURATION_ACCESS
        assert not operation.requires_effective_account
        expected_stream = (
            OperationStream.SSE
            if suffix.startswith(("/option-chain", "/option-surface"))
            else OperationStream.NONE
        )
        assert operation.stream is expected_stream


@pytest.fixture
def market_fleet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[scoped_contracts._Fleet]:
    """The existing real-socket fleet fixture with the clerk's actual read router."""
    original_agent_app = scoped_contracts._build_agent_app
    original_coordinator_app = scoped_contracts._coordinator_app

    def agent_app(
        identity: dict[str, object],
        *,
        flip_identity_after_first_event: bool = False,
        minted_receipts: list[str] | None = None,
        refuse_unpinned_mutations: bool = False,
    ) -> FastAPI:
        agent = original_agent_app(
            identity,
            flip_identity_after_first_event=flip_identity_after_first_event,
            minted_receipts=minted_receipts,
            refuse_unpinned_mutations=refuse_unpinned_mutations,
        )
        agent.include_router(
            broker.read_router,
            prefix="/api/broker",
            dependencies=[Depends(require_data_plane_control_secret_always)],
        )
        return agent

    monkeypatch.setattr(scoped_contracts, "_build_agent_app", agent_app)

    def coordinator_app(lane: scoped_contracts._Lane) -> FastAPI:
        coordinator = original_coordinator_app(lane)
        install_fleet_control_error_handler(coordinator)
        return coordinator

    monkeypatch.setattr(scoped_contracts, "_coordinator_app", coordinator_app)
    handle = scoped_contracts._Fleet(tmp_path)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-plane-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    monkeypatch.setattr(fleet_settings, "COORDINATOR_SERVICE_TOKEN", scoped_contracts.COORDINATOR_TOKEN)
    try:
        yield handle
    finally:
        handle.stop()


async def test_clerk_scoped_health_reports_disconnection_and_requires_control_secret(
    market_fleet: scoped_contracts._Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disconnected() -> None:
        raise NotConnectedError("No IBKR session")

    monkeypatch.setattr(broker, "get_client", disconnected)
    monkeypatch.setattr(broker, "is_broker_disabled", lambda: False)
    url = f"{market_fleet.base}/market-data/ibkr/health"

    async with market_fleet.client() as client:
        refused = await client.get(url)
        assert refused.status_code == 403

        response = await client.get(
            url, headers={"X-Data-Plane-Control-Secret": "test-plane-secret"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["connected"] is False
    assert response.headers["x-fleet-broker"] == "alpaca"
    assert response.headers["x-fleet-clerk-id"] == market_fleet.lane.clerk_id


async def test_disconnected_clerk_refuses_option_reads_and_streams(
    market_fleet: scoped_contracts._Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disconnected() -> None:
        raise HTTPException(503, "IBKR client not connected")

    monkeypatch.setattr(broker, "require_connected_client", disconnected)
    base = f"{market_fleet.base}/market-data/ibkr"
    headers = {"X-Data-Plane-Control-Secret": "test-plane-secret"}

    async with market_fleet.client() as client:
        metadata = await client.get(f"{base}/expirations/SPY", headers=headers)
        stream = await client.get(
            f"{base}/option-chain/SPY",
            params={"expiry_ms": _EXPIRY, "strikes": 500},
            headers=headers,
        )

    assert metadata.status_code == 503
    assert metadata.json()["reason"] == "clerk_unreachable"
    assert stream.status_code == 503
    assert stream.json()["reason"] == "clerk_unreachable"


async def test_clerk_scoped_option_metadata_and_streams_use_its_ibkr_feed(
    market_fleet: scoped_contracts._Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = object()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(broker, "require_connected_client", lambda: feed)

    async def expirations(client: object, symbol: str) -> list[int]:
        calls.append(("expirations", client, symbol))
        return [_EXPIRY]

    async def strikes(client: object, symbol: str, expiry_ms: int) -> list[float]:
        calls.append(("strikes", client, symbol, expiry_ms))
        return [500.0, 505.0]

    async def chain(
        client: object,
        symbol: str,
        expiry_ms: int,
        band: list[float],
        *,
        debounce_seconds: float,
    ) -> AsyncIterator[IbkrChainSnapshot]:
        calls.append(("chain", client, symbol, expiry_ms, band, debounce_seconds))
        yield IbkrChainSnapshot(symbol=symbol, expiry_ms=expiry_ms, quotes=[], as_of_ms=1)

    async def surface(
        client: object,
        symbol: str,
        expiry_ms: list[int],
        band: list[float],
        *,
        debounce_seconds: float,
        max_lines: int,
    ) -> AsyncIterator[IbkrSurfaceSnapshot]:
        calls.append(("surface", client, symbol, expiry_ms, band, debounce_seconds, max_lines))
        yield IbkrSurfaceSnapshot(
            symbol=symbol,
            expiries=[IbkrSurfaceExpiry(expiry_ms=_EXPIRY, quotes=[])],
            line_count=1,
            as_of_ms=2,
        )

    monkeypatch.setattr(broker.ibkr_contracts, "list_expirations", expirations)
    monkeypatch.setattr(broker.ibkr_contracts, "list_qualified_strikes", strikes)
    monkeypatch.setattr(broker, "stream_option_chain", chain)
    monkeypatch.setattr(broker, "stream_option_surface", surface)
    headers = {"X-Data-Plane-Control-Secret": "test-plane-secret"}
    base = f"{market_fleet.base}/market-data/ibkr"

    async with market_fleet.client() as client:
        expiry_response = await client.get(f"{base}/expirations/SPY", headers=headers)
        strikes_response = await client.get(
            f"{base}/strikes/SPY", params={"expiry_ms": _EXPIRY}, headers=headers
        )
        chain_response = await client.get(
            f"{base}/option-chain/SPY",
            params={"expiry_ms": _EXPIRY, "strikes": 500},
            headers=headers,
        )
        surface_response = await client.get(
            f"{base}/option-surface/SPY",
            params={"expiry_ms": _EXPIRY, "strikes": 500},
            headers=headers,
        )

    assert expiry_response.status_code == 200, expiry_response.text
    assert expiry_response.json() == {"symbol": "SPY", "expirations_ms": [_EXPIRY]}
    assert strikes_response.status_code == 200, strikes_response.text
    assert strikes_response.json()["strikes"] == [500.0, 505.0]
    assert chain_response.status_code == 200, chain_response.text
    assert "event: chain" in chain_response.text
    assert surface_response.status_code == 200, surface_response.text
    assert "event: surface" in surface_response.text
    assert calls == [
        ("expirations", feed, "SPY"),
        ("strikes", feed, "SPY", _EXPIRY),
        ("chain", feed, "SPY", _EXPIRY, [500.0], 0.25),
        ("surface", feed, "SPY", [_EXPIRY], [500.0], 0.25, 100),
    ]

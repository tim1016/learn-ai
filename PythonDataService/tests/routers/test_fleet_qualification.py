"""Guards for the Compose-only serving-lane qualification hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from fastapi import HTTPException

import app.routers.fleet_qualification as qualification
from app.broker.alpaca.broker import AlpacaBroker
from app.routers.fleet_qualification import (
    QualificationHoldRequestResponse,
    QualificationMarketDependencyAvailable,
    QualificationMarketDependencyUnavailable,
    qualification_router,
    require_qualification_secret,
)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


def test_qualification_router_is_absent_without_each_strict_guard() -> None:
    """Normal, coordinator, and non-random namespaces cannot expose the hook."""
    assert qualification_router(role="combined", namespace="compose:fleetqualificationx", secret="s", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="host:local", secret="s", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="s", broker_url="http://fake", market_data_url="") is None


@pytest.mark.parametrize("supplied", [None, "wrong-secret", "é"])
def test_require_qualification_secret_answers_404_for_any_wrong_secret(supplied: str | None) -> None:
    """A non-ASCII header (Starlette decodes header bytes as Latin-1) must get
    the same 404 as any wrong secret; ``hmac.compare_digest`` on ``str``
    raised ``TypeError`` for it, a 500 that reveals the hidden surface."""
    with pytest.raises(HTTPException) as refused:
        require_qualification_secret(supplied, "probe-secret")

    assert refused.value.status_code == 404


def test_require_qualification_secret_admits_the_exact_secret() -> None:
    require_qualification_secret("probe-secret", "probe-secret")


def test_qualification_router_has_no_execution_operation() -> None:
    """The opt-in surface is observation and held ASGI work only -- never an
    order, fill, or position (issue #2206 widens this from the original
    "read-only, no POST" invariant to add the command-pool probe, still
    behind this router's own secret gate and not an execution op).
    """
    router = qualification_router(
        role="clerk_agent",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake",
        market_data_url="http://market",
    )
    assert router is not None
    paths = {route.path for route in router.routes}
    assert paths == {
        "/internal/fleet-qualification/dependency/market-data",
        "/internal/fleet-qualification/hold/request",
        "/internal/fleet-qualification/hold/stream",
        "/internal/fleet-qualification/hold/command",
    }


def test_qualification_hold_command_route_is_a_post() -> None:
    """Issue #2206: must be POST so the deployed middleware's method-based
    split (``_requires_command_capacity``) classifies it into the command
    pool rather than the request pool."""
    router = qualification_router(
        role="clerk_agent",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake",
        market_data_url="http://market",
    )
    assert router is not None
    routes = {route.path: route for route in router.routes}
    assert routes["/internal/fleet-qualification/hold/command"].methods == {"POST"}
    assert routes["/internal/fleet-qualification/hold/request"].methods == {"GET"}


def test_is_qualification_coordinator_lane_requires_every_guard() -> None:
    """Mirrors ``_is_qualification_lane``'s guard shape for the coordinator role."""
    from app.routers.fleet_qualification import is_qualification_coordinator_lane

    assert is_qualification_coordinator_lane(
        role="fleet_coordinator", namespace="compose:fleetqualificationx", secret="s"
    ) is True
    assert is_qualification_coordinator_lane(
        role="combined", namespace="compose:fleetqualificationx", secret="s"
    ) is False
    assert is_qualification_coordinator_lane(
        role="clerk_agent", namespace="compose:fleetqualificationx", secret="s"
    ) is False
    assert is_qualification_coordinator_lane(
        role="fleet_coordinator", namespace="host:local", secret="s"
    ) is False
    assert is_qualification_coordinator_lane(
        role="fleet_coordinator", namespace="compose:fleetqualificationx", secret=""
    ) is False


def test_is_qualification_coordinator_lane_from_environment_reads_the_probe_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers.fleet_qualification import is_qualification_coordinator_lane_from_environment

    monkeypatch.delenv("FLEET_QUALIFICATION_PROBE_SECRET", raising=False)
    assert is_qualification_coordinator_lane_from_environment(
        "fleet_coordinator", "compose:fleetqualificationx"
    ) is False

    monkeypatch.setenv("FLEET_QUALIFICATION_PROBE_SECRET", "minted-secret")
    assert is_qualification_coordinator_lane_from_environment(
        "fleet_coordinator", "compose:fleetqualificationx"
    ) is True


def test_qualification_probe_routes_declare_strict_success_and_failure_models() -> None:
    """The hidden probes still have typed Pydantic-v2 response contracts."""
    router = qualification_router(
        role="clerk_agent",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake",
        market_data_url="http://market",
    )
    assert router is not None
    routes = {route.path: route for route in router.routes}
    dependency = routes["/internal/fleet-qualification/dependency/market-data"]
    hold_request = routes["/internal/fleet-qualification/hold/request"]

    assert dependency.response_model is QualificationMarketDependencyAvailable
    assert dependency.responses[503]["model"] is QualificationMarketDependencyUnavailable
    assert hold_request.response_model is QualificationHoldRequestResponse
    assert QualificationMarketDependencyAvailable.model_json_schema()["additionalProperties"] is False
    assert QualificationMarketDependencyUnavailable.model_json_schema()["additionalProperties"] is False
    assert QualificationHoldRequestResponse.model_json_schema()["additionalProperties"] is False


def test_install_qualification_bindings_default_off_leaves_registry_and_consumer_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal role never imports the SDK or changes either runtime singleton."""
    monkeypatch.setattr(
        qualification,
        "get_broker_registry",
        lambda: pytest.fail("the qualification registry must stay untouched"),
    )
    monkeypatch.setattr(
        qualification,
        "set_market_liveness_consumer",
        lambda _consumer: pytest.fail("the qualification consumer must stay untouched"),
    )

    assert qualification.install_qualification_bindings(
        role="combined",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake-provider",
        market_data_url="http://fake-market-data",
        account_mode="paper",
    ) is False


@pytest.mark.asyncio
async def test_install_qualification_bindings_paper_uses_sdk_override_and_status_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paper's normal account read uses the client seam and installs its supported feed."""
    calls: dict[str, object] = {}

    class FakeSdkClient:
        def __init__(self, **kwargs: object) -> None:
            calls.update(kwargs)

        def get_account(self) -> dict[str, object]:
            return {
                "account_number": "PAQUALIFICATION",
                "status": "ACTIVE",
                "cash": "1000",
                "equity": "1000",
                "buying_power": "1000",
                "portfolio_value": "1000",
                "long_market_value": "0",
                "short_market_value": "0",
                "trading_blocked": False,
                "account_blocked": False,
            }

    class Registry:
        port: object | None = None

        def register(self, port: object) -> None:
            self.port = port

    fake_module = ModuleType("alpaca.trading.client")
    fake_module.TradingClient = FakeSdkClient
    registry = Registry()
    consumers: list[object] = []
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_module)
    monkeypatch.setattr(qualification, "get_broker_registry", lambda: registry)
    monkeypatch.setattr(qualification, "set_market_liveness_consumer", consumers.append)

    assert qualification.install_qualification_bindings(
        role="clerk_agent",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake-provider",
        market_data_url="http://fake-market-data",
        account_mode="paper",
    ) is True
    assert registry.port is not None
    snapshot = await cast(AlpacaBroker, registry.port).get_account()

    assert snapshot.account_id == "PAQUALIFICATION"
    assert calls == {
        "api_key": "qualification-key",
        "secret_key": "qualification-secret",
        "paper": True,
        "raw_data": True,
        "url_override": "http://fake-provider",
    }
    assert len(consumers) == 1


@pytest.mark.asyncio
async def test_install_qualification_bindings_live_uses_its_own_account_mode_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live qualifies its independent SDK account path without Paper's status source."""
    calls: dict[str, object] = {}

    class FakeSdkClient:
        def __init__(self, **kwargs: object) -> None:
            calls.update(kwargs)

        def get_account(self) -> dict[str, object]:
            return {
                "account_number": "LQUALIFICATION",
                "status": "ACTIVE",
                "cash": "1000",
                "equity": "1000",
                "buying_power": "1000",
                "portfolio_value": "1000",
                "long_market_value": "0",
                "short_market_value": "0",
                "trading_blocked": False,
                "account_blocked": False,
            }

    class Registry:
        port: object | None = None

        def register(self, port: object) -> None:
            self.port = port

    fake_module = ModuleType("alpaca.trading.client")
    fake_module.TradingClient = FakeSdkClient
    registry = Registry()
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_module)
    monkeypatch.setattr(qualification, "get_broker_registry", lambda: registry)
    monkeypatch.setattr(
        qualification,
        "set_market_liveness_consumer",
        lambda _consumer: pytest.fail("Live must not use Paper's shared status source"),
    )

    assert qualification.install_qualification_bindings(
        role="clerk_agent",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake-live-provider",
        market_data_url="http://fake-market-data",
        account_mode="live",
    ) is True
    assert registry.port is not None
    snapshot = await cast(AlpacaBroker, registry.port).get_account()

    assert snapshot.account_id == "LQUALIFICATION"
    assert snapshot.account_mode == "live"
    assert calls["paper"] is False
    assert calls["url_override"] == "http://fake-live-provider"


# ---- issue #2206 fix: the identity fence refuses the clerk POST hooks ------
#
# app/main.py installs FleetIdentityMiddleware(refuse_unpinned_mutations=True)
# on a clerk_agent role (#2075): every POST/PUT/PATCH/DELETE is refused
# before any handler or lane-capacity pool runs unless it proves itself as
# the coordinator's own forward (app.broker.fleet.delivery.lane_forward_is_authorized
# -- the coordinator token plus the X-Fleet-Broker/X-Fleet-Clerk-Id pin). A
# bespoke FastAPI app hosting only ``qualification_router`` (as the tests
# above do) never mounts this middleware, so it cannot see this failure --
# only a real ``app.main`` import can. Role gating happens once at Python
# import time (app/main.py reads FLEET_ROLE at module scope), so this runs
# in a fresh subprocess, the same reasoning as
# ``test_history_batch_operation.py::test_route_is_absent_outside_the_coordinator_role``.

_HOLD_COMMAND_IDENTITY_FENCE_PROBE = """
import asyncio
import json
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="qualification-hold-command-probe-")
os.environ.update({
    "FLEET_ROLE": "clerk_agent",
    "FLEET_DEPLOYMENT_NAMESPACE": "compose:fleetqualificationprobe",
    "FLEET_QUALIFICATION_PROBE_SECRET": "probe-secret",
    "FLEET_FAKE_BROKER_URL": "http://fake-broker:8011",
    "FLEET_FAKE_MARKET_DATA_URL": "http://fake-market:8013",
    "FLEET_QUALIFICATION_ACCOUNT_MODE": "paper",
    "FLEET_CLERK_ID": "clrk_probe",
    "FLEET_COORDINATOR_SERVICE_TOKEN": "coord-token",
    "FLEET_AGENT_SERVICE_TOKEN": "agent-token",
    "FLEET_COORDINATOR_URL": "http://127.0.0.1:1",
    "FLEET_MAX_INFLIGHT_REQUESTS": "1",
    "FLEET_MAX_INFLIGHT_STREAMS": "1",
    "FLEET_MAX_INFLIGHT_COMMANDS": "1",
    "FLEET_REQUEST_QUEUE_LIMIT": "0",
    "FLEET_REQUEST_QUEUE_TIMEOUT_MS": "0",
    "ALPACA_CLERK_DIR": tmp,
    "IBKR_LIVE_RUNS_ROOT": f"{tmp}/live_runs",
    "IBKR_LIVE_BARS_ROOT": f"{tmp}/live_bars",
    "BROKER_CAPTURE_DIR": f"{tmp}/captures",
    "POLYGON_API_KEY": "",
    "DATA_PLANE_CONTROL_SECRET": "",
    "TRUSTED_HOSTS": "localhost,127.0.0.1,test",
})

from httpx import ASGITransport, AsyncClient

import app.main as main
from scripts import run_broker_fleet_compose_qualification as harness


async def run():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
        unproven = await client.post(
            "/internal/fleet-qualification/hold/command",
            headers={"X-Fleet-Qualification-Secret": "probe-secret"},
        )
        # The exact headers the ceremony's in-container command probe sends.
        proven = await client.post(
            "/internal/fleet-qualification/hold/command",
            headers=harness._qualification_probe_headers("POST"),
        )
    return {
        "unproven_status": unproven.status_code,
        "unproven_body": unproven.json(),
        "proven_status": proven.status_code,
        "proven_body": proven.json(),
    }


sys.stdout.write(json.dumps(asyncio.run(run())))
"""


@pytest.mark.slow
def test_hold_command_requires_a_proven_coordinator_forward_through_the_real_middleware_stack() -> None:
    """Fails before the fix: the qualification secret alone used to be
    treated as sufficient by the harness's probe, but the real identity
    fence refuses an unproven POST with 400 ``broker_and_clerk_required``
    regardless of the qualification secret -- it runs before the qualification
    router's own handler ever sees the request. Sending the coordinator's
    proven-forward headers alongside the qualification secret -- built by
    ``run_broker_fleet_compose_qualification._qualification_probe_headers``,
    the same builder the ceremony's in-container command probe uses -- is
    admitted.
    """
    result = subprocess.run(
        [sys.executable, "-c", _HOLD_COMMAND_IDENTITY_FENCE_PROBE],
        cwd=_SERVICE_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    payload = json.loads(result.stdout)

    assert payload["unproven_status"] == 400
    assert payload["unproven_body"]["reason"] == "broker_and_clerk_required"
    assert payload["proven_status"] == 200
    assert payload["proven_body"] == {"held": "command"}

"""Guards for the Compose-only serving-lane qualification hook."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import cast

import pytest

import app.routers.fleet_qualification as qualification
from app.broker.alpaca.broker import AlpacaBroker
from app.routers.fleet_qualification import (
    QualificationHoldRequestResponse,
    QualificationMarketDependencyAvailable,
    QualificationMarketDependencyUnavailable,
    qualification_router,
)


def test_qualification_router_is_absent_without_each_strict_guard() -> None:
    """Normal, coordinator, and non-random namespaces cannot expose the hook."""
    assert qualification_router(role="combined", namespace="compose:fleetqualificationx", secret="s", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="host:local", secret="s", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="s", broker_url="http://fake", market_data_url="") is None


def test_qualification_router_has_no_mutation_or_provider_registration_route() -> None:
    """The opt-in surface is read-only observation and held ASGI work only."""
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
    }
    assert all("POST" not in route.methods for route in router.routes)


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

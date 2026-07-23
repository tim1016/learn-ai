"""Tests for the broker registry."""

from __future__ import annotations

import pytest

from app.broker.contract.errors import UnknownBrokerError
from app.broker.contract.registry import (
    BrokerRegistry,
    get_broker_registry,
    reset_broker_registry_for_testing,
)


class _FakePort:
    """Minimal stand-in — the registry only reads ``broker_id``."""

    def __init__(self, broker_id: str) -> None:
        self.broker_id = broker_id


def test_register_and_resolve() -> None:
    registry = BrokerRegistry()
    port = _FakePort("alpaca")

    registry.register(port)

    assert registry.resolve("alpaca") is port
    assert registry.registered_brokers() == ["alpaca"]


def test_resolve_unknown_broker_raises_with_detail() -> None:
    registry = BrokerRegistry()
    registry.register(_FakePort("alpaca"))

    with pytest.raises(UnknownBrokerError) as excinfo:
        registry.resolve("ibkr")

    error = excinfo.value
    assert error.broker == "ibkr"
    assert error.http_status == 404
    assert "alpaca" in (error.detail or "")


def test_resolve_on_empty_registry_names_none() -> None:
    with pytest.raises(UnknownBrokerError) as excinfo:
        BrokerRegistry().resolve("alpaca")

    assert "none" in (excinfo.value.detail or "")


def test_register_rebinds_same_id() -> None:
    registry = BrokerRegistry()
    first = _FakePort("alpaca")
    second = _FakePort("alpaca")

    registry.register(first)
    registry.register(second)

    assert registry.resolve("alpaca") is second
    assert registry.registered_brokers() == ["alpaca"]


def test_reset_clears_registrations() -> None:
    registry = BrokerRegistry()
    registry.register(_FakePort("alpaca"))

    registry.reset()

    assert registry.registered_brokers() == []


def test_process_singleton_is_stable_and_resettable() -> None:
    reset_broker_registry_for_testing()
    try:
        assert get_broker_registry() is get_broker_registry()
        get_broker_registry().register(_FakePort("alpaca"))
        assert get_broker_registry().registered_brokers() == ["alpaca"]

        reset_broker_registry_for_testing()
        assert get_broker_registry().registered_brokers() == []
    finally:
        reset_broker_registry_for_testing()

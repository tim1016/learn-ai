"""Tests for the broker registry."""

from __future__ import annotations

from app.broker.contract.registry import (
    BrokerRegistry,
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


def test_register_rebinds_same_id() -> None:
    registry = BrokerRegistry()
    first = _FakePort("alpaca")
    second = _FakePort("alpaca")

    registry.register(first)
    registry.register(second)

    assert registry.resolve("alpaca") is second
    assert registry.registered_brokers() == ["alpaca"]

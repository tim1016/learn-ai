"""Application-level composition of the production provider registry.

The fleet package stays broker-neutral by construction — no module under
``app/broker/fleet/`` imports a provider implementation (asserted by
``tests/broker/fleet/test_import_isolation.py``). This module is the one
place the application wires concrete adapters into the production mapping the
coordinator, the CLI and the agent registration path share. A provider enters
this mapping only by reviewed code change (PRD FR-001); delivery A2 adds
Alpaca and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet.provider import BrokerProviderAdapter, validate_operation_catalog


def production_provider_adapters() -> Mapping[str, BrokerProviderAdapter]:
    """The deployment's production adapters, validated at composition time.

    Validation runs here so a malformed catalog refuses at startup, before
    any surface can route against it — the catalog is the single contract
    (ADR 0062 addendum, item 4).
    """
    adapters: dict[str, BrokerProviderAdapter] = {
        "alpaca": AlpacaProviderAdapter(),
    }
    for provider_id, adapter in adapters.items():
        if adapter.provider_id != provider_id:
            raise ValueError(
                f"the adapter registered for {provider_id!r} declares "
                f"{adapter.provider_id!r}"
            )
        validate_operation_catalog(adapter.operations())
    return adapters


__all__ = ["production_provider_adapters"]

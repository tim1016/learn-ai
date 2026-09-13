"""The provider-adapter protocol and the code-owned production registry.

PRD §9.1: the fleet package defines a narrow protocol; the first production
adapter will be Alpaca (Phase 2). Test-only fakes are injectable through
constructor injection and never enter the production mapping. A provider
adapter declares its immutable provider ID, adapter version, canonical
account-ID function, typed capabilities and provider-authored summary shape;
the provider alone owns its clients, credentials, verification, commands,
custody, receipts, risk, arming, recovery and tests (FR-004).

The generic layer routes and verifies; it does not trade. It never translates
one provider's operation into another's, and it never infers that a capability
declared by one provider exists for another (FR-006).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.broker.fleet.errors import BrokerNotSupported


class Capability(StrEnum):
    """The closed typed operation vocabulary a provider adapter may declare.

    Capability is evidence: an action whose capability is not declared by the
    concrete provider is a typed refusal, never an emulation or a downgrade.
    The vocabulary is widened only here, in code review, never per request.
    """

    ACCOUNT_READ = "account_read"
    POSITIONS_READ = "positions_read"
    ORDERS_READ = "orders_read"
    CONFIGURATION_MANAGE = "configuration_manage"
    BOT_PANEL_READ = "bot_panel_read"
    BOT_ACTION = "bot_action"
    CUSTODY_READ = "custody_read"
    GALLERY_READ = "gallery_read"
    STREAM_SUBSCRIBE = "stream_subscribe"


@dataclass(frozen=True, slots=True)
class ServedContext:
    """The identity a routed operation must be checked against (PRD FR-075).

    The agent-side validation hook receives this; the coordinator-side checks
    (broker/clerk/epoch/generation) live in the control service. Kept in this
    package so provider adapters validate against one shared shape instead of
    inventing per-provider context types.
    """

    broker: str
    clerk_id: str
    agent_instance_id: str
    routing_epoch: int
    account_id: str | None
    capability: Capability
    effective_binding_generation: int


@runtime_checkable
class BrokerProviderAdapter(Protocol):
    """What a broker-owned clerk implementation presents to the fleet spine."""

    @property
    def provider_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    @property
    def route_catalog(self) -> frozenset[str]:
        """The route templates this provider's clerk serves (PRD FR-003).

        Broker- and clerk-scoped path templates (for example
        ``/api/brokers/{broker}/clerks/{clerk_id}/orders``), declared by the
        provider so the coordinator can verify a routed operation names a
        route the provider actually serves — the routing contract's analogue
        of capability evidence. Populated per provider as its Phase 2/3
        routes land; an empty catalog means the provider serves no routes yet.
        """
        ...

    def canonical_account_id(self, external_account_id: str) -> str:
        """Canonicalize one external account ID under this provider's rules.

        The provider alone defines canonicity (PRD FR-051); the registry treats
        the result as opaque. Two providers may canonicalize the same raw
        string differently, which is why assignment uniqueness is
        provider-qualified.
        """
        ...

    def provider_summary(self, observation: Mapping[str, object]) -> Mapping[str, object]:
        """Author the provider-typed summary for the directory.

        The returned mapping is the provider's own vocabulary (for Alpaca,
        endpoint mode and authority state, per PRD §10.1). The fleet directory
        does not interpret its financial contents; the observation carries only
        facts the registry already holds (reported state, account, binding
        generation).
        """
        ...

    def validate_served_context(self, context: ServedContext) -> None:
        """Refuse a served context this provider cannot honor (PRD FR-003).

        Provider-owned safety gates (for Alpaca: mode agreement, arming,
        envelope) answer here; the generic layer has already checked broker,
        clerk, epoch and capability.
        """
        ...


# The production registry is code-owned (PRD FR-001): a provider enters by a
# reviewed code change, never by configuration or request. It deliberately
# declares no providers in this slice — the Alpaca adapter lands as the PRD's
# Phase 2, and until then every production provider name fails closed.
PRODUCTION_PROVIDER_ADAPTERS: Mapping[str, BrokerProviderAdapter] = {}


def require_adapter(
    adapters: Mapping[str, BrokerProviderAdapter], provider_id: str
) -> BrokerProviderAdapter:
    """Resolve one adapter from a deployment-owned mapping, failing closed.

    The one refusal construction every lookup shares, so the coordinator's
    injected set and the production registry refuse identically.
    """
    adapter = adapters.get(provider_id)
    if adapter is None:
        raise BrokerNotSupported(
            f"No provider adapter is registered for {provider_id!r} in this deployment.",
            next_step="Use a provider this deployment supports; adding one is a "
            "reviewed code change, not a request parameter.",
        )
    return adapter


def production_adapter(provider_id: str) -> BrokerProviderAdapter:
    """Resolve a production adapter, failing closed on any other name."""
    return require_adapter(PRODUCTION_PROVIDER_ADAPTERS, provider_id)


__all__ = [
    "PRODUCTION_PROVIDER_ADAPTERS",
    "BrokerProviderAdapter",
    "Capability",
    "ServedContext",
    "production_adapter",
    "require_adapter",
]

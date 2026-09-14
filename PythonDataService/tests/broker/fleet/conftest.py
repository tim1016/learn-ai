"""Shared fixtures for the fleet spine suite.

The two test-only fake providers prove the extension boundary (PRD Phase 6 /
FR-002): they implement the adapter protocol, declare *different* capability
sets, canonicalize accounts *differently* (the headline boundary proof
alongside the capability split), and reach the service only through
constructor injection — never through the production registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    ProviderOperation,
    ServedContext,
)
from app.broker.fleet.records import StoredLifecycleState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore

FAKE_ALPHA_CAPABILITIES = frozenset(
    {
        Capability.ACCOUNT_READ,
        Capability.ORDERS_READ,
        Capability.BOT_ACTION,
    }
)
FAKE_BETA_CAPABILITIES = frozenset(
    {
        Capability.ACCOUNT_READ,
        Capability.GALLERY_READ,
        Capability.CONFIGURATION_MANAGE,
    }
)

_FAKE_ALPHA_OPERATIONS = frozenset(
    {
        ProviderOperation(
            operation_id="account_read",
            method="GET",
            path_template="/account",
            agent_path_template="/api/fake-alpha/account",
            capability=Capability.ACCOUNT_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="orders_read",
            method="GET",
            path_template="/orders",
            agent_path_template="/api/fake-alpha/orders",
            capability=Capability.ORDERS_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="bot_action",
            method="POST",
            path_template="/bots/{sid}/actions",
            agent_path_template="/api/fake-alpha/bots/{sid}/actions",
            capability=Capability.BOT_ACTION,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=True,
            idempotency=OperationIdempotency.DURABLE_KEY,
        ),
    }
)
_FAKE_BETA_OPERATIONS = frozenset(
    {
        ProviderOperation(
            operation_id="account_read",
            method="GET",
            path_template="/account",
            agent_path_template="/api/fake-beta/account",
            capability=Capability.ACCOUNT_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="gallery_stream",
            method="GET",
            path_template="/gallery/stream",
            agent_path_template="/api/fake-beta/gallery/stream",
            capability=Capability.GALLERY_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="configuration_apply",
            method="POST",
            path_template="/configuration/apply",
            agent_path_template="/api/fake-beta/configuration/apply",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=OperationReadiness.CONFIGURATION_ACCESS,
            requires_effective_account=False,
            idempotency=OperationIdempotency.ONE_SHOT,
        ),
    }
)


def _alpha_canonical_account_id(raw: str) -> str:
    """Alpha's canonical key: strip and upper-case."""
    return raw.strip().upper()


def _beta_canonical_account_id(raw: str) -> str:
    """Beta's canonical key: strip, lower-case, and fold ``-`` to ``_``.

    Deliberately different from alpha's in *shape* as well as case, so no
    case-insensitive comparison can collapse the two back together. A blank
    input still canonicalizes to the empty identity the service refuses
    (``ClerkAccountMismatch`` in ``reserve_assignment``,
    ``app/broker/fleet/service.py:726``), so the empty-canonical gate stays
    reachable for both providers.
    """
    return raw.strip().lower().replace("-", "_")


class FrozenClock:
    """A clock the tests advance explicitly; nothing here reads wall time."""

    def __init__(self, start_ms: int = 1_789_000_000_000) -> None:
        self._now = start_ms

    def __call__(self) -> int:
        """Return the frozen instant."""
        return self._now

    def advance(self, ms: int) -> int:
        """Move the frozen clock forward by ``ms``."""
        self._now += ms
        return self._now


@dataclass
class FakeProviderAdapter:
    """A minimal in-memory provider adapter owned by the tests, not the app."""

    provider_id: str
    capabilities: frozenset[Capability]
    declared_operations: frozenset[ProviderOperation]
    canonical_rule: Callable[[str], str]
    adapter_version: str = "test.1"
    refused_accounts: frozenset[str] = field(default_factory=frozenset)
    served_context_refusals: list[str] = field(default_factory=list)
    summaries: list[Mapping[str, object]] = field(default_factory=list)

    def operations(self) -> frozenset[ProviderOperation]:
        """The typed operation catalog this fake serves."""
        return self.declared_operations

    def canonical_account_id(self, external_account_id: str) -> str:
        if external_account_id.strip() in self.refused_accounts:
            raise LookupError(f"{self.provider_id} refuses account {external_account_id!r}")
        return self.canonical_rule(external_account_id)

    def provider_summary(self, observation: Mapping[str, object]) -> Mapping[str, object]:
        summary = {
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "observed_state": observation.get("reported_state"),
            "reported_summary": observation.get("reported_summary"),
        }
        self.summaries.append(summary)
        return summary

    def validate_served_context(self, context: ServedContext) -> None:
        if context.capability.value in self.served_context_refusals:
            raise LookupError(
                f"{self.provider_id} refuses to serve {context.capability.value}"
            )


def fake_alpha() -> FakeProviderAdapter:
    """Build the alpha fake provider with its declared capability set."""
    return FakeProviderAdapter(
        provider_id="fake_alpha",
        capabilities=FAKE_ALPHA_CAPABILITIES,
        declared_operations=_FAKE_ALPHA_OPERATIONS,
        canonical_rule=_alpha_canonical_account_id,
    )


def fake_beta() -> FakeProviderAdapter:
    """Build the beta fake provider with its declared capability set."""
    return FakeProviderAdapter(
        provider_id="fake_beta",
        capabilities=FAKE_BETA_CAPABILITIES,
        declared_operations=_FAKE_BETA_OPERATIONS,
        canonical_rule=_beta_canonical_account_id,
    )


@pytest.fixture
def clock() -> FrozenClock:
    """One frozen clock shared by a test's service constructions."""
    return FrozenClock()


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
    """One fresh coordinator control volume per test."""
    return tmp_path / "fleet-control"


@pytest.fixture
def fleet_service(
    control_dir: Path, clock: FrozenClock
) -> FleetControlService:
    """A coordinator over the two fake providers (the production set is empty)."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": fake_alpha(), "fake_beta": fake_beta()},
        clock=clock,
    )
    yield service
    service.close()


@dataclass
class Lane:
    """One provisioned fake lane: registry row, volume root and worker key."""

    clerk_id: str
    broker: str
    volume_root: Path
    worker_key: str
    attestation_id: str

    @property
    def lifecycle_state(self) -> str:
        return str(StoredLifecycleState.PROVISIONED)


def provision_lane(
    service: FleetControlService,
    *,
    broker: str,
    label: str,
    tmp_path: Path,
    attestation_id: str | None = None,
) -> Lane:
    """Provision one fake lane and return its identities and root."""
    volume_root = tmp_path / "volumes" / attestation_id if attestation_id else tmp_path / "volumes" / label
    volume_root.mkdir(parents=True, exist_ok=True)
    provisioned = service.provision_clerk(
        broker=broker,
        display_label=label,
        volume_root=volume_root,
        attestation_id=attestation_id or f"vol-{label}",
    )
    return Lane(
        clerk_id=provisioned.clerk.clerk_id,
        broker=broker,
        volume_root=volume_root,
        worker_key=provisioned.clerk.worker_key,
        attestation_id=provisioned.clerk.volume_attestation_id,
    )


def bind_lane(
    service: FleetControlService,
    lane: Lane,
    *,
    account: str,
    binding_generation: int = 1,
):
    """Register, reserve and confirm one lane, returning its session.

    The confirmation carries the registering session's instance and epoch —
    exactly what a real agent presents from its own registration.
    """
    session = service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    service.reserve_assignment(
        broker=lane.broker, clerk_id=lane.clerk_id, external_account_id=account
    )
    confirmed = service.confirm_assignment(
        broker=lane.broker,
        clerk_id=lane.clerk_id,
        external_account_id=account,
        binding_generation=binding_generation,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    return session, confirmed


__all__ = [
    "FAKE_ALPHA_CAPABILITIES",
    "FAKE_BETA_CAPABILITIES",
    "FakeProviderAdapter",
    "FrozenClock",
    "Lane",
    "bind_lane",
    "fake_alpha",
    "fake_beta",
    "provision_lane",
]

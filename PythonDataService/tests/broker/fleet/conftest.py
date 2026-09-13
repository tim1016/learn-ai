"""Shared fixtures for the fleet spine suite.

The two test-only fake providers prove the extension boundary (PRD Phase 6 /
FR-002): they implement the adapter protocol, declare *different* capability
sets, and reach the service only through constructor injection — never through
the production registry, which stays empty until the Alpaca adapter lands as
Phase 2.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.broker.fleet.provider import Capability, ServedContext
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
    }
)


class FrozenClock:
    """A clock the tests advance explicitly; nothing here reads wall time."""

    def __init__(self, start_ms: int = 1_789_000_000_000) -> None:
        self._now = start_ms

    def __call__(self) -> int:
        return self._now

    def advance(self, ms: int) -> int:
        self._now += ms
        return self._now


@dataclass
class FakeProviderAdapter:
    """A minimal in-memory provider adapter owned by the tests, not the app."""

    provider_id: str
    adapter_version: str = "test.1"
    capabilities: frozenset[Capability] = FAKE_ALPHA_CAPABILITIES
    route_catalog: frozenset[str] = frozenset(
        {"/api/brokers/{broker}/clerks/{clerk_id}/account"}
    )
    canonical_rule: Callable[[str], str] = lambda raw: raw.strip().upper()
    refused_accounts: frozenset[str] = field(default_factory=frozenset)
    served_context_refusals: list[str] = field(default_factory=list)
    summaries: list[Mapping[str, object]] = field(default_factory=list)

    def canonical_account_id(self, external_account_id: str) -> str:
        if external_account_id.strip() in self.refused_accounts:
            raise LookupError(f"{self.provider_id} refuses account {external_account_id!r}")
        return self.canonical_rule(external_account_id)

    def provider_summary(self, observation: Mapping[str, object]) -> Mapping[str, object]:
        summary = {
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "observed_state": observation.get("reported_state"),
        }
        self.summaries.append(summary)
        return summary

    def validate_served_context(self, context: ServedContext) -> None:
        if context.capability.value in self.served_context_refusals:
            raise LookupError(
                f"{self.provider_id} refuses to serve {context.capability.value}"
            )


def fake_alpha() -> FakeProviderAdapter:
    return FakeProviderAdapter(provider_id="fake_alpha", capabilities=FAKE_ALPHA_CAPABILITIES)


def fake_beta() -> FakeProviderAdapter:
    return FakeProviderAdapter(provider_id="fake_beta", capabilities=FAKE_BETA_CAPABILITIES)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
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


__all__ = [
    "FAKE_ALPHA_CAPABILITIES",
    "FAKE_BETA_CAPABILITIES",
    "FakeProviderAdapter",
    "FrozenClock",
    "Lane",
    "fake_alpha",
    "fake_beta",
    "provision_lane",
]

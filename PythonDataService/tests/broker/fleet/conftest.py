"""Shared fixtures for the fleet spine suite.

The two test-only fake providers prove the extension boundary (PRD Phase 6 /
FR-002): they implement the adapter protocol, declare *different* capability
sets, canonicalize accounts *differently* (the headline boundary proof
alongside the capability split), and reach the service only through
constructor injection — never through the production registry.
"""

from __future__ import annotations

import json
import sqlite3
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
from app.broker.fleet.records import AccountAssignmentRecord, StoredLifecycleState
from app.broker.fleet.recovery import (
    BACKUP_DATABASE_FILENAME,
    BACKUP_MANIFEST_FILENAME,
    D_COMPATIBLE_SCHEMA_VERSION,
    _sha256,
)
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
        # Operation ids deliberately differ from their capability's value
        # (e.g. "read_account" vs. "account_read") — a fixture whose id and
        # capability happen to read the same, as this one did before #2104's
        # capability-resolution follow-up, cannot catch a bug where one field
        # is substituted for the other.
        ProviderOperation(
            operation_id="read_account",
            method="GET",
            path_template="/account",
            agent_path_template="/api/fake-alpha/account",
            capability=Capability.ACCOUNT_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="read_orders",
            method="GET",
            path_template="/orders",
            agent_path_template="/api/fake-alpha/orders",
            capability=Capability.ORDERS_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="submit_bot_action",
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
            operation_id="read_account",
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


#: The bounded attribution every test ceremony carries (ADR 0063 Decision 4).
TEST_OPERATOR = "host-operator"
TEST_CHANGE_REF = "test-change-ref-0001"


def release_after_drain(
    service: FleetControlService,
    clock: FrozenClock,
    lane: Lane,
    *,
    account: str,
    expected_assignment_generation: int = 1,
    operator: str = TEST_OPERATOR,
    change_ref: str = TEST_CHANGE_REF,
) -> AccountAssignmentRecord:
    """Drain, wait out the deadline, then release under a bounded attribution.

    The ADR 0063 release ceremony's full order (§4.1): the drain closes the
    door, the deadline bounds the wait, and the release records the
    operator/change_ref the deleted proof token used to stand in for.
    """
    drained = service.drain_clerk(clerk_id=lane.clerk_id)
    assert drained.drain_deadline_at_ms is not None
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    return service.release_assignment(
        broker=lane.broker,
        external_account_id=account,
        expected_assignment_generation=expected_assignment_generation,
        operator=operator,
        change_ref=change_ref,
    )


def downgrade_backup_to_v2(backup_dir: Path) -> None:
    """Rewrite a fresh backup into the v2 shape it carried before the upgrade.

    A fresh backup carries every object the *current* ``schema.SCHEMA_VERSION``
    adds, not just v3's — so producing genuine pre-upgrade evidence means
    dropping v3's nested-root index and trigger, v4's audit indexes (#2133
    P2-a), and v5's drain-ceremony columns and triggers (ADR 0063). The v5
    columns cannot leave through ``ALTER TABLE DROP COLUMN`` — the fresh
    DDL's cross-column CHECKs reference them — so ``clerks`` and
    ``account_assignment_history`` are rebuilt without them, the v1→v2
    migration's own table-replace pattern, and the v2-era triggers and
    indexes are recreated on the rebuilt tables. Restamping the meta row is
    what a D-compatible rollback is for, without reconstructing the v2 DDL
    by hand.
    """
    database = backup_dir / BACKUP_DATABASE_FILENAME
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TRIGGER IF EXISTS trg_clerks_volume_root_not_nested")
        connection.execute("DROP INDEX IF EXISTS ux_clerks_volume_root")
        connection.execute("DROP INDEX IF EXISTS ix_routing_receipts_created_at")
        connection.execute("DROP INDEX IF EXISTS ix_routing_receipts_clerk_created_at")
        connection.execute("DROP INDEX IF EXISTS ix_routing_receipts_unsettled")
        connection.execute("DROP TABLE IF EXISTS force_retire_correlations")
        # Dropping the table takes its index and both append-only triggers
        # with it, which is the whole of v6.
        connection.execute("DROP TABLE IF EXISTS clerk_lane_confirmations")
        connection.execute(
            """CREATE TABLE clerks_v2 (
                clerk_id                TEXT PRIMARY KEY,
                broker                  TEXT NOT NULL CHECK (length(broker) > 0),
                worker_key              TEXT NOT NULL,
                display_label           TEXT NOT NULL CHECK (length(display_label) > 0),
                volume_id               TEXT NOT NULL,
                volume_root             TEXT NOT NULL CHECK (length(volume_root) > 0),
                deployment_namespace    TEXT NOT NULL CHECK (length(deployment_namespace) > 0),
                volume_attestation_kind TEXT NOT NULL CHECK (length(volume_attestation_kind) > 0),
                volume_attestation_id   TEXT NOT NULL CHECK (length(volume_attestation_id) > 0),
                lifecycle_state         TEXT NOT NULL CHECK (lifecycle_state IN ('provisioned', 'draining', 'retired')),
                created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= 253402300799999),
                retired_at_ms INTEGER CHECK (retired_at_ms IS NULL OR (retired_at_ms >= 0 AND retired_at_ms <= 253402300799999)),
                CHECK ((retired_at_ms IS NULL) = (lifecycle_state <> 'retired'))
            )"""
        )
        connection.execute(
            "INSERT INTO clerks_v2 (clerk_id, broker, worker_key, display_label, "
            "volume_id, volume_root, deployment_namespace, volume_attestation_kind, "
            "volume_attestation_id, lifecycle_state, created_at_ms, retired_at_ms) "
            "SELECT clerk_id, broker, worker_key, display_label, volume_id, "
            "volume_root, deployment_namespace, volume_attestation_kind, "
            "volume_attestation_id, lifecycle_state, created_at_ms, retired_at_ms "
            "FROM clerks"
        )
        connection.execute("DROP TABLE clerks")
        connection.execute("ALTER TABLE clerks_v2 RENAME TO clerks")
        connection.execute(
            "CREATE UNIQUE INDEX ux_clerks_worker_key ON clerks(worker_key) "
            "WHERE lifecycle_state <> 'retired'"
        )
        connection.execute(
            "CREATE UNIQUE INDEX ux_clerks_volume_id ON clerks(volume_id) "
            "WHERE lifecycle_state <> 'retired'"
        )
        connection.execute(
            "CREATE UNIQUE INDEX ux_clerks_volume_attestation ON clerks"
            "(deployment_namespace, volume_attestation_kind, volume_attestation_id) "
            "WHERE lifecycle_state <> 'retired'"
        )
        connection.execute(
            "CREATE INDEX ix_clerks_listing ON clerks(broker, created_at_ms, clerk_id)"
        )
        connection.execute(
            """CREATE TRIGGER trg_clerks_identity_immutable
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.worker_key IS NOT NEW.worker_key
    OR OLD.volume_id IS NOT NEW.volume_id
    OR OLD.volume_root IS NOT NEW.volume_root
    OR OLD.deployment_namespace IS NOT NEW.deployment_namespace
    OR OLD.volume_attestation_kind IS NOT NEW.volume_attestation_kind
    OR OLD.volume_attestation_id IS NOT NEW.volume_attestation_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a clerk identity is written once at provisioning and never changes');
END"""
        )
        connection.execute(
            """CREATE TRIGGER trg_clerks_no_delete
BEFORE DELETE ON clerks
BEGIN
    SELECT RAISE(ABORT, 'a clerk is retired, never deleted; IDs are not recycled');
END"""
        )
        connection.execute(
            """CREATE TRIGGER trg_clerks_lifecycle_forward
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'provisioned' AND OLD.retired_at_ms IS NOT NEW.retired_at_ms)
    OR (OLD.lifecycle_state = 'draining' AND NEW.lifecycle_state NOT IN ('draining', 'retired'))
    OR (OLD.lifecycle_state = 'retired' AND NEW.lifecycle_state <> 'retired')
BEGIN
    SELECT RAISE(ABORT, 'a clerk lifecycle moves forward only');
END"""
        )
        connection.execute(
            """CREATE TABLE account_assignment_history_v2 (
                broker                          TEXT NOT NULL,
                canonical_external_account_id   TEXT NOT NULL,
                clerk_id                        TEXT NOT NULL,
                assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
                state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
                effective_profile_id            TEXT,
                effective_revision              INTEGER,
                recorded_at_ms                  INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= 253402300799999),
                updated_at_ms                   INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= 253402300799999),
                PRIMARY KEY (broker, canonical_external_account_id, assignment_generation, state)
            )"""
        )
        connection.execute(
            "INSERT INTO account_assignment_history_v2 (broker, "
            "canonical_external_account_id, clerk_id, assignment_generation, state, "
            "effective_profile_id, effective_revision, recorded_at_ms, updated_at_ms) "
            "SELECT broker, canonical_external_account_id, clerk_id, "
            "assignment_generation, state, effective_profile_id, effective_revision, "
            "recorded_at_ms, updated_at_ms FROM account_assignment_history"
        )
        connection.execute("DROP TABLE account_assignment_history")
        connection.execute(
            "ALTER TABLE account_assignment_history_v2 RENAME TO account_assignment_history"
        )
        connection.execute(
            """CREATE TRIGGER trg_account_assignment_history_immutable
BEFORE UPDATE ON account_assignment_history
BEGIN
    SELECT RAISE(ABORT, 'assignment history is append-only');
END"""
        )
        connection.execute(
            """CREATE TRIGGER trg_account_assignment_history_no_delete
BEFORE DELETE ON account_assignment_history
BEGIN
    SELECT RAISE(ABORT, 'assignment history is append-only');
END"""
        )
        connection.execute(
            "UPDATE fleet_meta SET schema_version = ? WHERE id = 1",
            (D_COMPATIBLE_SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()
    manifest_path = backup_dir / BACKUP_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["registry_schema_version"] = D_COMPATIBLE_SCHEMA_VERSION
    manifest["database_sha256"] = _sha256(database)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


__all__ = [
    "FAKE_ALPHA_CAPABILITIES",
    "FAKE_BETA_CAPABILITIES",
    "TEST_CHANGE_REF",
    "TEST_OPERATOR",
    "FakeProviderAdapter",
    "FrozenClock",
    "Lane",
    "bind_lane",
    "downgrade_backup_to_v2",
    "fake_alpha",
    "fake_beta",
    "provision_lane",
    "release_after_drain",
]

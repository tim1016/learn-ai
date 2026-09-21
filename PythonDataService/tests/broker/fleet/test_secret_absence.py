"""Secret material appears in no sink a full ceremony touches — asserted, not assumed.

The spine never sees a credential (resolution is a Phase 2 code-owned
mapping). An environment-variable canary is not a test: ``app/broker/fleet/**``
reads no environment variable, so the assertion can only ever pass. What is
worth pinning is the negative space around values the ceremony genuinely
handles — the worker key and the two service tokens minted at provisioning —
swept across every durable and observable sink one full ceremony writes: the
registry (its WAL/SHM), the broker-neutral directory payload, the debug log,
the volume identity marker, the clerk's confirmation-evidence checkpoint, the
routing-receipt projection, and a registry backup's database and manifest.
The worker key is durable registry identity, so it *is* stored there (and in
a registry backup, which is a full logical copy of those same rows, not a
byte-identical file copy) — it never crosses any of the other six sinks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    confirmation_evidence_path,
    write_confirmation_evidence,
)
from app.broker.fleet.records import RoutingReceiptRecord, RoutingReceiptState
from app.broker.fleet.recovery import (
    BACKUP_DATABASE_FILENAME,
    BACKUP_MANIFEST_FILENAME,
    create_registry_backup,
)
from app.broker.fleet.store import registry_database_path
from app.broker.fleet.volume import marker_path
from app.utils.timestamps import now_ms_utc
from tests.broker.fleet.conftest import (
    FrozenClock,
    Lane,
    provision_lane,
    release_after_drain,
)


@dataclass(frozen=True, slots=True)
class _CeremonyArtifacts:
    """Every durable/observable artifact one full ceremony produced."""

    lane: Lane
    payload: dict[str, object]
    receipts: list[RoutingReceiptRecord]
    backup_dir: Path


def _drive_ceremonies(fleet_service, clock: FrozenClock, tmp_path: Path) -> _CeremonyArtifacts:
    """Drive every ceremony once; return every durable/observable artifact."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="secrets-check", tmp_path=tmp_path
    )
    session = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-secret"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-secret",
        binding_generation=1,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    attempt = fleet_service.open_routing_attempt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-secret",
        idempotency_key="idem-secret",
        pinned_routing_epoch=session.routing_epoch,
        pinned_binding_generation=1,
        pinned_agent_instance_id=session.agent_instance_id,
    )
    fleet_service.mark_routing_dispatched(correlation_id=attempt.correlation_id)
    fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="upstream/r-1",
    )
    secret_assignment = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-SECRET"
    )
    assert secret_assignment is not None

    # The clerk-side confirmation checkpoint (audit 2026-09-13, finding 2) is
    # written by the agent's own boot path, not by the coordinator's
    # registry — drive it directly so this sink is real, not assumed empty.
    clerk = fleet_service._store.read_clerk(lane.clerk_id)
    assert clerk is not None
    write_confirmation_evidence(
        lane.volume_root,
        ConfirmationEvidence(
            clerk_id=lane.clerk_id,
            volume_id=clerk.volume_id,
            registry_id=fleet_service._store.registry_id,
            assignment_generation=secret_assignment.assignment_generation,
            canonical_account_id=secret_assignment.canonical_external_account_id,
            binding_generation=1,
            effective_profile_id=None,
            effective_revision=None,
            confirmed_at_ms=now_ms_utc(),
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
        ),
    )
    receipts = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id)

    release_after_drain(fleet_service, clock, lane, account="acct-secret")
    payload = fleet_service.directory(include_retired=True)

    backup_dir = tmp_path / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup_dir)

    return _CeremonyArtifacts(
        lane=lane, payload=payload, receipts=receipts, backup_dir=backup_dir
    )


def test_no_secret_reaches_any_sink_the_ceremony_touches(
    control_dir: Path,
    clock: FrozenClock,
    fleet_service,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker key and the minted service tokens reach no non-registry sink.

    An environment-variable canary is not a test: ``app/broker/fleet/**``
    reads no environment variable, so the assertion can only ever pass. What
    is worth pinning is the negative space around values the ceremony
    genuinely handles.
    """
    with caplog.at_level(logging.DEBUG):
        artifacts = _drive_ceremonies(fleet_service, clock, control_dir.parent)

    registry_bytes = registry_database_path(control_dir).read_bytes()
    for wal_suffix in ("-wal", "-shm"):
        wal = registry_database_path(control_dir).with_name(
            registry_database_path(control_dir).name + wal_suffix
        )
        if wal.exists():
            registry_bytes += wal.read_bytes()
    logged = "\n".join(
        record.getMessage() + str(record.__dict__) for record in caplog.records
    )
    sinks = {
        "registry": registry_bytes,
        "directory": repr(artifacts.payload).encode("utf-8"),
        "logs": logged.encode("utf-8"),
        "volume_marker": marker_path(artifacts.lane.volume_root).read_bytes(),
        "confirmation_evidence": confirmation_evidence_path(
            artifacts.lane.volume_root
        ).read_bytes(),
        "routing_receipts": repr(artifacts.receipts).encode("utf-8"),
        "backup_database": (artifacts.backup_dir / BACKUP_DATABASE_FILENAME).read_bytes(),
        "backup_manifest": (artifacts.backup_dir / BACKUP_MANIFEST_FILENAME).read_bytes(),
    }

    # Positive control: every assertion above is a negative, and a sink that
    # silently degrades to empty or trivial content would satisfy every one
    # of them by accident — the same defect class as the env canary this
    # test replaces. Prove each sink actually carries the ceremony's
    # distinctive, nonsecret content before trusting its absence checks.
    assert artifacts.receipts, "the ceremony settled at least one routing receipt"
    assert caplog.records, "the ceremony logged at least one record"
    assert b"secrets-check" in sinks["directory"], "the label reaches the directory"
    assert b"secrets-check" in sinks["volume_marker"], "the label reaches the marker"
    assert b"secrets-check" in sinks["backup_database"], "the label reaches the backup"
    assert b"strategy/sid-secret" in sinks["routing_receipts"], (
        "the receipt sink carries operator content"
    )
    assert b"strategy/sid-secret" in sinks["backup_database"], (
        "the target ref reaches the backup"
    )

    for name, blob in sinks.items():
        assert b"svct_" not in blob, name
        if name in ("registry", "backup_database"):
            continue
        assert artifacts.lane.worker_key.encode("utf-8") not in blob, name
    # The exclusion above is not an assumption: prove the worker key really
    # is in the backup, the way it is in the live registry (already proven
    # by ``test_the_worker_key_is_stored_but_never_projected``) — an empty
    # or truncated backup file would otherwise satisfy the skipped check for
    # free. A registry backup is a full logical copy of the same rows the
    # live database holds (``store.backup_to`` uses SQLite's own page-level
    # backup API, not a byte-identical file copy), so the worker key is
    # durable registry identity in both, by design.
    assert artifacts.lane.worker_key.encode("utf-8") in sinks["backup_database"]


def test_the_worker_key_is_stored_but_never_projected(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """The worker key lives only in the registry, never in a projection."""
    artifacts = _drive_ceremonies(fleet_service, clock, control_dir.parent)
    db_path = registry_database_path(control_dir)
    registry_bytes = db_path.read_bytes()
    for wal_suffix in ("-wal", "-shm"):
        wal = db_path.with_name(db_path.name + wal_suffix)
        if wal.exists():
            registry_bytes += wal.read_bytes()
    # The durable identity is in the registry and only there.
    entries = artifacts.payload["clerks"]
    assert entries, "the ceremony provisioned at least one clerk"
    for entry in entries:
        rendered_entry = repr(entry)
        assert "worker_key" not in rendered_entry
        assert "wkrk_" not in rendered_entry
    assert b"wkrk_" in registry_bytes


def test_transport_service_tokens_are_stored_nowhere(
    control_dir: Path, fleet_service
) -> None:
    """Audit 2026-09-13, finding 3: the environment-only transport tokens minted
    at provisioning reach no registry byte and no projection."""
    volume_root = control_dir.parent / "volumes" / "tokens"
    volume_root.mkdir(parents=True, exist_ok=True)
    provisioned = fleet_service.provision_clerk(
        broker="fake_alpha",
        display_label="tokens",
        volume_root=volume_root,
    )

    db_path = registry_database_path(control_dir)
    registry_bytes = db_path.read_bytes()
    for wal_suffix in ("-wal", "-shm"):
        wal = db_path.with_name(db_path.name + wal_suffix)
        if wal.exists():
            registry_bytes += wal.read_bytes()

    assert provisioned.agent_service_token.startswith("svct_")
    assert provisioned.coordinator_service_token.startswith("svct_")
    assert provisioned.agent_service_token != provisioned.coordinator_service_token
    for token in (provisioned.agent_service_token, provisioned.coordinator_service_token):
        assert token.encode("utf-8") not in registry_bytes
    assert b"svct_" not in registry_bytes
    directory = fleet_service.directory()
    assert "svct_" not in repr(directory)

"""The fleet control service: the coordinator's rules surface.

One seam for provisioning, volume verification, session registration,
broker-qualified assignment fencing, routing receipts and the broker-neutral
directory. The refusals this service raises are PRD §10.4's stable families;
storage lives in ``store.py``, identity minting in ``identity.py``, and
provider declarations in ``provider.py``.

The load-bearing invariants, each with a test:

- Assignment ownership never expires. Nothing in this service consults
  heartbeat age before refusing a rival reservation (PRD FR-054); liveness
  only projects ``unreachable`` in the directory.
- Volume identity is proven before authority: registration and reservation
  both verify the marker against the registry row (PRD FR-025/026).
- The directory computes no financial facts (PRD FR-034) and never exposes
  ``worker_key`` (FR-012).
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.broker.fleet import volume as volume_module
from app.broker.fleet.errors import (
    BrokerAndClerkRequired,
    BrokerClerkCapabilityUnavailable,
    ClerkAccountMismatch,
    ClerkAssignmentConflict,
    ClerkBrokerMismatch,
    ClerkIdentityMismatch,
    ClerkNotFound,
    ClerkVolumeAlreadyRegistered,
    ClerkVolumeCloneDetected,
)
from app.broker.fleet.identity import (
    is_clerk_id,
    new_agent_instance_id,
    new_clerk_id,
    new_correlation_id,
    new_volume_id,
    new_worker_key,
)
from app.broker.fleet.provider import (
    PRODUCTION_PROVIDER_ADAPTERS,
    BrokerProviderAdapter,
    Capability,
)
from app.broker.fleet.records import (
    AccountAssignmentRecord,
    AssignmentState,
    ClerkDescriptor,
    ClerkLifecycleState,
    ClerkRecord,
    ClerkSessionRecord,
    RoutingReceiptRecord,
    RoutingReceiptState,
    StoredLifecycleState,
    VolumeMarker,
)
from app.broker.fleet.store import FleetRegistryStore
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: How stale a heartbeat may be before the directory projects ``unreachable``.
#: A projection only: it never releases, downgrades or transfers anything.
DEFAULT_SESSION_STALE_AFTER_MS = 30_000

_COMPOSE_NAMED_VOLUME = "compose_named_volume"
ATTESTATION_KINDS = frozenset({_COMPOSE_NAMED_VOLUME})


@dataclass(frozen=True, slots=True)
class ProvisionedClerk:
    """What a host ceremony gets back: identities to wire into deployment."""

    clerk: ClerkRecord
    marker: VolumeMarker


class FleetControlService:
    """The coordinator's one rules surface over the registry store."""

    def __init__(
        self,
        *,
        store: FleetRegistryStore,
        provider_adapters: Mapping[str, BrokerProviderAdapter] | None = None,
        clock: Callable[[], int] = now_ms_utc,
        session_stale_after_ms: int = DEFAULT_SESSION_STALE_AFTER_MS,
    ) -> None:
        self._store = store
        # Constructor injection is the extension boundary (PRD FR-002): a
        # test-only fake adapter is passed here and never enters the
        # code-owned production mapping.
        self._provider_adapters: Mapping[str, BrokerProviderAdapter] = (
            provider_adapters if provider_adapters is not None else PRODUCTION_PROVIDER_ADAPTERS
        )
        self._clock = clock
        self._session_stale_after_ms = session_stale_after_ms

    def close(self) -> None:
        self._store.close()

    # ---- provider adapters ----------------------------------------------

    def _adapter(self, broker: str) -> BrokerProviderAdapter:
        adapter = self._provider_adapters.get(broker)
        if adapter is None:
            from app.broker.fleet.errors import BrokerNotSupported

            raise BrokerNotSupported(
                f"No provider adapter is registered for {broker!r} in this deployment.",
                next_step="Use a provider this deployment supports; adding one is a "
                "reviewed code change, not a request parameter.",
            )
        return adapter

    def require_capability(self, *, broker: str, capability: Capability) -> None:
        """Refuse an operation the provider does not declare (PRD FR-006)."""
        adapter = self._adapter(broker)
        if capability not in adapter.capabilities:
            raise BrokerClerkCapabilityUnavailable(
                f"Provider {broker!r} does not declare the capability "
                f"{capability.value!r}.",
                next_step="The provider's own surface defines what it supports; "
                "unsupported actions are unavailable, never emulated.",
            )

    # ---- provisioning (host ceremony) ------------------------------------

    def provision_clerk(
        self,
        *,
        broker: str,
        display_label: str,
        volume_root: Path,
        attestation_kind: str = _COMPOSE_NAMED_VOLUME,
        attestation_id: str | None = None,
    ) -> ProvisionedClerk:
        """Create one clerk lane: mint identities, mark the volume, register.

        The attestation for a compose named volume is the volume's name —
        deployment-owned and nonsecret. The root must be a fresh canonical
        mount; a root that already carries a marker is a copied or re-mounted
        volume and refuses (PRD FR-026).
        """
        if not broker or not display_label:
            raise BrokerAndClerkRequired(
                "Provisioning requires a broker and a display label.",
            )
        self._adapter(broker)  # unknown production provider fails closed here
        if attestation_kind not in ATTESTATION_KINDS:
            raise ClerkVolumeCloneDetected(
                f"Unknown volume attestation kind {attestation_kind!r}.",
                next_step=f"Use one of {sorted(ATTESTATION_KINDS)}.",
            )
        if attestation_id is None:
            # The compose named-volume identity is the attestation itself.
            attestation_id = volume_root.name
        if not attestation_id:
            raise ClerkVolumeCloneDetected(
                "A volume attestation must not be empty.",
            )

        now = self._clock()
        volume_module.resolve_canonical_root(volume_root)
        existing_marker = volume_module.read_volume_marker(volume_root)
        if existing_marker is not None:
            raise ClerkVolumeCloneDetected(
                f"The root {volume_root} already carries the identity marker of "
                f"clerk {existing_marker.clerk_id}; provisioning writes a fresh "
                "volume, never a second identity onto an existing one.",
                next_step="Use a fresh named volume for the new clerk.",
            )

        clerk_id = new_clerk_id()
        volume_id = new_volume_id()
        worker_key = new_worker_key()
        marker = VolumeMarker(
            marker_version=volume_module.MARKER_SCHEMA_VERSION,
            broker=broker,
            clerk_id=clerk_id,
            volume_id=volume_id,
            attestation_kind=attestation_kind,
            attestation_id=attestation_id,
            created_at_ms=now,
        )

        # Registry first under the write lock: a duplicate attestation or
        # volume identity for an active clerk refuses here, before the volume
        # is marked, so a failed provisioning leaves no half-marked root.
        if self._store.find_clerk_by_attestation(
            attestation_kind=attestation_kind, attestation_id=attestation_id
        ) is not None:
            raise ClerkVolumeAlreadyRegistered(
                f"Another active clerk already attests volume "
                f"{attestation_kind}:{attestation_id}.",
                next_step="Each clerk owns a distinct physical volume; mount a "
                "fresh one for the new clerk.",
            )
        if self._store.find_clerk_by_volume(volume_id) is not None:
            raise ClerkVolumeCloneDetected(
                f"Volume identity {volume_id} is already registered to an active clerk.",
            )
        record = ClerkRecord(
            clerk_id=clerk_id,
            broker=broker,
            worker_key=worker_key,
            display_label=display_label,
            volume_id=volume_id,
            volume_attestation_kind=attestation_kind,
            volume_attestation_id=attestation_id,
            lifecycle_state=StoredLifecycleState.PROVISIONED,
            created_at_ms=now,
            retired_at_ms=None,
        )
        with self._store.transaction() as conn:
            self._store.insert_clerk(conn, record)
        volume_module.write_volume_marker(volume_root, marker)
        logger.info(
            "fleet clerk provisioned",
            extra={
                "broker": broker,
                "clerk_id": clerk_id,
                "volume_id": volume_id,
                "attestation_kind": attestation_kind,
            },
        )
        return ProvisionedClerk(clerk=record, marker=marker)

    def verify_clerk_volume(self, *, clerk_id: str, volume_root: Path) -> VolumeMarker:
        """Re-run the fail-before-authority gate against the registry."""
        clerk = self._require_clerk(clerk_id)
        return self._verify_volume(clerk, volume_root)

    def _verify_volume(self, clerk: ClerkRecord, volume_root: Path) -> VolumeMarker:
        marker = volume_module.verify_volume_identity(
            volume_root,
            expected_broker=clerk.broker,
            expected_clerk_id=clerk.clerk_id,
            expected_volume_id=clerk.volume_id,
            expected_attestation_kind=clerk.volume_attestation_kind,
            expected_attestation_id=clerk.volume_attestation_id,
        )
        # A copied root can carry a *valid* marker for an active clerk while a
        # different physical volume already registers the same identity.
        owner = self._store.find_clerk_by_volume(marker.volume_id)
        if owner is not None and owner.clerk_id != clerk.clerk_id:
            raise ClerkVolumeCloneDetected(
                f"Volume identity {marker.volume_id} is registered to clerk "
                f"{owner.clerk_id}, not {clerk.clerk_id}.",
                next_step="Mount each clerk's own named volume; a copied root is "
                "not a second lane.",
            )
        return marker

    def retire_clerk(self, *, clerk_id: str) -> ClerkRecord:
        """Retirement is terminal; IDs are never recycled (PRD FR-013).

        Refuses while the clerk still holds an effective assignment — the
        obligations the assignment represents must be resolved by the release
        ceremony first, not silently orphaned.
        """
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            return clerk
        held = [
            assignment
            for assignment in self._store.list_assignments_for_clerk(clerk_id)
            if assignment.state != AssignmentState.RELEASED
        ]
        if held:
            raise ClerkAssignmentConflict(
                f"Clerk {clerk_id} still holds {len(held)} account assignment(s); "
                "retirement requires the release ceremony to prove obligations "
                "clear first.",
                next_step="Run the host release ceremony for each assigned account, "
                "then retire.",
            )
        now = self._clock()
        updated = False
        with self._store.transaction() as conn:
            updated = self._store.update_clerk_lifecycle(
                conn,
                clerk_id=clerk_id,
                lifecycle_state=StoredLifecycleState.RETIRED,
                retired_at_ms=now,
            )
        if not updated:
            raise ClerkNotFound(f"No clerk carries identity {clerk_id!r}.")
        logger.info("fleet clerk retired", extra={"broker": clerk.broker, "clerk_id": clerk_id})
        retired = self._store.read_clerk(clerk_id)
        assert retired is not None
        return retired

    # ---- agent sessions ---------------------------------------------------

    def register_agent_session(
        self,
        *,
        clerk_id: str,
        worker_key: str,
        agent_instance_id: str | None = None,
        volume_root: Path | None = None,
    ) -> ClerkSessionRecord:
        """Install the clerk's one current agent session, bumping the epoch.

        The worker key is compared with ``hmac.compare_digest``. A restart of
        the same clerk presents a new instance id and supersedes the previous
        session under a higher epoch (FR-065's idempotent same-clerk
        recovery); the archived session keeps the epoch history auditable.
        Retirement refuses registration — routing to a retired clerk is gone
        for good.
        """
        clerk = self._require_clerk(clerk_id)
        if not hmac.compare_digest(clerk.worker_key, worker_key):
            raise ClerkIdentityMismatch(
                f"The worker key presented for clerk {clerk_id} does not match its "
                "registry identity.",
                next_step="Present the worker key issued to this clerk at provisioning.",
            )
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired; a retired lane never returns to service.",
            )
        if volume_root is not None:
            self._verify_volume(clerk, volume_root)
        now = self._clock()
        instance = agent_instance_id if agent_instance_id is not None else new_agent_instance_id()
        current = self._store.read_session(clerk_id)
        if (
            current is not None
            and current.agent_instance_id == instance
            and current.broker == clerk.broker
        ):
            # Idempotent re-registration of the same instance: refresh only.
            refreshed = ClerkSessionRecord(
                broker=clerk.broker,
                clerk_id=clerk_id,
                agent_instance_id=instance,
                routing_epoch=current.routing_epoch,
                started_at_ms=current.started_at_ms,
                last_seen_at_ms=now,
                reported_binding_generation=current.reported_binding_generation,
                reported_account_id=current.reported_account_id,
                reported_state=current.reported_state,
            )
            with self._store.transaction() as conn:
                self._store.upsert_session(conn, refreshed)
            return refreshed

        epoch = 1 if current is None else current.routing_epoch + 1
        session = ClerkSessionRecord(
            broker=clerk.broker,
            clerk_id=clerk_id,
            agent_instance_id=instance,
            routing_epoch=epoch,
            started_at_ms=now,
            last_seen_at_ms=now,
        )
        with self._store.transaction() as conn:
            if current is not None:
                self._store.archive_session(conn, current, superseded_at_ms=now)
            self._store.upsert_session(conn, session)
        logger.info(
            "fleet agent session registered",
            extra={
                "broker": clerk.broker,
                "clerk_id": clerk_id,
                "agent_instance_id": instance,
                "routing_epoch": epoch,
            },
        )
        return session

    def observe_session(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        reported_binding_generation: int | None = None,
        reported_account_id: str | None = None,
        reported_state: str | None = None,
    ) -> bool:
        """Record a heartbeat and the worker's observed binding facts.

        An observation updates liveness projections only; it can never move
        an assignment, a lifecycle state, or ownership of anything.
        """
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(f"Clerk {clerk_id} is retired.")
        touched = False
        with self._store.transaction() as conn:
            touched = self._store.touch_session(
                conn,
                clerk_id=clerk_id,
                agent_instance_id=agent_instance_id,
                last_seen_at_ms=self._clock(),
                reported_binding_generation=reported_binding_generation,
                reported_account_id=reported_account_id,
                reported_state=reported_state,
            )
        return touched

    # ---- broker-qualified account assignments -----------------------------

    def reserve_assignment(
        self,
        *,
        broker: str,
        clerk_id: str,
        external_account_id: str,
        volume_root: Path | None = None,
    ) -> AccountAssignmentRecord:
        """Reserve ``(broker, canonical account)`` for one clerk (PRD §9.6).

        The provider adapter alone canonicalizes the external ID; the key is
        provider-qualified, so the same raw string under another provider is a
        different reservation. A rival active reservation or assignment
        refuses — *regardless of heartbeat age*: liveness never transfers
        ownership (FR-054). Re-reserving for the same owner is idempotent.
        """
        if not broker:
            raise BrokerAndClerkRequired("A reservation requires a broker.")
        clerk = self._require_clerk(clerk_id)
        if clerk.broker != broker:
            raise ClerkBrokerMismatch(
                f"Clerk {clerk_id} belongs to broker {clerk.broker!r}, not {broker!r}.",
            )
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            # A retired clerk's routes are gone for good: to the fleet it is
            # simply absent, not a conflict to resolve.
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired; only a provisioned clerk reserves accounts.",
            )
        if clerk.lifecycle_state != StoredLifecycleState.PROVISIONED:
            raise ClerkAssignmentConflict(
                f"Clerk {clerk_id} is {clerk.lifecycle_state.value}; only a "
                "provisioned clerk reserves accounts.",
            )
        adapter = self._adapter(broker)
        canonical = adapter.canonical_account_id(external_account_id)
        if not canonical:
            raise ClerkAccountMismatch(
                f"Provider {broker!r} canonicalized account {external_account_id!r} "
                "to an empty identity.",
            )
        if volume_root is not None:
            self._verify_volume(clerk, volume_root)

        now = self._clock()
        existing = self._store.read_assignment(broker=broker, canonical_account_id=canonical)
        if existing is None:
            assignment = AccountAssignmentRecord(
                broker=broker,
                canonical_external_account_id=canonical,
                clerk_id=clerk_id,
                assignment_generation=1,
                state=AssignmentState.RESERVED,
                recorded_at_ms=now,
                updated_at_ms=now,
            )
            try:
                with self._store.transaction() as conn:
                    self._store.insert_assignment(conn, assignment)
            except sqlite3.IntegrityError as exc:
                # Another writer's reservation committed between this read and
                # insert; the constraint is the fence, and the loser gets the
                # same typed refusal a sequential rival would.
                raise ClerkAssignmentConflict(
                    f"Account {canonical} under broker {broker!r} was reserved "
                    "concurrently by another clerk.",
                    next_step="Assignment ownership does not expire; reassignment "
                    "is a proof-driven host ceremony.",
                ) from exc
            logger.info(
                "fleet account assignment reserved",
                extra={"broker": broker, "clerk_id": clerk_id},
            )
            return assignment
        if existing.clerk_id == clerk_id and existing.state == AssignmentState.RESERVED:
            return existing
        if existing.state == AssignmentState.RELEASED:
            # Reuse after the release ceremony: a fresh reservation at a
            # higher generation, compare-and-swapped so a racing third writer
            # cannot also land.
            reasserted = AccountAssignmentRecord(
                broker=broker,
                canonical_external_account_id=canonical,
                clerk_id=clerk_id,
                assignment_generation=existing.assignment_generation + 1,
                state=AssignmentState.RESERVED,
                recorded_at_ms=now,
                updated_at_ms=now,
            )
            accepted = False
            with self._store.transaction() as conn:
                accepted = self._store.cas_update_assignment(
                    conn, reasserted, previous_generation=existing.assignment_generation
                )
            if not accepted:
                raise ClerkAssignmentConflict(
                    f"Account {canonical} under broker {broker!r} changed while "
                    "re-reserving; re-read and retry.",
                )
            logger.info(
                "fleet account assignment re-reserved",
                extra={"broker": broker, "clerk_id": clerk_id},
            )
            return reasserted
        raise ClerkAssignmentConflict(
            f"Account {canonical} under broker {broker!r} is already "
            f"{existing.state.value} for clerk {existing.clerk_id}.",
            next_step="Assignment ownership does not expire; reassignment is a "
            "proof-driven host ceremony.",
        )

    def confirm_assignment(
        self,
        *,
        broker: str,
        clerk_id: str,
        external_account_id: str,
        binding_generation: int,
        effective_profile_id: str | None = None,
        effective_revision: int | None = None,
    ) -> AccountAssignmentRecord:
        """Move this clerk's reservation to effective (PRD FR-064).

        The worker calls this after acknowledging its local binding; the
        generation and profile facts recorded here are what a routed command
        is later checked against.
        """
        clerk = self._require_clerk(clerk_id)
        if clerk.broker != broker:
            raise ClerkBrokerMismatch(
                f"Clerk {clerk_id} belongs to broker {clerk.broker!r}, not {broker!r}.",
            )
        adapter = self._adapter(broker)
        canonical = adapter.canonical_account_id(external_account_id)
        existing = self._store.read_assignment(broker=broker, canonical_account_id=canonical)
        if existing is None or existing.clerk_id != clerk_id:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} is not reserved for "
                f"clerk {clerk_id}.",
            )
        if existing.state == AssignmentState.EFFECTIVE:
            return existing
        if existing.state == AssignmentState.RELEASED:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} was released; a "
                "released assignment is re-reserved, never re-confirmed.",
            )
        now = self._clock()
        confirmed = AccountAssignmentRecord(
            broker=broker,
            canonical_external_account_id=canonical,
            clerk_id=clerk_id,
            assignment_generation=existing.assignment_generation,
            state=AssignmentState.EFFECTIVE,
            effective_profile_id=effective_profile_id,
            effective_revision=effective_revision,
            recorded_at_ms=existing.recorded_at_ms,
            updated_at_ms=now,
        )
        accepted = False
        with self._store.transaction() as conn:
            accepted = self._store.cas_update_assignment(
                conn, confirmed, previous_generation=existing.assignment_generation
            )
        if not accepted:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} changed while "
                "confirming; re-read and retry.",
            )
        with self._store.transaction() as conn:
            self._store.touch_session(
                conn,
                clerk_id=clerk_id,
                agent_instance_id=self._require_session(clerk_id).agent_instance_id,
                last_seen_at_ms=now,
                reported_binding_generation=binding_generation,
                reported_account_id=canonical,
                reported_state="binding_confirmed",
            )
        logger.info(
            "fleet account assignment effective",
            extra={"broker": broker, "clerk_id": clerk_id},
        )
        return confirmed

    def release_assignment(
        self,
        *,
        broker: str,
        external_account_id: str,
        proof: str,
    ) -> AccountAssignmentRecord:
        """The host-only release ceremony (PRD FR-055).

        Terminal for the generation: the row records ``released`` and stays,
        which is what makes "never expire into takeover" auditable. Reuse of
        the account starts a fresh reservation with a higher generation.
        """
        if not proof or proof.strip() != "old-clerk-offline-and-obligations-clear":
            raise ClerkAssignmentConflict(
                "The release ceremony requires the offline-and-obligations-clear proof.",
                next_step="Prove the old agent and volume are offline and the "
                "provider's obligations are clear, then re-run with the proof token.",
            )
        adapter = self._adapter(broker)
        canonical = adapter.canonical_account_id(external_account_id)
        existing = self._store.read_assignment(broker=broker, canonical_account_id=canonical)
        if existing is None:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} has no assignment to release.",
            )
        if existing.state == AssignmentState.RELEASED:
            return existing
        now = self._clock()
        released = AccountAssignmentRecord(
            broker=broker,
            canonical_external_account_id=canonical,
            clerk_id=existing.clerk_id,
            assignment_generation=existing.assignment_generation,
            state=AssignmentState.RELEASED,
            effective_profile_id=existing.effective_profile_id,
            effective_revision=existing.effective_revision,
            recorded_at_ms=existing.recorded_at_ms,
            updated_at_ms=now,
        )
        accepted = False
        with self._store.transaction() as conn:
            accepted = self._store.cas_update_assignment(
                conn, released, previous_generation=existing.assignment_generation
            )
        if not accepted:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} changed while releasing.",
            )
        logger.info(
            "fleet account assignment released",
            extra={"broker": broker},
        )
        return released

    # ---- routing -----------------------------------------------------------

    def resolve_route(
        self, *, broker: str, clerk_id: str, expected_routing_epoch: int | None = None
    ) -> tuple[ClerkRecord, ClerkSessionRecord]:
        """Resolve a routed operation's clerk, verifying path identities.

        ``clerk_id`` is resolved first, then the path broker must equal the
        clerk's immutable broker (PRD FR-071). A retired or draining clerk and
        a clerk with no live session are not routable. When the caller pins an
        epoch, a mismatch refuses rather than silently retargeting (FR-078).
        """
        if not broker or not clerk_id:
            raise BrokerAndClerkRequired(
                "A clerk-scoped operation requires both broker and clerk identity.",
            )
        clerk = self._require_clerk(clerk_id)
        if clerk.broker != broker:
            raise ClerkBrokerMismatch(
                f"Route broker {broker!r} does not match clerk {clerk_id}'s "
                f"immutable broker {clerk.broker!r}.",
            )
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(f"Clerk {clerk_id} is retired; its routes are gone.")
        if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
            from app.broker.fleet.errors import ClerkUnreachable

            raise ClerkUnreachable(
                f"Clerk {clerk_id} is draining and accepts no routed operations.",
            )
        session = self._store.read_session(clerk_id)
        if session is None:
            from app.broker.fleet.errors import ClerkUnreachable

            raise ClerkUnreachable(
                f"Clerk {clerk_id} has no registered agent session.",
            )
        if (
            expected_routing_epoch is not None
            and session.routing_epoch != expected_routing_epoch
        ):
            raise ClerkIdentityMismatch(
                f"Routing epoch {expected_routing_epoch} is no longer clerk "
                f"{clerk_id}'s current epoch {session.routing_epoch}; a retry must "
                "not cross an epoch change automatically.",
            )
        return clerk, session

    def record_routing_receipt(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation_kind: str,
        nonsecret_target_ref: str,
        idempotency_key: str,
        state: RoutingReceiptState,
        upstream_receipt_ref: str | None = None,
        correlation_id: str | None = None,
    ) -> RoutingReceiptRecord:
        """Correlate one routing attempt (PRD FR-079).

        A receipt never replaces the provider clerk's execution or custody
        receipt; the upstream reference is carried, not interpreted. Retrying
        the same idempotency identity updates the existing receipt.
        """
        now = self._clock()
        existing = self._store.find_routing_receipt_by_idempotency(
            broker=broker, clerk_id=clerk_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            updated = False
            with self._store.transaction() as conn:
                updated = self._store.update_routing_receipt_outcome(
                    conn,
                    correlation_id=existing.correlation_id,
                    state=state,
                    upstream_receipt_ref=upstream_receipt_ref,
                    updated_at_ms=now,
                )
            if updated:
                receipt = self._store.read_routing_receipt(existing.correlation_id)
                assert receipt is not None
                return receipt
        receipt = RoutingReceiptRecord(
            correlation_id=correlation_id if correlation_id is not None else new_correlation_id(),
            broker=broker,
            clerk_id=clerk_id,
            operation_kind=operation_kind,
            nonsecret_target_ref=nonsecret_target_ref,
            idempotency_key=idempotency_key,
            state=state,
            upstream_receipt_ref=upstream_receipt_ref,
            created_at_ms=now,
            updated_at_ms=now,
        )
        with self._store.transaction() as conn:
            self._store.insert_routing_receipt(conn, receipt)
        return receipt

    # ---- directory ---------------------------------------------------------

    def directory(self, *, include_retired: bool = False) -> dict[str, object]:
        """The broker-neutral, read-only lane directory (PRD §10.1).

        Every entry carries broker and clerk identity; the provider summary is
        authored by the provider adapter from registry-held facts only. No
        financial quantity is computed, combined or projected here (FR-034).
        """
        now = self._clock()
        entries: list[dict[str, object]] = []
        for clerk in self._store.list_clerks(include_retired=include_retired):
            session = self._store.read_session(clerk.clerk_id)
            entries.append(
                self._descriptor(clerk, session, now).public_fields()
            )
        return {"observed_at_ms": now, "clerks": entries}

    def describe_clerk(self, clerk_id: str) -> ClerkDescriptor:
        """The directory projection for one clerk (operator ceremony reads)."""
        clerk = self._require_clerk(clerk_id)
        session = self._store.read_session(clerk_id)
        return self._descriptor(clerk, session, self._clock())

    def _descriptor(
        self, clerk: ClerkRecord, session: ClerkSessionRecord | None, now: int
    ) -> ClerkDescriptor:
        adapter = self._provider_adapters.get(clerk.broker)
        capabilities: tuple[str, ...] = ()
        provider_summary: dict[str, object] | None = None
        effective_state = _project_lifecycle(clerk, session, now, self._session_stale_after_ms)
        if adapter is not None:
            capabilities = tuple(sorted(cap.value for cap in adapter.capabilities))
            if session is not None:
                observation: dict[str, object] = {
                    "reported_state": session.reported_state,
                    "reported_account_id": session.reported_account_id,
                    "reported_binding_generation": session.reported_binding_generation,
                    "lifecycle_state": str(effective_state),
                }
                provider_summary = dict(adapter.provider_summary(observation))
        return ClerkDescriptor(
            broker=clerk.broker,
            clerk_id=clerk.clerk_id,
            display_label=clerk.display_label,
            lifecycle_state=effective_state,
            volume_id=clerk.volume_id,
            last_seen_at_ms=None if session is None else session.last_seen_at_ms,
            routing_epoch=None if session is None else session.routing_epoch,
            effective_binding_generation=(
                None if session is None else session.reported_binding_generation
            ),
            capabilities=capabilities,
            provider_summary=provider_summary,
            observed_at_ms=now,
        )

    # ---- read-only aggregation over lanes -----------------------------------

    def aggregate_lane_reads(
        self, lane_reads: Sequence[tuple[str, str, Callable[[], Mapping[str, object]]]]
    ) -> dict[str, object]:
        """Provenance-preserving partial aggregation (PRD FR-083/084).

        Each lane contributes an independent reader. One lane's exception is
        reported as that lane's explicit failure — never omission, never
        substitution, never a global failure. The coordinator combines no
        values: it returns each lane's mapping untouched.
        """
        now = self._clock()
        results: list[dict[str, object]] = []
        for broker, clerk_id, reader in lane_reads:
            try:
                payload = dict(reader())
                outcome: dict[str, object] = {
                    "broker": broker,
                    "clerk_id": clerk_id,
                    "ok": True,
                    "value": payload,
                }
            except Exception as exc:
                outcome = {
                    "broker": broker,
                    "clerk_id": clerk_id,
                    "ok": False,
                    "error_reason": getattr(exc, "reason", type(exc).__name__),
                    "error_message": str(exc),
                }
            results.append(outcome)
        return {"observed_at_ms": now, "lanes": results}

    # ---- internal ------------------------------------------------------------

    def _require_clerk(self, clerk_id: str) -> ClerkRecord:
        # A malformed identity cannot exist, so it is reported as not found —
        # callers learn nothing about any real clerk from the distinction.
        if not is_clerk_id(clerk_id):
            raise ClerkNotFound(f"No clerk carries identity {clerk_id!r}.")
        clerk = self._store.read_clerk(clerk_id)
        if clerk is None:
            raise ClerkNotFound(f"No clerk carries identity {clerk_id!r}.")
        return clerk

    def _require_session(self, clerk_id: str) -> ClerkSessionRecord:
        session = self._store.read_session(clerk_id)
        if session is None:
            from app.broker.fleet.errors import ClerkUnreachable

            raise ClerkUnreachable(f"Clerk {clerk_id} has no registered agent session.")
        return session


def _project_lifecycle(
    clerk: ClerkRecord,
    session: ClerkSessionRecord | None,
    now: int,
    stale_after_ms: int,
) -> ClerkLifecycleState:
    """Durable states pass through; live states project from observations.

    A stored flag would let a historical acknowledgement present itself as
    current liveness (PRD FR-081), so readiness is recomputed from the
    session's freshness and the assignment state every time.
    """
    if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
        return ClerkLifecycleState.RETIRED
    if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
        return ClerkLifecycleState.DRAINING
    if session is None:
        return ClerkLifecycleState.PROVISIONED
    if now - session.last_seen_at_ms > stale_after_ms:
        return ClerkLifecycleState.UNREACHABLE
    if session.reported_state == "degraded":
        return ClerkLifecycleState.DEGRADED
    if session.reported_binding_generation is None:
        return ClerkLifecycleState.STARTING
    return ClerkLifecycleState.READY


__all__ = [
    "ATTESTATION_KINDS",
    "DEFAULT_SESSION_STALE_AFTER_MS",
    "FleetControlService",
    "ProvisionedClerk",
]

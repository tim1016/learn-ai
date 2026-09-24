"""The fleet control service: the coordinator's rules surface.

One seam for provisioning, volume verification, session registration,
broker-qualified assignment fencing, routing attempts and the broker-neutral
directory. The refusals this service raises are PRD §10.4's stable families
plus the internal families added by the 2026-09-13 audit; storage lives in
``store.py``, identity minting in ``identity.py``, and provider declarations
in ``provider.py``.

The load-bearing invariants, each with a test:

- Assignment ownership never expires. Nothing in this service consults
  heartbeat age before refusing a rival reservation (PRD FR-054); liveness
  only projects ``unreachable`` in the directory.
- Volume identity is proven before authority: registration and reservation
  both verify the marker against the registry row (PRD FR-025/026), and root
  comparisons are qualified by deployment namespace so equal path strings in
  two containers never masquerade as one physical volume.
- Observed facts never confirm anything (audit 2026-09-13, finding 1): a
  heartbeat refreshes liveness projections only; the confirmed binding
  observation lives on the assignment row, is fenced by the confirming
  instance and epoch, refuses stale generations, and is the only routing
  fence for execution operations.
- The directory computes no financial facts (PRD FR-034) and never exposes
  ``worker_key`` (FR-012).
- Routing attempts pin their context before dispatch, and a delivered
  outcome is terminal (audit 2026-09-13, finding 7).
- A clerk that has served leaves service only through the drain ceremony
  (ADR 0063): drain closes the door, the deadline bounds the wait, and the
  normal retirement path refuses without a lane-quiet confirmation rather
  than degrading to an attestation; ``force-retire`` is the separately
  named, operator-attributed exit.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from app.broker.fleet import volume as volume_module
from app.broker.fleet.drain_deadline import (
    DRAIN_DEADLINE_FLOOR_MS,
    drain_deadline_at_ms,
)
from app.broker.fleet.errors import (
    BrokerAndClerkRequired,
    BrokerClerkCapabilityUnavailable,
    ClerkAccountMismatch,
    ClerkAssignmentConflict,
    ClerkBindingGenerationConflict,
    ClerkBrokerMismatch,
    ClerkCommandQuietRequired,
    ClerkDrainDeadlinePending,
    ClerkDrainRequired,
    ClerkEndpointNotApproved,
    ClerkIdentityMismatch,
    ClerkLaneDraining,
    ClerkLaneQuietUnproven,
    ClerkLaneRetired,
    ClerkNotFound,
    ClerkReassignmentBlocked,
    ClerkRoutingAttemptConflict,
    ClerkRoutingOutcomeUnknown,
    ClerkUnreachable,
    ClerkVolumeAlreadyRegistered,
    ClerkVolumeCloneDetected,
    FleetProtocolIncompatible,
)
from app.broker.fleet.identity import (
    is_clerk_id,
    new_agent_instance_id,
    new_clerk_id,
    new_correlation_id,
    new_service_token,
    new_volume_id,
    new_worker_key,
)
from app.broker.fleet.internal_http import address_is_private
from app.broker.fleet.provider import (
    FLEET_PROTOCOL_VERSION,
    PRODUCTION_PROVIDER_ADAPTERS,
    BrokerProviderAdapter,
    Capability,
    OperationReadiness,
    require_adapter,
    require_protocol_compatible,
)
from app.broker.fleet.records import (
    AccountAssignmentRecord,
    ApprovedEndpointRecord,
    AssignmentState,
    ClerkDescriptor,
    ClerkLifecycleState,
    ClerkRecord,
    ClerkSessionRecord,
    LaneConfirmationState,
    LaneQuietConfirmationRecord,
    ProviderSummaryObservation,
    RoutingReceiptRecord,
    RoutingReceiptState,
    SessionObservation,
    StoredLifecycleState,
    VolumeMarker,
)
from app.broker.fleet.recovery import require_recovery_hold_clear
from app.broker.fleet.store import FleetRegistryStore
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: How stale a heartbeat may be before the directory projects ``unreachable``.
#: A projection only: it never releases, downgrades or transfers anything.
DEFAULT_SESSION_STALE_AFTER_MS = 30_000

#: How long one lane-quiet confirmation stays valid (ADR 0063 Decision 2's
#: 2026-09-19 amendment, #2154). The lane re-asserts on its heartbeat
#: cadence (``HEARTBEAT_INTERVAL_S``, 10 s by default), so this is nine
#: beats: long enough to ride out a refused beat and the lane repair that
#: follows it, short enough that a gate reading "quiet" is reading a claim
#: made minutes ago rather than hours. Sized from that push cadence but
#: written as a literal: the cadence is a deployment setting, and a registry
#: constant must not reach into application settings to compute itself.
#: Deliberately **not** reasoned from ``DEFAULT_SESSION_STALE_AFTER_MS`` —
#: that constant measures heartbeat-staleness *detection latency*, a
#: different quantity, and Decision 5 already forbids reasoning from it for
#: the drain deadline. Note the trap: three times that constant happens to
#: equal this value today, so no test can catch a re-derivation from it and
#: the ban is a review-time rule. What goes stale here is the evidence,
#: never the lane: a lane beating steadily but no longer confirming refuses
#: exactly as a dead one does, which is what keeps this clear of Decision
#: 5's rule that silence never *moves* anything.
DEFAULT_LANE_QUIET_VALID_FOR_MS = 90_000

#: The drain ceremony's deployment-owned duration (ADR 0063 Decision 5): the
#: first leg of the two-legged deadline floor. A deployment may raise it; the
#: floor in ``drain_deadline.py`` refuses anything lower. It must be judged
#: against a plausible working-order lifetime, never against
#: ``DEFAULT_SESSION_STALE_AFTER_MS`` — that 30-second constant measures
#: heartbeat-staleness detection latency, an entirely different quantity.
DEFAULT_DRAIN_DEADLINE_MS = DRAIN_DEADLINE_FLOOR_MS

#: The bounded operator attribution every release, reassignment and
#: force-retirement carries (ADR 0063 Decision 4), in the shape
#: ``closeout_empty_registry_recovery`` already uses. Attribution, not
#: proof: a change reference names who acted and why, where the deleted
#: ``RELEASE_PROOF_TOKEN`` named nobody and was published in the artifact
#: that checked it.
_OPERATOR_MAX_CHARS = 128
_CHANGE_REF_MAX_CHARS = 512

_COMPOSE_NAMED_VOLUME = "compose_named_volume"
ATTESTATION_KINDS = frozenset({_COMPOSE_NAMED_VOLUME})

#: The namespace a single-host, unfenced deployment provisions into. Real
#: deployments name their own (for example ``compose:prod``); roots compare
#: only within one namespace, because the same path in two containers is two
#: mounts, not one shared volume (audit 2026-09-13, finding 4).
DEFAULT_DEPLOYMENT_NAMESPACE = "host:local"

_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,63}$")
_ENDPOINT_REF_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,63}$")
_ADAPTER_VERSION_MAX_CHARS = 64

#: Shared next_step prose for refusals that are the same remediation at every
#: call site (a clerk's broker is immutable; a clerk id the registry has never
#: seen), rather than tailored per-context like most of the backfill (#2067).
_BROKER_IMMUTABLE_NEXT_STEP = (
    "Use the broker this clerk was provisioned under; a clerk's broker is immutable."
)
_UNKNOWN_CLERK_NEXT_STEP = "Confirm the clerk id against the fleet directory before retrying."


@dataclass(frozen=True, slots=True)
class ProvisionedClerk:
    """What a host ceremony gets back: identities to wire into deployment.

    The two service tokens are transport credentials minted once for the
    operator's uncommitted environment files — the agent-to-coordinator
    token and the coordinator-to-agent token. They are never stored in the
    registry; the ``worker_key`` in ``clerk`` remains the stored durable
    identity and is never a transport credential (audit 2026-09-13,
    finding 3).
    """

    clerk: ClerkRecord
    marker: VolumeMarker
    agent_service_token: str
    coordinator_service_token: str


class FleetControlService:
    """The coordinator's one rules surface over the registry store."""

    def __init__(
        self,
        *,
        store: FleetRegistryStore,
        provider_adapters: Mapping[str, BrokerProviderAdapter] | None = None,
        clock: Callable[[], int] = now_ms_utc,
        session_stale_after_ms: int = DEFAULT_SESSION_STALE_AFTER_MS,
        drain_deadline_ms: int = DEFAULT_DRAIN_DEADLINE_MS,
        lane_quiet_valid_for_ms: int = DEFAULT_LANE_QUIET_VALID_FOR_MS,
    ) -> None:
        """Bind the registry store, the deployment's adapters, and the clock."""
        self._store = store
        # Constructor injection is the extension boundary (PRD FR-002): a
        # test-only fake adapter is passed here and never enters the
        # code-owned production mapping.
        self._provider_adapters: Mapping[str, BrokerProviderAdapter] = (
            provider_adapters if provider_adapters is not None else PRODUCTION_PROVIDER_ADAPTERS
        )
        self._clock = clock
        self._session_stale_after_ms = session_stale_after_ms
        if drain_deadline_ms < DRAIN_DEADLINE_FLOOR_MS:
            raise ValueError(
                f"drain_deadline_ms={drain_deadline_ms} is below the "
                f"{DRAIN_DEADLINE_FLOOR_MS} ms floor (ADR 0063 Decision 5); a "
                "deployment may raise the duration, never lower it"
            )
        self._drain_deadline_ms = drain_deadline_ms
        if lane_quiet_valid_for_ms <= 0:
            raise ValueError(
                f"lane_quiet_valid_for_ms={lane_quiet_valid_for_ms} must be positive; "
                "a non-positive window would make every confirmation stale on arrival "
                "and close the normal retirement path #2154 exists to open"
            )
        self._lane_quiet_valid_for_ms = lane_quiet_valid_for_ms

    @property
    def lane_quiet_valid_for_ms(self) -> int:
        """How long a lane-quiet confirmation answers the gate, in ms."""
        return self._lane_quiet_valid_for_ms

    def close(self) -> None:
        """Close the underlying registry store."""
        self._store.close()

    def _require_recovery_hold_clear(self) -> None:
        """Reject routing and assignment mutation during a restore ceremony."""
        require_recovery_hold_clear(
            self._store.db_path.parent.parent,
            registry_id=self._store.registry_id,
        )

    # ---- provider adapters ----------------------------------------------

    def _adapter(self, broker: str) -> BrokerProviderAdapter:
        """Resolve the deployment's adapter for one broker, failing closed."""
        return require_adapter(self._provider_adapters, broker)

    def adapters(self) -> Mapping[str, BrokerProviderAdapter]:
        """The deployment's provider composition (catalog reads, never mutation)."""
        return dict(self._provider_adapters)

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

    def approved_endpoint(self, clerk_id: str) -> ApprovedEndpointRecord | None:
        """One lane's approved internal destination (routing reads it, never sets it)."""
        return self._store.read_approved_endpoint(clerk_id)

    # ---- provisioning (host ceremony) ------------------------------------

    def provision_clerk(
        self,
        *,
        broker: str,
        display_label: str,
        volume_root: Path,
        attestation_kind: str = _COMPOSE_NAMED_VOLUME,
        attestation_id: str | None = None,
        deployment_namespace: str = DEFAULT_DEPLOYMENT_NAMESPACE,
    ) -> ProvisionedClerk:
        """Create one clerk lane: mint identities, mark the volume, register.

        The attestation for a compose named volume is the volume's name —
        deployment-owned and nonsecret. The root must be a fresh canonical
        mount; a root that already carries a marker is a copied or re-mounted
        volume and refuses (PRD FR-026). Root comparisons run only against
        clerks in the same deployment namespace: equal path strings across
        namespaces are different mounts, and different strings do not prove
        different volumes (audit 2026-09-13, finding 4).
        """
        self._require_recovery_hold_clear()
        if not broker or not display_label:
            raise BrokerAndClerkRequired(
                "Provisioning requires a broker and a display label.",
                next_step="Pass a non-empty broker id and display label; "
                "provisioning has no default for either.",
            )
        self._adapter(broker)  # unknown production provider fails closed here
        if _NAMESPACE_PATTERN.fullmatch(deployment_namespace) is None:
            raise ValueError(
                f"deployment namespace {deployment_namespace!r} is not a bounded "
                "host:style token"
            )
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
        canonical_root = volume_module.resolve_canonical_root(volume_root)
        existing_marker = volume_module.read_volume_marker(volume_root)
        if existing_marker is not None:
            raise ClerkVolumeCloneDetected(
                f"The root {volume_root} already carries the identity marker of "
                f"clerk {existing_marker.clerk_id}; provisioning writes a fresh "
                "volume, never a second identity onto an existing one.",
                next_step="Use a fresh named volume for the new clerk.",
            )
        # ``resolve()`` is pure normalization here — the canonical-root proof
        # above has already excluded every symlink in the chain.
        new_root = canonical_root.resolve()

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
            deployment_namespace=deployment_namespace,
            attestation_kind=attestation_kind,
            attestation_id=attestation_id,
        ) is not None:
            raise ClerkVolumeAlreadyRegistered(
                f"Another active clerk already attests volume "
                f"{attestation_kind}:{attestation_id} in deployment namespace "
                f"{deployment_namespace!r}.",
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
            volume_root=str(new_root),
            deployment_namespace=deployment_namespace,
            volume_attestation_kind=attestation_kind,
            volume_attestation_id=attestation_id,
            lifecycle_state=StoredLifecycleState.PROVISIONED,
            created_at_ms=now,
            retired_at_ms=None,
        )
        with self._store.transaction() as conn:
            # FR-020/021: writable subtrees of one mounted volume never host
            # two clerks. Distinct attestations do not make nested roots
            # distinct physical volumes, so containment is refused in both
            # directions — but only within one deployment namespace, where
            # path strings share a filesystem. The check reads inside the
            # write transaction so no rival provisioning can commit between it
            # and the insert it guards; schema v3's index and nesting trigger
            # are the backstop underneath it.
            for active in self._store.list_clerks_on(conn):
                if active.deployment_namespace != deployment_namespace:
                    continue
                existing_root = Path(active.volume_root)
                if new_root == existing_root or new_root.is_relative_to(
                    existing_root
                ) or existing_root.is_relative_to(new_root):
                    raise ClerkVolumeAlreadyRegistered(
                        f"The root {volume_root} shares a mounted volume with active "
                        f"clerk {active.clerk_id} ({existing_root}) in deployment "
                        f"namespace {deployment_namespace!r}; one clerk, one "
                        "physical volume.",
                        next_step="Mount a separate named volume for the new clerk.",
                    )
            try:
                self._store.insert_clerk(conn, record)
            except sqlite3.IntegrityError as exc:
                # A rival provisioning committed between the pre-checks and
                # this insert, or the schema's own nested-root fence fired;
                # the loser gets the typed refusal, never the constraint
                # traceback.
                raise ClerkVolumeAlreadyRegistered(
                    f"Another active clerk already claims this volume's identity "
                    f"in deployment namespace {deployment_namespace!r}.",
                    next_step="Each clerk owns a distinct physical volume; mount a "
                    "fresh one for the new clerk.",
                ) from exc
        try:
            volume_module.write_volume_marker(volume_root, marker)
        except Exception:
            # Compensation: the registry row is committed but the volume never
            # received its marker (read-only volume, full disk, killed
            # process). Retire the half-born clerk so its attestation and
            # volume identity leave the active set and the lane can be
            # re-provisioned cleanly instead of wedging on a ghost. The
            # clerk provably never served (no session, no assignment, no
            # history), so the lifecycle trigger's never-served arm admits
            # this direct retirement — the one legitimate writer of that
            # edge besides the ceremony's own predicate.
            with self._store.transaction() as conn:
                self._store.retire_clerk_row(
                    conn,
                    clerk_id=clerk_id,
                    retired_at_ms=self._clock(),
                )
            logger.error(
                "fleet clerk provisioning failed after registration; retired the "
                "partial clerk",
                extra={"broker": broker, "clerk_id": clerk_id},
            )
            raise
        logger.info(
            "fleet clerk provisioned",
            extra={
                "broker": broker,
                "clerk_id": clerk_id,
                "volume_id": volume_id,
                "attestation_kind": attestation_kind,
                "deployment_namespace": deployment_namespace,
            },
        )
        return ProvisionedClerk(
            clerk=record,
            marker=marker,
            agent_service_token=new_service_token(),
            coordinator_service_token=new_service_token(),
        )

    def verify_clerk_volume(self, *, clerk_id: str, volume_root: Path) -> VolumeMarker:
        """Re-run the fail-before-authority gate against the registry."""
        clerk = self._require_clerk(clerk_id)
        return self._verify_volume(clerk, volume_root)

    def clerk_volume_expectation(self, clerk_id: str) -> dict[str, object]:
        """The nonsecret marker facts an agent proves its mounted root against.

        Served by the coordinator's internal surface so a remote agent can
        run the volume gate locally — the coordinator never inspects an
        agent-local path (audit 2026-09-13, finding 4).
        """
        clerk = self._require_clerk(clerk_id)
        return {
            "registry_id": self._store.registry_id,
            "broker": clerk.broker,
            "clerk_id": clerk.clerk_id,
            "volume_id": clerk.volume_id,
            "attestation_kind": clerk.volume_attestation_kind,
            "attestation_id": clerk.volume_attestation_id,
        }

    def _verify_volume(self, clerk: ClerkRecord, volume_root: Path) -> VolumeMarker:
        """Verify the mounted root against the clerk's registry identity."""
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

    def drain_clerk(self, *, clerk_id: str) -> ClerkRecord:
        """Enter the drain: ``provisioned -> draining``, deliberately cheap (ADR 0063 Decision 1).

        Nothing gates entry but the registry recovery hold and the clerk
        being provisioned: the transition *is* the stop-accepting-new-work
        step (``resolve_route`` already refuses a draining clerk), and a
        drain that could be refused for being inconvenient would leave an
        operator unable to close the door. It writes the durable
        ``draining_since_ms`` and the absolute ``drain_deadline_at_ms``
        resolved from the deployment duration and the trading calendar, in
        one transaction.

        Idempotent without extending the bound (§7.3): re-draining a
        draining clerk returns the existing record with both instants
        untouched — a retry loop must not make the deadline decorative. The
        write-once trigger beneath the store seam is the fence a race cannot
        step over. Drain is irreversible; an operator who drains the wrong
        lane provisions a new one.
        """
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired; a retired lane never returns to service.",
                next_step="Provision a new clerk; a retired lane's identity is "
                "never reinstated.",
            )
        if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
            return clerk
        now = self._clock()
        deadline = drain_deadline_at_ms(
            draining_since_ms=now, drain_deadline_ms=self._drain_deadline_ms
        )
        with self._store.transaction() as conn:
            live = self._store.read_clerk_on(conn, clerk_id)
            if live is None or live.lifecycle_state != StoredLifecycleState.PROVISIONED:
                raise ClerkAssignmentConflict(
                    f"Clerk {clerk_id} is no longer provisioned; drain entered or "
                    "retired concurrently.",
                    next_step="Re-read the clerk's lifecycle state and retry the "
                    "ceremony against the current state.",
                )
            updated = self._store.drain_clerk_row(
                conn,
                clerk_id=clerk_id,
                draining_since_ms=now,
                drain_deadline_at_ms=deadline,
            )
            if not updated:
                raise ClerkAssignmentConflict(
                    f"Clerk {clerk_id} left provisioned while the drain was "
                    "committing; no drain was written.",
                    next_step="Re-read the clerk's lifecycle state and retry.",
                )
        logger.info(
            "fleet clerk drained",
            extra={
                "broker": clerk.broker,
                "clerk_id": clerk_id,
                "draining_since_ms": now,
                "drain_deadline_at_ms": deadline,
            },
        )
        drained = self._store.read_clerk(clerk_id)
        assert drained is not None
        return drained

    def retire_clerk(self, *, clerk_id: str) -> ClerkRecord:
        """Retirement is terminal; IDs are never recycled (PRD FR-013, ADR 0063).

        A clerk that provably never served — no row in the append-only
        assignment history, none in the append-only session history, and no
        live session row — retires directly. A clerk that has ever served
        must pass through the drain: it must be ``draining``, command-quiet
        (zero dispatches whose outcome the coordinator lost), and covered by
        a fresh lane-quiet confirmation from its current session (#2154). A
        lane that has genuinely gone quiet now retires here; a lane that has
        not, or that cannot answer, is refused by name and leaves through
        ``force_retire_clerk``. A draining Alpaca lane answers on every
        heartbeat (``fleet_boot._confirm_lane_quiet_if_draining``). The held
        assignment check runs first in every branch, and each gate shares
        one write transaction with the transition it guards.
        """
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            return clerk
        now = self._clock()
        with self._store.transaction() as conn:
            held = [
                assignment
                for assignment in self._store.list_assignments_for_clerk_on(conn, clerk_id)
                if assignment.state != AssignmentState.RELEASED
            ]
            if held:
                raise ClerkAssignmentConflict(
                    f"Clerk {clerk_id} still holds {len(held)} account assignment(s); "
                    "retirement requires the release ceremony for each assigned "
                    "account after the drain, not a silent orphaning.",
                    next_step="Drain the clerk, run the release ceremony for each "
                    "assigned account, then retire.",
                )
            if not self._store.clerk_has_served_on(conn, clerk_id):
                # Decision 6: the bypass is closed against an observable.
                # This half-born or never-booted clerk retires directly; the
                # lifecycle trigger's fourth arm mirrors this predicate
                # exactly, so no caller can step over it.
                updated = self._store.retire_clerk_row(
                    conn, clerk_id=clerk_id, retired_at_ms=now
                )
                if not updated:
                    raise ClerkAssignmentConflict(
                        f"Clerk {clerk_id} changed while retiring; re-read and retry.",
                    )
            else:
                live = self._store.read_clerk_on(conn, clerk_id)
                assert live is not None
                if live.lifecycle_state == StoredLifecycleState.PROVISIONED:
                    raise ClerkDrainRequired(
                        f"Clerk {clerk_id} has served and retires only through "
                        "draining; a served clerk's direct retirement is closed.",
                        next_step="Run the drain ceremony first, then wait out the "
                        "deadline and retire.",
                    )
                self._require_command_quiet(conn, clerk_id)
                # Read inside the write transaction because every other
                # fence here is. It cannot be raced today — #2155 refuses a
                # draining clerk's re-registration, so the session this
                # scopes to cannot be replaced mid-ceremony — and it is held
                # as the file's standing pattern against that rule relaxing,
                # not as a live race. The store-level tests pin the scoping
                # where it can still fail.
                self._require_lane_quiet(conn, live, now=now)
                updated = self._store.retire_clerk_row(
                    conn,
                    clerk_id=clerk_id,
                    retired_at_ms=now,
                    lane_confirmation=LaneConfirmationState.PRESENT,
                )
                if not updated:
                    raise ClerkAssignmentConflict(
                        f"Clerk {clerk_id} changed while retiring; re-read and retry.",
                    )
        logger.info("fleet clerk retired", extra={"broker": clerk.broker, "clerk_id": clerk_id})
        retired = self._store.read_clerk(clerk_id)
        assert retired is not None
        return retired

    def force_retire_clerk(
        self, *, clerk_id: str, operator: str, change_ref: str
    ) -> ClerkRecord:
        """The separately named exit for a drain that cannot answer (ADR 0063 Decision 5).

        Requires the clerk to be draining and the two-legged deadline to have
        actually elapsed. Settles every still-unsettled attempt as
        ``outcome_unknown`` — fabricating nothing: that value *means* "may
        have executed, reconcile by identity, never resubmit" — releases any
        assignment the lane still holds under the same attribution (a lane
        that cannot answer is exactly a lane whose release ceremony cannot
        run, because a lost dispatch outcome is what blocks it), records the
        retirement with ``lane_confirmation: absent`` and the operator's
        bounded attribution, and records every forced correlation id in the registry's append-only
        obligations table — in the same transaction, so the reconciliation
        work queue is atomic with the retirement and rides every backup.

        A distinct ceremony, never a branch inside ``retire``: an operator
        who force-retires knows they did, and an auditor can count how often
        it happens. Since the Alpaca lane answers lane quiet (#2154) it is
        the exit only for a lane that cannot answer.
        """
        bounded_operator, bounded_change_ref = _bounded_attestation(operator, change_ref)
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            return clerk
        now = self._clock()
        forced_correlation_ids: list[str] = []
        with self._store.transaction() as conn:
            live = self._store.read_clerk_on(conn, clerk_id)
            assert live is not None
            if live.lifecycle_state == StoredLifecycleState.PROVISIONED:
                raise ClerkDrainRequired(
                    f"Clerk {clerk_id} is provisioned; force-retire is the exit for "
                    "a stalled drain, and a clerk that never served retires "
                    "directly.",
                    next_step="Run the plain retire for a never-served clerk, or "
                    "drain first and wait out the deadline.",
                )
            if live.drain_deadline_at_ms is None or now < live.drain_deadline_at_ms:
                deadline = live.drain_deadline_at_ms
                raise ClerkDrainDeadlinePending(
                    f"Clerk {clerk_id}'s drain deadline "
                    f"({'unknown' if deadline is None else deadline}) has not "
                    f"elapsed (now {now}); the bound is what a stalled drain "
                    "holds a lane to.",
                    next_step="Wait for the drain deadline recorded on the clerk "
                    "row, then re-run force-retire.",
                )
            unsettled = self._store.list_unsettled_routing_receipts_on(conn, clerk_id)
            forced_correlation_ids = [receipt.correlation_id for receipt in unsettled]
            for receipt in unsettled:
                settled = self._store.update_routing_receipt_outcome(
                    conn,
                    correlation_id=receipt.correlation_id,
                    state=RoutingReceiptState.OUTCOME_UNKNOWN,
                    upstream_receipt_ref=None,
                    updated_at_ms=now,
                )
                if not settled:
                    raise ClerkRoutingAttemptConflict(
                        f"Attempt {receipt.correlation_id} changed while "
                        "force-retire was settling it; re-read and retry.",
                    )
            held = [
                assignment
                for assignment in self._store.list_assignments_for_clerk_on(conn, clerk_id)
                if assignment.state != AssignmentState.RELEASED
            ]
            for assignment in held:
                # The same release the ceremony would have run, under the same
                # attribution: the sweep happens only after the attempts that
                # blocked release are settled, so nothing is orphaned past an
                # unresolved obligation.
                released = replace(
                    assignment,
                    state=AssignmentState.RELEASED,
                    updated_at_ms=now,
                    lane_confirmation=LaneConfirmationState.ABSENT,
                    attested_operator=bounded_operator,
                    attested_change_ref=bounded_change_ref,
                    attested_at_ms=now,
                )
                accepted = self._store.cas_update_assignment(
                    conn,
                    released,
                    previous_generation=assignment.assignment_generation,
                    previous_state=assignment.state,
                )
                if not accepted:
                    raise ClerkAssignmentConflict(
                        f"Account {assignment.canonical_external_account_id} under "
                        f"broker {assignment.broker!r} changed while force-retire "
                        "was releasing it; re-read and retry.",
                    )
            updated = self._store.retire_clerk_row(
                conn,
                clerk_id=clerk_id,
                retired_at_ms=now,
                lane_confirmation=LaneConfirmationState.ABSENT,
                retire_operator=bounded_operator,
                retire_change_ref=bounded_change_ref,
            )
            if not updated:
                raise ClerkAssignmentConflict(
                    f"Clerk {clerk_id} changed while force-retiring; re-read and "
                    "retry.",
                )
            # The forced-unknown obligations are recorded in the same
            # transaction as the retirement: a failure anywhere above rolls
            # the whole ceremony back, and the work queue can never be
            # orphaned by a sidecar write that did not happen.
            if forced_correlation_ids:
                self._store.record_forced_correlations(
                    conn,
                    broker=clerk.broker,
                    clerk_id=clerk_id,
                    forced_at_ms=now,
                    operator=bounded_operator,
                    change_ref=bounded_change_ref,
                    correlation_ids=forced_correlation_ids,
                )
        logger.info(
            "fleet clerk force-retired",
            extra={
                "broker": clerk.broker,
                "clerk_id": clerk_id,
                "operator": bounded_operator,
                "forced_correlation_count": len(forced_correlation_ids),
            },
        )
        retired = self._store.read_clerk(clerk_id)
        assert retired is not None
        return retired

    def _require_command_quiet(self, conn: sqlite3.Connection, clerk_id: str) -> None:
        """ADR 0063 Decision 3: refuse past a dispatch whose outcome was lost.

        Read inside the caller's write transaction, so a dispatch landing
        between the check and the commit cannot retire a lane past an
        attempt it just lost the outcome of. At its true strength and no
        higher: this is bookkeeping about the coordinator's own knowledge —
        it prevents retiring past a command whose outcome the coordinator
        lost, and it never claimed to measure orders.
        """
        unsettled = self._store.list_unsettled_routing_receipts_on(conn, clerk_id)
        if unsettled:
            raise ClerkCommandQuietRequired(
                f"Clerk {clerk_id} holds {len(unsettled)} dispatched routing "
                "attempt(s) whose outcome the coordinator lost; retiring the "
                "lane would strand a reconciliation obligation with no owner.",
                next_step="Reconcile or settle each unsettled attempt, then retry "
                "the ceremony.",
            )

    def _require_draining_predecessor_past_deadline(
        self, conn: sqlite3.Connection, clerk_id: str, *, now: int
    ) -> ClerkRecord:
        """ADR 0063 §4/§4.1's shared predecessor gate, read in the caller's
        write transaction: the owning lane must exist, be draining (a closed
        door is what makes the rest of the ceremony's evidence meaningful),
        and have actually waited out the deadline that bounds ``force-retire``.

        A retired predecessor refuses too: retirement sweeps or refuses every
        held assignment on its own terms, so a released assignment surfacing
        under one is a registry inconsistency this ceremony reports rather
        than repairs.
        """
        predecessor = self._store.read_clerk_on(conn, clerk_id)
        if predecessor is None:
            raise ClerkNotFound(
                f"The clerk holding this assignment, {clerk_id}, has no registry "
                "row.",
                next_step="Confirm the clerk id against the fleet directory "
                "before retrying.",
            )
        if predecessor.lifecycle_state != StoredLifecycleState.DRAINING:
            if predecessor.lifecycle_state == StoredLifecycleState.PROVISIONED:
                raise ClerkDrainRequired(
                    f"The owning clerk {clerk_id} is still provisioned; this "
                    "ceremony requires a lane whose door is closed.",
                    next_step="Drain the owning clerk first, then wait out the "
                    "drain deadline, then re-run.",
                )
            raise ClerkAssignmentConflict(
                f"The owning clerk {clerk_id} is retired; its assignment is "
                "not released here.",
                next_step="Restore the registry from its backup — a retired "
                "clerk never holds a live assignment.",
            )
        if predecessor.drain_deadline_at_ms is None or now < predecessor.drain_deadline_at_ms:
            raise ClerkDrainDeadlinePending(
                f"Clerk {clerk_id}'s drain deadline "
                f"({'unknown' if predecessor.drain_deadline_at_ms is None else predecessor.drain_deadline_at_ms}) "
                f"has not elapsed (now {now}); the ceremony waits out the same "
                "bound that gates force-retire.",
                next_step="Wait for the drain deadline recorded on the clerk "
                "row, then re-run the ceremony.",
            )
        return predecessor

    def _released_history_record(
        self, *, broker: str, canonical: str, generation: int
    ) -> AccountAssignmentRecord:
        """The persisted release row for one generation, from the audit history.

        The current-row pointer a released assignment leaves behind carries no
        ceremony facts — the attestation lives only on the history row the
        release inserted — so this is the record an idempotent retry owes its
        caller: identical audit facts to the first run, never nulls. A
        released current row with no matching history row is registry
        corruption (every release transition appends one) and refuses loudly
        rather than synthesizing a record.
        """
        for row in self._store.list_assignment_history(
            broker=broker, canonical_account_id=canonical
        ):
            if row.state == AssignmentState.RELEASED and row.assignment_generation == generation:
                return row
        raise ClerkAssignmentConflict(
            f"Account {canonical} under broker {broker!r} is released at generation "
            f"{generation} but its release history row is missing; the registry is "
            "not internally consistent.",
            next_step="Restore the registry from the coordinator control volume's "
            "backup, then retry.",
        )

    def confirm_lane_quiet(
        self,
        *,
        clerk_id: str,
        agent_instance_id: str,
        routing_epoch: int,
        observed_at_ms: int,
        runner_idle: bool,
        broker_work_ended: bool,
        account_flat: bool,
        intents_resolved: bool,
    ) -> LaneQuietConfirmationRecord:
        """Record one lane's answer about its own quiescence (#2154).

        ADR 0063 Decision 2, as amended 2026-09-19. The lane answers
        conditions 2-5 — no bot running, every working order on the account
        ended at the broker, the account flat, no order intent in flight.
        Condition 1, that the lane is draining, is the registry's own fact and
        is checked here rather than taken from the lane.

        An answer that leaves a condition outstanding is recorded exactly as a
        quiet one is. A lane saying "not yet" is evidence: it is what lets the
        retirement gate name what is outstanding instead of reporting the
        silence of a lane that never answered at all, and it is why the gate
        can distinguish the two.

        The fences are ``confirm_assignment``'s, inherited rather than
        re-invented: the calling instance and epoch must equal the current
        session's, compared inside the write transaction so a superseded
        session cannot slip its answer between the read and the commit.
        """
        self._require_recovery_hold_clear()
        self._require_clerk(clerk_id)
        now = self._clock()
        # A boundary check, not a paranoid guard: the observation instant
        # arrives from the agent. A lane whose clock runs ahead would hold a
        # confirmation that never ages out, so freshness is only a bound if
        # the instant cannot be in the future.
        if observed_at_ms > now:
            raise ClerkLaneQuietUnproven(
                f"Clerk {clerk_id} observed lane quiet at {observed_at_ms}, which is "
                f"ahead of the coordinator's clock ({now}); a confirmation from the "
                "future would never go stale.",
                next_step="Correct the lane host's clock, then re-confirm.",
            )
        if observed_at_ms < 0:
            raise ValueError("observed_at_ms is an int64 ms UTC instant at or after the epoch")
        confirmation = LaneQuietConfirmationRecord(
            clerk_id=clerk_id,
            agent_instance_id=agent_instance_id,
            routing_epoch=routing_epoch,
            observed_at_ms=observed_at_ms,
            recorded_at_ms=now,
            runner_idle=runner_idle,
            broker_work_ended=broker_work_ended,
            account_flat=account_flat,
            intents_resolved=intents_resolved,
        )
        with self._store.transaction() as conn:
            live = self._clerk_on_or_unknown(conn, clerk_id)
            if live.lifecycle_state != StoredLifecycleState.DRAINING:
                raise ClerkDrainRequired(
                    f"Clerk {clerk_id} is {live.lifecycle_state.value}; a lane "
                    "confirms lane quiet only while draining, because the claim is "
                    "about the period after the door closed.",
                    next_step="Run the drain ceremony first; a serving lane has "
                    "nothing to confirm.",
                )
            # The schema's CHECK makes this non-null for every draining row.
            draining_since_ms = live.draining_since_ms
            assert draining_since_ms is not None
            if observed_at_ms < draining_since_ms:
                raise ClerkLaneQuietUnproven(
                    f"Clerk {clerk_id} observed lane quiet at {observed_at_ms}, before "
                    f"the drain began at {draining_since_ms}; quiescence seen before "
                    "the door closed proves nothing about the period after it.",
                    next_step="Re-observe now and confirm that.",
                )
            session = self._store.read_session_on(conn, clerk_id)
            if session is None:
                raise ClerkUnreachable(
                    f"Clerk {clerk_id} has no registered agent session, so there "
                    "is no session for this confirmation to be fenced to.",
                    next_step="A draining lane cannot obtain one — #2155 refuses a "
                    "draining clerk's registration — so force-retire is its exit.",
                )
            if (
                session.agent_instance_id != agent_instance_id
                or session.routing_epoch != routing_epoch
            ):
                raise ClerkIdentityMismatch(
                    f"The lane-quiet confirmation for clerk {clerk_id} was prepared by "
                    f"session {agent_instance_id}/{routing_epoch}, but the current "
                    f"session is {session.agent_instance_id}/{session.routing_epoch}; a "
                    "superseded session cannot confirm.",
                    next_step="Confirm under the lane's current session. A draining "
                    "clerk's session is never replaced (#2155), so a lane that lost "
                    "its own exits through force-retire instead.",
                )
            self._store.record_lane_quiet_confirmation(conn, confirmation)
        logger.info(
            "fleet lane quiet confirmed",
            extra={
                "clerk_id": clerk_id,
                "action": "fleet_lane_quiet_confirmed",
                "quiet": confirmation.is_quiet,
                "outstanding_count": len(confirmation.outstanding),
            },
        )
        return confirmation

    def read_lane_quiet_confirmation(
        self, *, clerk_id: str
    ) -> LaneQuietConfirmationRecord | None:
        """This clerk's newest lane-quiet answer, from any session.

        The audit read, deliberately unscoped by session: an operator asking
        what a lane last claimed is asking about its history. The gate's own
        read is session-scoped and lives in ``_require_lane_quiet``.
        """
        return self._store.read_latest_lane_quiet_confirmation(clerk_id)

    def check_lane_quiet(self, *, clerk_id: str) -> LaneQuietConfirmationRecord:
        """Preview the lane-quiet gate for one clerk, changing nothing.

        The same read retirement and every handover make, so an operator can
        see before an irreversible release whether it would record
        ``present``. Raises the gate's own ``ClerkLaneQuietUnproven`` naming
        why not.
        """
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        with self._store.transaction() as conn:
            return self._require_lane_quiet(conn, clerk, now=self._clock())

    def _require_lane_quiet(
        self, conn: sqlite3.Connection, clerk: ClerkRecord, *, now: int
    ) -> LaneQuietConfirmationRecord:
        """ADR 0063 Decision 2's gate, read from the lane's own answer.

        Shared by retirement and by the three handover paths (§4.1's
        amendment: the hole closes only if the confirmation is wired into
        release, re-reservation and reassignment, not retirement alone).
        Returns the fresh, quiet confirmation it accepted.

        Three ways this refuses, each a different fact about the evidence and
        each named so an operator knows which one they are in:

        - the current session has not answered — silence, which is never read
          as quiet. The read is scoped to that session rather than filtered
          afterwards, because a confirmation does not survive the session that
          made it: conditions 2, 3 and 5 assert a running process's own
          knowledge, and a restart destroys it. There is deliberately no
          separate "a superseded session answered" refusal, because a
          draining clerk's session cannot be replaced at all — #2155 refuses
          its re-registration — so a lane that restarts mid-drain never
          confirms again and exits through force-retire. The next step says so
          rather than leaving an operator to infer it;
        - the answer is older than the validity window, so the lane stopped
          re-asserting. What refuses is the evidence's own age, never the
          lane's liveness: a lane beating steadily but no longer confirming
          refuses exactly as a dead one does. Decision 5 forbids silence from
          *moving* something; here it makes a gate refuse, which is the safe
          direction;
        - the answer names an outstanding condition, and the refusal names
          every one of them rather than the first, so an operator clearing
          them does not walk the ceremony once per item.

        ``force_retire_clerk`` remains the exit for a lane that cannot answer.
        """
        session = self._store.read_session_on(conn, clerk.clerk_id)
        confirmation = (
            None
            if session is None
            else self._store.read_lane_quiet_confirmation_on(
                conn,
                clerk.clerk_id,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
            )
        )
        if confirmation is None:
            raise ClerkLaneQuietUnproven(
                f"Clerk {clerk.clerk_id}'s current session has not confirmed lane "
                "quiet; silence is never read as quiet.",
                next_step="A draining lane confirms on its heartbeat once it has "
                "learned its drain and can read its account; wait one beat and retry. "
                "A lane whose process restarted during the drain can never confirm "
                "— #2155 refuses a draining clerk's re-registration — and neither "
                "can one with no clerk or an unreachable host, so force-retire is "
                "their exit.",
            )
        age_ms = now - confirmation.observed_at_ms
        if age_ms > self._lane_quiet_valid_for_ms:
            raise ClerkLaneQuietUnproven(
                f"Clerk {clerk.clerk_id}'s lane-quiet confirmation is stale: observed "
                f"{age_ms} ms ago, past the {self._lane_quiet_valid_for_ms} ms window. "
                "The lane has stopped re-asserting, so the answer no longer describes "
                "now.",
                next_step="Wait for the lane's next confirmation, or run force-retire.",
            )
        if not confirmation.is_quiet:
            outstanding = "; ".join(confirmation.outstanding)
            raise ClerkLaneQuietUnproven(
                f"Clerk {clerk.clerk_id} is not quiet — {outstanding}.",
                next_step="Clear every item above: stop the bots, cancel the "
                "working orders and flatten in the bot panel, and close anything "
                "opened by hand directly at the broker. The lane re-confirms on its "
                "next beat; then re-run the ceremony.",
            )
        return confirmation

    def _handover_lane_confirmation(
        self, conn: sqlite3.Connection, predecessor: ClerkRecord, *, now: int
    ) -> LaneConfirmationState:
        """Whether a fresh lane-quiet confirmation covers this handover (#2154).

        The account it covers needs no identity on the row: schema v7 makes
        one live assignment per clerk structural, and a draining clerk
        reserves nothing, so the draining predecessor's one live assignment
        is the only account its confirmation can be about. Release stays
        available without one — it is the fleet's only exit, and a lane that
        cannot answer still has to leave — but records ``absent``, and an
        ``absent`` release is what re-reservation later refuses.
        """
        try:
            self._require_lane_quiet(conn, predecessor, now=now)
        except ClerkLaneQuietUnproven as exc:
            logger.info(
                "fleet handover proceeds without lane quiet",
                extra={
                    "clerk_id": predecessor.clerk_id,
                    "action": "fleet_handover_lane_quiet_absent",
                    "reason": exc.message,
                },
            )
            return LaneConfirmationState.ABSENT
        return LaneConfirmationState.PRESENT

    # ---- approved endpoints (host ceremony) --------------------------------

    def approve_endpoint(
        self, *, clerk_id: str, endpoint_ref: str, base_url: str
    ) -> ApprovedEndpointRecord:
        """Approve or re-target one clerk's internal agent destination.

        Deployment-owned placement evidence (audit 2026-09-13, finding 4): a
        registration may cite ``endpoint_ref`` but can never change what it
        points at. The reference and the clerk binding are stable; only the
        destination moves, by re-running this ceremony.
        """
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        if _ENDPOINT_REF_PATTERN.fullmatch(endpoint_ref) is None:
            raise ValueError(
                f"endpoint reference {endpoint_ref!r} is not a bounded host:style token"
            )
        normalized = _validate_internal_base_url(base_url)
        now = self._clock()
        try:
            with self._store.transaction() as conn:
                record = self._store.approve_endpoint(
                    conn,
                    clerk_id=clerk.clerk_id,
                    endpoint_ref=endpoint_ref,
                    base_url=normalized,
                    now_ms=now,
                )
        except sqlite3.IntegrityError as exc:
            raise ClerkEndpointNotApproved(
                f"The endpoint approval for clerk {clerk_id} refused: {exc}",
                next_step="An approved reference is stable; re-approve the same "
                "reference with a new destination, or approve under a fresh "
                "reference for a new clerk.",
            ) from exc
        logger.info(
            "fleet agent endpoint approved",
            extra={"broker": clerk.broker, "clerk_id": clerk_id, "endpoint_ref": endpoint_ref},
        )
        return record

    # ---- agent sessions ---------------------------------------------------

    def register_agent_session(
        self,
        *,
        clerk_id: str,
        worker_key: str,
        agent_instance_id: str | None = None,
        volume_root: Path | None = None,
        endpoint_ref: str | None = None,
        adapter_version: str | None = None,
        fleet_protocol_version: int | None = None,
    ) -> ClerkSessionRecord:
        """Install the clerk's one current agent session, bumping the epoch.

        The worker key is compared with ``hmac.compare_digest``. A restart of
        the same clerk presents a new instance id and supersedes the previous
        session under a higher epoch (FR-065's idempotent same-clerk
        recovery); the archived session keeps the epoch history auditable.
        Retirement refuses registration — routing to a retired clerk is gone
        for good. Draining refuses with the typed ``ClerkLaneDraining`` so a
        restarting lane learns its own drain from the refusal and marks its
        evidence (#2155) rather than presenting a stale grant.

        An ``endpoint_ref`` must cite the deployment-approved reference for
        this clerk; it can never install or move a destination. A declared
        ``fleet_protocol_version`` must match this coordinator's, and a
        declared ``adapter_version`` must equal this coordinator's adapter
        label for the clerk's broker — the coordinator's routing allowlist
        derives from its own catalog, so a mixed build (an older agent
        registering against a newer coordinator's expanded catalog) refuses
        at registration instead of failing per-operation later (audit
        2026-09-13, finding 6).

        ``volume_root`` is the co-located caller's re-proof of the mounted
        root. It is optional because the coordinator process may not have
        the volume mounted at all; the agent's own gate
        (``fleet_boot.open_fleet_lane``) is the authority, and
        ``LocalPresence`` — the one transport that does share the filesystem
        — always supplies it.
        """
        clerk = self._require_clerk(clerk_id)
        if not hmac.compare_digest(clerk.worker_key, worker_key):
            raise ClerkIdentityMismatch(
                f"The worker key presented for clerk {clerk_id} does not match its "
                "registry identity.",
                next_step="Present the worker key issued to this clerk at provisioning.",
            )
        # The cheap pre-read refusal; the authoritative re-read runs inside the
        # write transaction below, past any concurrently committing drain.
        self._refuse_registration_for_closed_lane(clerk)
        if adapter_version is not None and len(adapter_version) > _ADAPTER_VERSION_MAX_CHARS:
            raise ClerkIdentityMismatch(
                "An adapter version string is a short build label, not free text.",
            )
        if adapter_version is not None:
            expected_adapter = self._provider_adapters.get(clerk.broker)
            expected_version = (
                expected_adapter.adapter_version if expected_adapter else None
            )
            if expected_version is not None and adapter_version != expected_version:
                raise FleetProtocolIncompatible(
                    f"Clerk {clerk_id}'s agent declares adapter version "
                    f"{adapter_version!r}; this coordinator routes {clerk.broker!r} "
                    f"by catalog {expected_version!r}. Upgrade the agent before "
                    "registering — a mixed build refuses rather than serving a "
                    "partial catalog.",
                )
        require_protocol_compatible(reported=fleet_protocol_version)
        if endpoint_ref is not None:
            approved = self._store.read_approved_endpoint(clerk_id)
            if approved is None or approved.endpoint_ref != endpoint_ref:
                raise ClerkEndpointNotApproved(
                    f"Clerk {clerk_id} cites endpoint reference {endpoint_ref!r}; the "
                    "deployment has "
                    + (
                        f"approved {approved.endpoint_ref!r}."
                        if approved is not None
                        else "approved no endpoint for this clerk."
                    ),
                    next_step="A registration cites an approved reference; destinations "
                    "change only through the host approval ceremony.",
                )
        if volume_root is not None:
            self._verify_volume(clerk, volume_root)
        now = self._clock()
        instance = agent_instance_id if agent_instance_id is not None else new_agent_instance_id()
        # The current-session read lives inside the write transaction: two
        # coordinators registering different agents for one clerk otherwise
        # both observe "no session", both take epoch 1, and the second upsert
        # silently overwrites the first with no history row. Under
        # ``BEGIN IMMEDIATE`` the second registration sees the first's row
        # and takes epoch 2.
        with self._store.transaction() as conn:
            # The lifecycle gate re-reads inside BEGIN IMMEDIATE — the same
            # fence the session read below is here for: a drain committing
            # between the lock-free pre-read above and this transaction still
            # refuses, rather than installing a session for a lane whose door
            # the ceremony already closed (#2155).
            self._refuse_registration_for_closed_lane(
                self._clerk_on_or_unknown(conn, clerk_id)
            )
            current = self._store.read_session_on(conn, clerk_id)
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
                    endpoint_ref=endpoint_ref if endpoint_ref is not None else current.endpoint_ref,
                    adapter_version=adapter_version if adapter_version is not None else current.adapter_version,
                    reported_summary_json=current.reported_summary_json,
                )
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
                endpoint_ref=endpoint_ref,
                adapter_version=adapter_version,
            )
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
        reported_summary: Mapping[str, object] | None = None,
    ) -> SessionObservation:
        """Record a heartbeat and the worker's *observed* binding facts.

        An observation updates liveness projections only; it can never move
        an assignment, a lifecycle state, a confirmed binding observation or
        ownership of anything (audit 2026-09-13, finding 1). The optional
        summary must be the bounded typed observation; agent-authored
        free-form JSON refuses.

        The answer carries the clerk's own lifecycle so a live lane learns it
        was drained and marks its evidence (#2155); observing a draining
        clerk succeeds — the heartbeat is the lesson's channel, and the lane
        keeps beating until the operator finishes the ceremony.
        """
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkLaneRetired(
                f"Clerk {clerk_id} is retired.",
                next_step="Stop this lane's bots and its heartbeat; a retired "
                "clerk's identity is never reinstated.",
            )
        summary_json = None
        if reported_summary is not None:
            try:
                summary_json = ProviderSummaryObservation.parse(reported_summary).to_json()
            except ValueError as exc:
                raise ClerkIdentityMismatch(
                    f"Clerk {clerk_id} reported a lane summary that is not a "
                    f"bounded typed observation: {exc}",
                    next_step="The lane summary vocabulary is endpoint_mode, "
                    "authority_state, a short detail line and an optional "
                    "account nickname.",
                ) from exc
        touched = False
        with self._store.transaction() as conn:
            # The touch and the lifecycle answer read one transactional view:
            # a clerk retired between the lock-free pre-read above and this
            # BEGIN IMMEDIATE refuses here instead of touching a dead lane's
            # session, and the answer can never carry a lifecycle a
            # concurrent ceremony already superseded.
            live = self._clerk_on_or_unknown(conn, clerk_id)
            if live.lifecycle_state == StoredLifecycleState.RETIRED:
                raise ClerkLaneRetired(
                    f"Clerk {clerk_id} is retired.",
                    next_step="Stop this lane's bots and its heartbeat; a retired "
                    "clerk's identity is never reinstated.",
                )
            touched = self._store.touch_session(
                conn,
                clerk_id=clerk_id,
                agent_instance_id=agent_instance_id,
                last_seen_at_ms=self._clock(),
                reported_binding_generation=reported_binding_generation,
                reported_account_id=reported_account_id,
                reported_state=reported_state,
                reported_summary_json=summary_json,
            )
        return SessionObservation(
            touched=touched, lifecycle_state=live.lifecycle_state
        )

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
        ownership (FR-054). Re-reserving is defined for the same owner in
        both live states: a reserved row returns as-is, and an effective row
        is the same-owner resume of a restarted clerk, returning the
        confirmed facts untouched (audit 2026-09-13, finding 1). A released
        row is a transfer — release followed by reserve is the reassignment
        ceremony's two-step reach — and carries that ceremony's gate: it
        moves only if its release was covered by the predecessor's lane-quiet
        confirmation (``_reserve_released_account``, #2157, #2154).

        ``volume_root`` is the co-located caller's re-proof of the mounted
        root. It is optional because the coordinator process may not have
        the volume mounted at all; the agent's own gate
        (``fleet_boot.open_fleet_lane``) is the authority, and
        ``LocalPresence`` — the one transport that does share the filesystem
        — always supplies it. On the released-row transfer path it stops
        being optional: a transfer without the successor's volume proof
        refuses (#2157).
        """
        self._require_recovery_hold_clear()
        if not broker:
            raise BrokerAndClerkRequired(
                "A reservation requires a broker.",
                next_step="Include broker in the reservation request; no "
                "default provider is assumed.",
            )
        clerk = self._require_clerk(clerk_id)
        if clerk.broker != broker:
            raise ClerkBrokerMismatch(
                f"Clerk {clerk_id} belongs to broker {clerk.broker!r}, not {broker!r}.",
                next_step=_BROKER_IMMUTABLE_NEXT_STEP,
            )
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            # A retired clerk's routes are gone for good: to the fleet it is
            # simply absent, not a conflict to resolve.
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired; only a provisioned clerk reserves accounts.",
                next_step="Provision a new clerk to reserve an account; a "
                "retired clerk's routes are gone for good.",
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
                next_step="Verify the external account id with the provider "
                "directly; this deployment cannot route to an empty canonical "
                "identity.",
            )
        if volume_root is not None:
            self._verify_volume(clerk, volume_root)

        now = self._clock()
        # The read-decide-write runs inside one transaction with a lifecycle
        # recheck, so a concurrent retirement cannot interleave: whichever
        # write transaction commits first is the truth the other re-reads.
        outcome: AccountAssignmentRecord | None = None
        try:
            with self._store.transaction() as conn:
                live_clerk = self._store.read_clerk_on(conn, clerk_id)
                if live_clerk is None or live_clerk.lifecycle_state != (
                    StoredLifecycleState.PROVISIONED
                ):
                    raise ClerkAssignmentConflict(
                        f"Clerk {clerk_id} is no longer provisioned; only a "
                        "provisioned clerk reserves accounts.",
                    )
                existing = self._store.read_assignment_on(
                    conn, broker=broker, canonical_account_id=canonical
                )
                if existing is None:
                    self._require_no_live_assignment(conn, clerk_id)
                    outcome = AccountAssignmentRecord(
                        broker=broker,
                        canonical_external_account_id=canonical,
                        clerk_id=clerk_id,
                        assignment_generation=1,
                        state=AssignmentState.RESERVED,
                        recorded_at_ms=now,
                        updated_at_ms=now,
                    )
                    self._store.insert_assignment(conn, outcome)
                elif existing.clerk_id == clerk_id and existing.state in (
                    AssignmentState.RESERVED,
                    AssignmentState.EFFECTIVE,
                ):
                    # Same-owner resume: an idempotent re-reservation by the
                    # owning clerk (restart recovery, lost reply). The
                    # confirmed observation on an effective row survives
                    # untouched — a resume is not a re-confirmation.
                    outcome = existing
                elif existing.state == AssignmentState.RELEASED:
                    outcome = self._reserve_released_account(
                        conn,
                        existing=existing,
                        clerk_id=clerk_id,
                        volume_root=volume_root,
                        now=now,
                    )
                else:
                    raise ClerkAssignmentConflict(
                        f"Account {canonical} under broker {broker!r} is already "
                        f"{existing.state.value} for clerk {existing.clerk_id}.",
                        next_step="Assignment ownership does not expire; reassignment "
                        "is a proof-driven host ceremony.",
                    )
        except sqlite3.IntegrityError as exc:
            # Another writer's reservation committed before this transaction
            # took the write lock; the constraint is the fence, and the loser
            # gets the same typed refusal a sequential rival would.
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} was reserved "
                "concurrently by another clerk.",
                next_step="Assignment ownership does not expire; reassignment "
                "is a proof-driven host ceremony.",
            ) from exc
        assert outcome is not None
        logger.info(
            "fleet account assignment reserved",
            extra={"broker": broker, "clerk_id": clerk_id},
        )
        return outcome

    def _reserve_released_account(
        self,
        conn: sqlite3.Connection,
        *,
        existing: AccountAssignmentRecord,
        clerk_id: str,
        volume_root: Path | None,
        now: int,
        attestation: tuple[str, str] | None = None,
    ) -> AccountAssignmentRecord:
        """Reserve a released account for a new clerk — a transfer (#2157, #2154).

        A released row is a clerk-to-clerk transfer wearing a reservation's
        clothes, so it carries the reassignment ceremony's gate rather than a
        second, weaker policy — and ends where that ceremony ends, with the
        old lane retired. Two facts, both durable: the old lane is retired,
        so it can never re-register or be routed to; and its release carried
        ``lane_confirmation: present`` — it proved it had stopped, cancelled,
        flattened and resolved every intent, over this account (schema v7
        makes that account unambiguous), inside the drain it could no longer
        leave. An ``absent`` release — a lane that could not answer — stays
        refused, because nothing says that lane stopped writing.
        """
        canonical = existing.canonical_external_account_id
        broker = existing.broker
        if volume_root is None:
            # "The coordinator may not have the volume mounted" is a reason
            # to refuse a transfer, not to skip its proof.
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} is released; "
                "reserving it is a clerk-to-clerk transfer, and a transfer "
                "requires the successor's volume proof (volume_root) (#2157).",
                next_step="Run the host reassignment ceremony with the "
                "successor's volume root; a remote reservation never transfers "
                "a released account.",
            )
        predecessor = self._store.read_clerk_on(conn, existing.clerk_id)
        if predecessor is None or predecessor.lifecycle_state == StoredLifecycleState.PROVISIONED:
            # Only the release ceremony produces released rows, and it
            # requires a drained, past-deadline owner — a released row under a
            # provisioned or absent clerk is registry inconsistency, reported
            # rather than repaired.
            holder = "no registry row" if predecessor is None else "still provisioned"
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} is released under "
                f"clerk {existing.clerk_id}, which has {holder}; a released "
                "assignment belongs to a lane the release ceremony drained.",
                next_step="Restore the registry from the coordinator control "
                "volume's backup, then retry.",
            )
        release = self._released_history_record(
            broker=broker, canonical=canonical, generation=existing.assignment_generation
        )
        if release.lane_confirmation != LaneConfirmationState.PRESENT:
            raise ClerkReassignmentBlocked(
                f"Account {canonical} under broker {broker!r} was released without "
                f"a lane-quiet confirmation from clerk {existing.clerk_id}; nothing "
                "proves that lane stopped writing, so the account does not move "
                "to another lane (#2157, #2154).",
                next_step="A released account moves only when its release was "
                "covered by the draining lane's own lane-quiet confirmation. Use "
                "whole-machine migration, which needs no successor (#2151).",
            )
        if predecessor.lifecycle_state == StoredLifecycleState.DRAINING:
            # Checked after the release's own evidence: an absent release can
            # never move, a draining lane only has to finish. The release proved the lane quiet, but the lane is still alive
            # and still reads this account: retiring it first ends it for
            # good (it can never re-register or be routed to again), and it
            # retires on a quiet answer before a successor can trade and make
            # that answer impossible.
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} is released, but "
                f"clerk {existing.clerk_id}, which held it, is still draining; a "
                "released account moves only once its old lane is retired.",
                next_step="Retire the old lane (`retire`; it confirms lane quiet "
                "on its heartbeat), then reserve again.",
            )
        self._require_no_live_assignment(conn, clerk_id)
        if not self._store.cas_update_assignment(
            conn,
            self._successor_reservation(
                existing, clerk_id=clerk_id, now=now, attestation=attestation
            ),
            previous_generation=existing.assignment_generation,
            previous_state=AssignmentState.RELEASED,
        ):
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} changed while reserving.",
            )
        return self._persisted_assignment_on(conn, broker=broker, canonical=canonical)

    def _persisted_assignment_on(
        self, conn: sqlite3.Connection, *, broker: str, canonical: str
    ) -> AccountAssignmentRecord:
        """The row a transfer left behind, read back rather than restated.

        The compare-and-swap keeps the account row's original
        ``recorded_at_ms``, so the record a transfer built is not the one it
        stored; callers get the stored one.
        """
        persisted = self._store.read_assignment_on(
            conn, broker=broker, canonical_account_id=canonical
        )
        assert persisted is not None
        return persisted

    @staticmethod
    def _successor_reservation(
        released: AccountAssignmentRecord,
        *,
        clerk_id: str,
        now: int,
        attestation: tuple[str, str] | None = None,
    ) -> AccountAssignmentRecord:
        """The next generation's reservation of an account, for a new clerk.

        ``attestation`` is the host ceremony's bounded ``(operator,
        change_ref)``, recorded on the reservation's history row when an
        operator moved the account; a lane's own reservation has none.
        """
        operator, change_ref = attestation if attestation is not None else (None, None)
        return AccountAssignmentRecord(
            broker=released.broker,
            canonical_external_account_id=released.canonical_external_account_id,
            clerk_id=clerk_id,
            assignment_generation=released.assignment_generation + 1,
            state=AssignmentState.RESERVED,
            recorded_at_ms=now,
            updated_at_ms=now,
            attested_operator=operator,
            attested_change_ref=change_ref,
            attested_at_ms=None if attestation is None else now,
        )

    def _require_no_live_assignment(self, conn: sqlite3.Connection, clerk_id: str) -> None:
        """A successor acquiring an account may hold no other live one.

        Schema v7's unique owner index backstops this; the check is here so
        the refusal names the successor instead of surfacing as a constraint
        failure that reads like a concurrent reservation.
        """
        if any(
            assignment.state != AssignmentState.RELEASED
            for assignment in self._store.list_assignments_for_clerk_on(conn, clerk_id)
        ):
            raise ClerkAssignmentConflict(
                f"Clerk {clerk_id} already holds a live assignment; one lane "
                "holds one account.",
            )

    def confirm_assignment(
        self,
        *,
        broker: str,
        clerk_id: str,
        external_account_id: str,
        binding_generation: int,
        agent_instance_id: str,
        routing_epoch: int,
        effective_profile_id: str | None = None,
        effective_revision: int | None = None,
    ) -> AccountAssignmentRecord:
        """Record the worker's confirmed binding observation (PRD FR-064).

        One transaction compares the authenticated clerk, the *current*
        session's instance and epoch, the reserved-or-effective assignment
        owner, and the proposed binding facts (audit 2026-09-13, finding 1):

        - a confirmation from a superseded session refuses atomically;
        - a generation lower than the confirmed one refuses — restoring old
          evidence is its own ceremony, not a confirmation;
        - an equal generation re-acknowledges (crash recovery converging on
          the same identity, including after a session replacement);
        - a higher generation advances the confirmed observation, which is
          the normal Stage → Apply → restart progression.

        The clerk-local selection transaction remains the authority for the
        effective tuple; this row is the coordinator's confirmed observation
        of it and the fence routed commands are checked against.
        """
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        if clerk.broker != broker:
            raise ClerkBrokerMismatch(
                f"Clerk {clerk_id} belongs to broker {clerk.broker!r}, not {broker!r}.",
                next_step=_BROKER_IMMUTABLE_NEXT_STEP,
            )
        # The cheap pre-read refusal; the authoritative re-read runs inside the
        # write transaction below, past any concurrently committing drain.
        self._refuse_confirmation_for_closed_lane(clerk)
        if binding_generation < 1:
            raise ClerkBindingGenerationConflict(
                "A binding generation is a positive integer.",
            )
        if (effective_profile_id is None) != (effective_revision is None):
            raise ValueError(
                "an effective tuple carries both profile id and revision, or neither"
            )
        adapter = self._adapter(broker)
        canonical = adapter.canonical_account_id(external_account_id)
        now = self._clock()
        outcome: AccountAssignmentRecord | None = None
        with self._store.transaction() as conn:
            # The session check lives inside the write transaction: a
            # superseded instance must not slip its confirmation between the
            # read and the commit. The lifecycle gate shares the transaction
            # for the same reason — a drain committing between the lock-free
            # pre-read above and this BEGIN IMMEDIATE still refuses the
            # confirmation here (#2155).
            self._refuse_confirmation_for_closed_lane(
                self._clerk_on_or_unknown(conn, clerk_id)
            )
            session = self._store.read_session_on(conn, clerk_id)
            if session is None:
                raise ClerkUnreachable(
                    f"Clerk {clerk_id} has no registered agent session.",
                    next_step="Have the agent register a session before "
                    "confirming; retry once registration completes.",
                )
            if (
                session.agent_instance_id != agent_instance_id
                or session.routing_epoch != routing_epoch
            ):
                raise ClerkIdentityMismatch(
                    f"The confirmation for clerk {clerk_id} was prepared by session "
                    f"{agent_instance_id}/{routing_epoch}, but the current session is "
                    f"{session.agent_instance_id}/{session.routing_epoch}; a "
                    "superseded session cannot confirm.",
                    next_step="The replacement session re-confirms from the clerk's "
                    "current effective binding.",
                )
            existing = self._store.read_assignment_on(
                conn, broker=broker, canonical_account_id=canonical
            )
            if existing is None or existing.clerk_id != clerk_id:
                raise ClerkAssignmentConflict(
                    f"Account {canonical} under broker {broker!r} is not reserved for "
                    f"clerk {clerk_id}.",
                )
            if existing.state == AssignmentState.RELEASED:
                raise ClerkAssignmentConflict(
                    f"Account {canonical} under broker {broker!r} was released; a "
                    "released assignment is re-reserved, never re-confirmed.",
                )
            if (
                existing.state == AssignmentState.EFFECTIVE
                and existing.confirmed_binding_generation is not None
                and binding_generation < existing.confirmed_binding_generation
            ):
                raise ClerkBindingGenerationConflict(
                    f"Clerk {clerk_id} confirmed binding generation "
                    f"{existing.confirmed_binding_generation}; the stale generation "
                    f"{binding_generation} refuses — restoring superseded evidence "
                    "is its own ceremony.",
                    next_step="Re-read the clerk's current effective binding and "
                    "confirm that.",
                )
            if (
                existing.state == AssignmentState.EFFECTIVE
                and existing.confirmed_binding_generation == binding_generation
                and (
                    effective_profile_id,
                    effective_revision,
                )
                != (existing.confirmed_profile_id, existing.confirmed_revision)
            ):
                # One generation names one tuple: a changed tuple is a new
                # generation, and a retry that omits the tuple cannot clear
                # what a full confirmation recorded.
                raise ClerkBindingGenerationConflict(
                    f"Clerk {clerk_id}'s generation {binding_generation} confirms "
                    f"tuple ({existing.confirmed_profile_id!r}, "
                    f"{existing.confirmed_revision!r}); the same generation cannot "
                    "confirm a different or partial tuple.",
                    next_step="A changed effective tuple advances the binding "
                    "generation; re-confirm with the clerk's current tuple.",
                )
            confirmed = replace(
                existing,
                state=AssignmentState.EFFECTIVE,
                effective_profile_id=effective_profile_id,
                effective_revision=effective_revision,
                confirmed_binding_generation=binding_generation,
                confirmed_profile_id=effective_profile_id,
                confirmed_revision=effective_revision,
                confirmed_at_ms=now,
                confirmed_agent_instance_id=agent_instance_id,
                confirmed_routing_epoch=routing_epoch,
                updated_at_ms=now,
            )
            accepted = self._store.cas_update_assignment(
                conn,
                confirmed,
                previous_generation=existing.assignment_generation,
                previous_state=existing.state,
            )
            if not accepted:
                raise ClerkAssignmentConflict(
                    f"Account {canonical} under broker {broker!r} changed while "
                    "confirming; re-read and retry.",
                )
            # The confirmation above is the write this method is named for
            # (the coordinator's confirmed observation, in
            # ``account_assignments``); the session's reported facts are a
            # separate write this transaction also makes, so the directory
            # projects the acknowledged binding immediately rather than wait
            # for the next heartbeat to say the same thing.
            self._store.touch_session(
                conn,
                clerk_id=clerk_id,
                agent_instance_id=agent_instance_id,
                last_seen_at_ms=now,
                reported_binding_generation=binding_generation,
                reported_account_id=canonical,
                reported_state="binding_confirmed",
            )
            outcome = confirmed
        assert outcome is not None
        logger.info(
            "fleet account assignment confirmed",
            extra={"broker": broker, "clerk_id": clerk_id},
        )
        return outcome

    def release_assignment(
        self,
        *,
        broker: str,
        external_account_id: str,
        expected_assignment_generation: int,
        operator: str,
        change_ref: str,
    ) -> AccountAssignmentRecord:
        """The host-only release ceremony (PRD FR-055, ADR 0063 Decisions 4/4.1).

        Terminal for the generation: the row records ``released`` and stays
        in the append-only history, which is what makes "never expire into
        takeover" auditable. Reuse of the account starts a fresh reservation
        with a higher generation. The caller pins the assignment generation
        its evidence was prepared against, so release evidence prepared for
        generation N cannot silently release generation N+1's new owner.

        The fixed proof phrase is gone: the ceremony now requires a bounded,
        attributable ``operator``/``change_ref`` — attribution, not proof,
        in the shape ``closeout_empty_registry_recovery`` established — and
        the owning clerk must be draining, command-quiet, and past the
        drain deadline that bounds ``force-retire``. The released history row
        records ``lane_confirmation: present`` when a fresh, quiet
        confirmation from the owner's current session covers the release
        (#2154), and ``absent`` otherwise, so an auditor can count how many
        handovers proceeded without it — and a later re-reservation of this
        account reads exactly that fact.
        """
        bounded_operator, bounded_change_ref = _bounded_attestation(operator, change_ref)
        self._require_recovery_hold_clear()
        adapter = self._adapter(broker)
        canonical = adapter.canonical_account_id(external_account_id)
        existing = self._store.read_assignment(broker=broker, canonical_account_id=canonical)
        if existing is None:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} has no assignment to release.",
            )
        if existing.assignment_generation != expected_assignment_generation:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} is at assignment "
                f"generation {existing.assignment_generation}, not the pinned "
                f"{expected_assignment_generation}; release evidence cannot cross "
                "a reassignment.",
                next_step="Re-read the assignment and re-prepare the release evidence.",
            )
        if existing.state == AssignmentState.RELEASED:
            # Idempotent, and audited the same both times: the retry returns
            # the persisted release history row — which carries the
            # ``lane_confirmation`` and attribution the current-row pointer
            # does not store — so a repeated ceremony reports the same facts
            # the first one recorded, never null audit columns.
            return self._released_history_record(
                broker=broker, canonical=canonical, generation=expected_assignment_generation
            )
        now = self._clock()
        released = replace(
            existing,
            state=AssignmentState.RELEASED,
            updated_at_ms=now,
            attested_operator=bounded_operator,
            attested_change_ref=bounded_change_ref,
            attested_at_ms=now,
        )
        accepted = False
        with self._store.transaction() as conn:
            # The predecessor gates read inside the same write transaction as
            # the transition: a drain entered (or an attempt opened) between
            # the outer read and this commit is the state this ceremony
            # decides against, not a race it silently wins (ADR 0063 §4.1).
            live_assignment = self._store.read_assignment_on(
                conn, broker=broker, canonical_account_id=canonical
            )
            if live_assignment is None or live_assignment.state != existing.state:
                raise ClerkAssignmentConflict(
                    f"Account {canonical} under broker {broker!r} changed while releasing.",
                )
            predecessor = self._require_draining_predecessor_past_deadline(
                conn, existing.clerk_id, now=now
            )
            self._require_command_quiet(conn, existing.clerk_id)
            released = replace(
                released,
                lane_confirmation=self._handover_lane_confirmation(
                    conn, predecessor, now=now
                ),
            )
            accepted = self._store.cas_update_assignment(
                conn, released, previous_generation=existing.assignment_generation,
                previous_state=existing.state,
            )
        if not accepted:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} changed while releasing.",
            )
        logger.info(
            "fleet account assignment released",
            extra={"broker": broker, "operator": bounded_operator},
        )
        # Both paths answer with the persisted history row: the read-back is
        # itself a check that the ceremony's durable facts landed, and a
        # retry of the completed ceremony returns byte-identical audit facts.
        return self._released_history_record(
            broker=broker, canonical=canonical, generation=expected_assignment_generation
        )

    def reassign_assignment(
        self,
        *,
        broker: str,
        external_account_id: str,
        expected_assignment_generation: int,
        operator: str,
        change_ref: str,
        successor_clerk_id: str,
        successor_volume_root: Path,
    ) -> AccountAssignmentRecord:
        """Move one account from a drained lane to a new one (ADR 0063 §4.1/§7.1).

        Every check names its first outstanding item: the successor's marked
        volume is verified, its broker must match, it may hold no live
        assignment, the generation pin holds, and the predecessor must be
        draining, command-quiet and past the drain deadline. Past all of
        those, the predecessor's current session must hold a fresh, quiet
        lane-quiet confirmation (#2154). That confirmation is also what
        answers §7.1: a lane only confirms after it has learned its drain,
        and learning it tombstones the volume evidence an offline boot would
        otherwise resurrect, so a confirming lane is exactly one the drain
        reached. A lane that cannot answer is never reassigned.

        An account already released moves through the same transfer a
        co-located successor's reservation makes (``_reserve_released_account``:
        the release recorded ``present`` and the old lane is retired). This is
        the reachable path for it in the remote topology, where a lane's own
        boot carries no volume proof.

        The release, the successor's reservation and the old lane's
        retirement (``lane_confirmation: present``) commit together, so no
        split handover is ever observable and the old lane is never left
        draining beside a successor that trades its account. Whole-machine
        migration (#2151) remains the preferred lane move and needs no
        successor at all.
        """
        bounded_operator, bounded_change_ref = _bounded_attestation(operator, change_ref)
        self._require_recovery_hold_clear()
        successor = self._require_clerk(successor_clerk_id)
        if successor.broker != broker:
            raise ClerkBrokerMismatch(
                f"Successor clerk {successor_clerk_id} belongs to broker "
                f"{successor.broker!r}, not {broker!r}.",
                next_step=_BROKER_IMMUTABLE_NEXT_STEP,
            )
        self._verify_volume(successor, successor_volume_root)
        canonical = self._adapter(broker).canonical_account_id(external_account_id)
        now = self._clock()
        try:
            with self._store.transaction() as conn:
                existing = self._store.read_assignment_on(
                    conn, broker=broker, canonical_account_id=canonical
                )
                if existing is None:
                    raise ClerkAssignmentConflict(
                        f"Account {canonical} under broker {broker!r} has no assignment to reassign.",
                    )
                if existing.clerk_id == successor_clerk_id:
                    raise ClerkAssignmentConflict(
                        "A reassignment requires a distinct successor clerk.",
                        next_step="Use normal same-owner recovery for the original clerk.",
                    )
                live_successor = self._store.read_clerk_on(conn, successor_clerk_id)
                if (
                    live_successor is None
                    or live_successor.lifecycle_state != StoredLifecycleState.PROVISIONED
                ):
                    raise ClerkAssignmentConflict(
                        f"Successor clerk {successor_clerk_id} is no longer provisioned; "
                        "it cannot receive a reassignment.",
                    )
                self._require_no_live_assignment(conn, successor_clerk_id)
                if existing.assignment_generation != expected_assignment_generation:
                    raise ClerkAssignmentConflict(
                        f"Account {canonical} under broker {broker!r} is at assignment "
                        f"generation {existing.assignment_generation}, not the pinned "
                        f"{expected_assignment_generation}; reassignment evidence cannot cross "
                        "an ownership change.",
                    )
                if existing.state == AssignmentState.RELEASED:
                    # Already released, so the release recorded its evidence;
                    # this is the transfer a co-located successor's own boot
                    # would make, reached from the host because this ceremony
                    # carries the successor's volume proof and a remote boot
                    # (``RemotePresence``) never can. Same gate: the release
                    # recorded ``present`` and the old lane is retired.
                    moved = self._reserve_released_account(
                        conn,
                        existing=existing,
                        clerk_id=successor_clerk_id,
                        volume_root=successor_volume_root,
                        now=now,
                        attestation=(bounded_operator, bounded_change_ref),
                    )
                else:
                    # §4.1's predecessor preconditions, in order, each naming the
                    # first outstanding item.
                    predecessor = self._require_draining_predecessor_past_deadline(
                        conn, existing.clerk_id, now=now
                    )
                    self._require_command_quiet(conn, existing.clerk_id)
                    self._require_lane_quiet(conn, predecessor, now=now)
                    released = replace(
                        existing,
                        state=AssignmentState.RELEASED,
                        updated_at_ms=now,
                        lane_confirmation=LaneConfirmationState.PRESENT,
                        attested_operator=bounded_operator,
                        attested_change_ref=bounded_change_ref,
                        attested_at_ms=now,
                    )
                    self._store.reassign_assignment(
                        conn,
                        released=released,
                        reserved_successor=self._successor_reservation(
                            existing, clerk_id=successor_clerk_id, now=now
                        ),
                        previous_state=existing.state,
                    )
                    # The handover ends the old lane in the same commit, on the
                    # quiet answer just accepted: it holds nothing now (one live
                    # assignment per clerk), and once the successor trades, its
                    # account would never read quiet to it again.
                    if not self._store.retire_clerk_row(
                        conn,
                        clerk_id=existing.clerk_id,
                        retired_at_ms=now,
                        lane_confirmation=LaneConfirmationState.PRESENT,
                    ):
                        raise ClerkAssignmentConflict(
                            f"Clerk {existing.clerk_id} changed while reassigning; "
                            "re-read and retry.",
                        )
                    moved = self._persisted_assignment_on(
                        conn, broker=broker, canonical=canonical
                    )
        except sqlite3.IntegrityError as exc:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} changed while reassignment "
                "was preparing the successor; the original ownership remains intact.",
            ) from exc
        logger.info(
            "fleet account assignment reassigned",
            extra={
                "broker": broker,
                "clerk_id": successor_clerk_id,
                "predecessor_clerk_id": existing.clerk_id,
                "operator": bounded_operator,
            },
        )
        return moved

    # ---- routing -----------------------------------------------------------

    def resolve_route(
        self,
        *,
        broker: str,
        clerk_id: str,
        expected_routing_epoch: int | None = None,
        expected_binding_generation: int | None = None,
        expected_account_id: str | None = None,
        readiness: OperationReadiness = OperationReadiness.EXECUTION,
        routable_while_draining: bool = False,
    ) -> tuple[ClerkRecord, ClerkSessionRecord, AccountAssignmentRecord | None]:
        """Resolve a routed operation's clerk, verifying path identities.

        ``clerk_id`` is resolved first, then the path broker must equal the
        clerk's immutable broker (PRD FR-071). A retired clerk and a clerk
        with no live session are not routable. When the caller pins an epoch
        or a binding generation, a mismatch refuses rather than silently
        retargeting (FR-073/078).

        A draining clerk routes only what ``routable_while_draining`` admits
        — the operation's own ``ProviderOperation.routable_while_draining``,
        the one allow-list (#2351): its reads and the quiesce actions ADR
        0063 §2 has the operator make it quiet with. Everything that could
        open exposure refuses, so the drain still closes the door on new
        work; the default refuses, so a caller that names nothing is closed.

        Readiness closes the admission gap (audit 2026-09-13, finding 1):
        ``execution`` operations route only against the *confirmed* effective
        assignment — a heartbeat carrying plausible binding facts proves
        nothing — while ``configuration_access`` operations stay routable for
        a provisioned lane whose binding is missing or broken, so the operator
        can reach the configuration surface that would produce one.
        """
        self._require_recovery_hold_clear()
        if not broker or not clerk_id:
            raise BrokerAndClerkRequired(
                "A clerk-scoped operation requires both broker and clerk identity.",
                next_step="Call this route with both the broker and clerk_id "
                "path segments populated.",
            )
        clerk = self._require_clerk(clerk_id)
        if clerk.broker != broker:
            raise ClerkBrokerMismatch(
                f"Route broker {broker!r} does not match clerk {clerk_id}'s "
                f"immutable broker {clerk.broker!r}.",
                next_step=_BROKER_IMMUTABLE_NEXT_STEP,
            )
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired; its routes are gone.",
                next_step="Provision a new clerk; a retired clerk's routes are "
                "gone for good.",
            )
        if (
            clerk.lifecycle_state == StoredLifecycleState.DRAINING
            and not routable_while_draining
        ):
            raise ClerkUnreachable(
                f"Clerk {clerk_id} is draining; it routes only reads and the "
                "actions that stop its bots or reduce exposure.",
                next_step="Stop the lane's bots, cancel its working orders and "
                "flatten; nothing new starts on a draining lane.",
            )
        session = self._store.read_session(clerk_id)
        if session is None:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} has no registered agent session.",
                next_step="Wait for the agent to register a session, then retry.",
            )
        if self._clock() - session.last_seen_at_ms > self._session_stale_after_ms:
            raise ClerkUnreachable(
                f"Clerk {clerk_id}'s last heartbeat is older than "
                f"{self._session_stale_after_ms} ms; an unreachable lane accepts no "
                "routed operations until it recovers.",
                next_step="Wait for the agent to send a fresh heartbeat, then "
                "retry.",
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
        if readiness == OperationReadiness.CONFIGURATION_ACCESS:
            return clerk, session, None

        effective = self._store.list_effective_assignments_for_clerk(clerk_id)
        if not effective:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} holds no effective account assignment; "
                "execution routing stays closed until a confirmed binding exists.",
                next_step="Wait for a confirmed binding before routing; "
                "execution stays closed until one exists.",
            )
        if len(effective) > 1:
            raise ClerkIdentityMismatch(
                f"Clerk {clerk_id} holds {len(effective)} effective assignments; "
                "one lane serves exactly one confirmed account.",
            )
        assignment = effective[0]
        if assignment.confirmed_binding_generation is None:
            raise ClerkUnreachable(
                f"Clerk {clerk_id}'s assignment for "
                f"{assignment.canonical_external_account_id} has no confirmed "
                "binding observation; a starting lane is not command-routable.",
                next_step="Wait for the agent to confirm its binding before "
                "retrying.",
            )
        if (
            assignment.confirmed_agent_instance_id != session.agent_instance_id
            or assignment.confirmed_routing_epoch != session.routing_epoch
        ):
            # A replacement session inherits nothing: until it re-confirms
            # from the clerk's current effective binding, the lane is still
            # starting and not command-routable (audit 2026-09-13, finding 1).
            raise ClerkUnreachable(
                f"Clerk {clerk_id}'s confirmed binding was presented by session "
                f"{assignment.confirmed_agent_instance_id}/"
                f"{assignment.confirmed_routing_epoch}; the current session "
                f"{session.agent_instance_id}/{session.routing_epoch} has not "
                "re-confirmed it.",
                next_step="Wait for the current session to re-confirm its binding before retrying.",
            )
        if (
            expected_binding_generation is not None
            and assignment.confirmed_binding_generation != expected_binding_generation
        ):
            raise ClerkBindingGenerationConflict(
                f"Expected binding generation {expected_binding_generation} is not "
                f"clerk {clerk_id}'s confirmed generation "
                f"{assignment.confirmed_binding_generation}; re-prepare the command "
                "against the lane's current resource.",
            )
        if expected_account_id is not None:
            adapter = self._adapter(broker)
            canonical_expected = adapter.canonical_account_id(expected_account_id)
            if canonical_expected != assignment.canonical_external_account_id:
                raise ClerkAccountMismatch(
                    f"Account {canonical_expected} is not clerk {clerk_id}'s "
                    f"confirmed account {assignment.canonical_external_account_id}.",
                    next_step="Re-read the clerk's confirmed account before "
                    "retrying; the command's expected account no longer matches.",
                )
        return clerk, session, assignment

    # ---- routing attempts ---------------------------------------------------

    def open_routing_attempt(
        self,
        *,
        broker: str,
        clerk_id: str,
        operation_kind: str,
        nonsecret_target_ref: str,
        idempotency_key: str,
        pinned_routing_epoch: int,
        pinned_agent_instance_id: str,
        pinned_binding_generation: int | None = None,
    ) -> RoutingReceiptRecord:
        """Persist one routing attempt's pinned context *before* dispatch.

        The attempt is recorded as ``not_dispatched`` with the epoch and
        instance of the session the dispatch is fenced by — both mandatory,
        because a receipt that cannot prove which process received the
        command cannot stop a retry from crossing a restart — plus the
        confirmed binding generation when the operation carries one. A crash
        between decision and delivery leaves provable "never sent" evidence
        (audit 2026-09-13, finding 7). Retrying the same lane-scoped
        idempotency key returns the existing attempt — unless the retry names
        a different operation, target or pinned context, which is a conflict,
        never a silent cross-attribution.

        This ledger covers **commands only** (ADR 0063): routed streams open
        no receipt, so receipt silence is never lane silence — a lane serving
        open streams reads as quiet here. Streams gain their own attempt
        record only if they are ever to count as drain evidence; until then
        every consumer reasoning about lane activity must say this scope in
        its own docstring (#2153).
        """
        now = self._clock()
        existing = self._store.find_routing_receipt_by_idempotency(
            broker=broker, clerk_id=clerk_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            self._require_attempt_context_match(
                existing,
                operation_kind=operation_kind,
                nonsecret_target_ref=nonsecret_target_ref,
                pinned_routing_epoch=pinned_routing_epoch,
                pinned_binding_generation=pinned_binding_generation,
                pinned_agent_instance_id=pinned_agent_instance_id,
            )
            return existing
        receipt = RoutingReceiptRecord(
            correlation_id=new_correlation_id(),
            broker=broker,
            clerk_id=clerk_id,
            operation_kind=operation_kind,
            nonsecret_target_ref=nonsecret_target_ref,
            idempotency_key=idempotency_key,
            state=RoutingReceiptState.NOT_DISPATCHED,
            pinned_routing_epoch=pinned_routing_epoch,
            pinned_binding_generation=pinned_binding_generation,
            pinned_agent_instance_id=pinned_agent_instance_id,
            created_at_ms=now,
            updated_at_ms=now,
        )
        try:
            with self._store.transaction() as conn:
                self._store.insert_routing_receipt(conn, receipt)
        except sqlite3.IntegrityError:
            # A concurrent retry with the same lane-scoped key inserted first;
            # resolve to the winner rather than surfacing the constraint.
            winner = self._store.find_routing_receipt_by_idempotency(
                broker=broker, clerk_id=clerk_id, idempotency_key=idempotency_key
            )
            if winner is None:
                raise
            self._require_attempt_context_match(
                winner,
                operation_kind=operation_kind,
                nonsecret_target_ref=nonsecret_target_ref,
                pinned_routing_epoch=pinned_routing_epoch,
                pinned_binding_generation=pinned_binding_generation,
                pinned_agent_instance_id=pinned_agent_instance_id,
            )
            return winner
        return receipt

    def mark_routing_dispatched(self, *, correlation_id: str) -> RoutingReceiptRecord:
        """Claim the attempt's one dispatch to the provider clerk.

        One-way: once dispatched, an attempt can never present as
        definitively un-sent. The claim is exclusive, not idempotent: an
        attempt already dispatched and not settled is either still in flight
        or its outcome was lost (ADR 0063's reconciliation obligation), and a
        same-key retry must reconcile it by identity rather than deliver the
        command a second time (ADR 0062 D11, #2319).
        """
        now = self._clock()
        with self._store.transaction() as conn:
            claimed = self._store.mark_routing_receipt_dispatched(
                conn, correlation_id=correlation_id, dispatched_at_ms=now
            )
        receipt = self._store.read_routing_receipt(correlation_id)
        if receipt is None:
            raise ClerkRoutingAttemptConflict(
                f"No routing attempt carries correlation {correlation_id!r}.",
            )
        if not claimed:
            raise ClerkRoutingOutcomeUnknown(
                f"Idempotency key {receipt.idempotency_key} was already "
                f"dispatched as attempt {correlation_id} and is "
                f"{receipt.state.value}: it is still in flight, or its outcome "
                "was lost; reconcile with the provider clerk's receipt.",
                next_step="Read the command's outcome by its durable identity; "
                "never resubmit the same key.",
            )
        return receipt

    def settle_routing_attempt(
        self,
        *,
        correlation_id: str,
        outcome: RoutingReceiptState,
        upstream_receipt_ref: str | None = None,
    ) -> RoutingReceiptRecord:
        """Settle one attempt's outcome under the terminal-outcome rules.

        ``delivered`` (with the provider's durable receipt reference) is
        terminal — a late failed retry can never erase it. ``outcome_unknown``
        records that the attempt may have executed and must be reconciled by
        identity. ``provider_refused`` is a definitive provider refusal.
        Settling back to ``not_dispatched`` is not a settlement.
        """
        receipt = self._store.read_routing_receipt(correlation_id)
        if receipt is None:
            raise ClerkRoutingAttemptConflict(
                f"No routing attempt carries correlation {correlation_id!r}.",
            )
        if outcome == RoutingReceiptState.NOT_DISPATCHED:
            raise ClerkRoutingAttemptConflict(
                "'not_dispatched' is the pre-dispatch state, not a settlement.",
            )
        if receipt.state == RoutingReceiptState.DELIVERED and outcome != (
            RoutingReceiptState.DELIVERED
        ):
            raise ClerkRoutingAttemptConflict(
                f"Attempt {correlation_id} is delivered with upstream reference "
                f"{receipt.upstream_receipt_ref!r}; a delivered outcome is never "
                "downgraded.",
                next_step="Reconcile through the provider clerk's receipt, never by "
                "rewriting the routing attempt.",
            )
        if (
            receipt.state == RoutingReceiptState.DELIVERED
            and upstream_receipt_ref is not None
            and receipt.upstream_receipt_ref is not None
            and upstream_receipt_ref != receipt.upstream_receipt_ref
        ):
            # The first delivered provider receipt is the durable one: a late
            # or conflicting response cannot rewrite which command identity
            # the success belongs to.
            raise ClerkRoutingAttemptConflict(
                f"Attempt {correlation_id} is delivered with upstream reference "
                f"{receipt.upstream_receipt_ref!r}; a different reference "
                f"{upstream_receipt_ref!r} cannot replace it.",
                next_step="Reconcile through the provider clerk's receipt, never by "
                "rewriting the routing attempt.",
            )
        now = self._clock()
        try:
            with self._store.transaction() as conn:
                updated = self._store.update_routing_receipt_outcome(
                    conn,
                    correlation_id=correlation_id,
                    state=outcome,
                    upstream_receipt_ref=upstream_receipt_ref,
                    updated_at_ms=now,
                )
        except sqlite3.IntegrityError as exc:
            # A racing settlement moved the outcome between the read and this
            # write; the schema trigger is the fence, and the loser gets the
            # typed conflict rather than a raw constraint traceback.
            raise ClerkRoutingAttemptConflict(
                f"Attempt {correlation_id} refused by an outcome fence: {exc}",
                next_step="Re-read the attempt and reconcile through the provider "
                "clerk's receipt.",
            ) from exc
        if not updated:
            raise ClerkRoutingAttemptConflict(
                f"Attempt {correlation_id} changed while settling; re-read and retry.",
            )
        settled = self._store.read_routing_receipt(correlation_id)
        assert settled is not None
        return settled

    @staticmethod
    def _require_attempt_context_match(
        receipt: RoutingReceiptRecord,
        *,
        operation_kind: str,
        nonsecret_target_ref: str,
        pinned_routing_epoch: int | None,
        pinned_binding_generation: int | None,
        pinned_agent_instance_id: str | None,
    ) -> None:
        """Refuse an idempotency key reused under a different pinned context."""
        if (
            receipt.operation_kind != operation_kind
            or receipt.nonsecret_target_ref != nonsecret_target_ref
            or receipt.pinned_routing_epoch != pinned_routing_epoch
            or receipt.pinned_binding_generation != pinned_binding_generation
            or receipt.pinned_agent_instance_id != pinned_agent_instance_id
        ):
            raise ClerkRoutingAttemptConflict(
                f"Idempotency key {receipt.idempotency_key!r} on lane "
                f"{receipt.broker}/{receipt.clerk_id} already pins "
                f"{receipt.operation_kind} of {receipt.nonsecret_target_ref!r} at "
                f"epoch {receipt.pinned_routing_epoch}/generation "
                f"{receipt.pinned_binding_generation}; it cannot be reused for a "
                "different operation, target or pinned context.",
                next_step="Mint a fresh idempotency key for the new attempt.",
            )

    # ---- audit read surface (#2104) -----------------------------------------

    def _capability_for_operation_kind(
        self, *, broker: str, operation_kind: str, warned: set[tuple[str, str]]
    ) -> str | None:
        """The capability ``operation_kind`` currently maps to, or ``None``.

        Resolved *at read time* from the live provider catalog -- ``capability``
        and ``operation_kind`` are separate fields on ``ProviderOperation``
        (many operation ids can share one capability; the reviewed Alpaca
        catalog collapses 76 operations into 12 capabilities), and
        ``RoutingReceiptRecord`` stores only ``operation_kind``. A receipt can
        carry an ``operation_kind`` the current catalog no longer declares --
        a retired operation, or a receipt written before a rename. That is
        reported as ``None``, loudly logged, and never coerced to a wrong
        capability or allowed to fail the whole read (owner principle: fail
        loudly over silent pass).

        ``warned`` is one request's dedup set, owned by the caller (#2133
        P2-b): a page of receipts from one retired operation would otherwise
        log once *per receipt* -- up to ``limit`` warnings per poll, forever,
        for a single already-known fact. The warning still fires once per
        distinct ``(broker, operation_kind)`` per request; it is never
        silenced across requests, only de-duplicated within one.
        """
        adapter = self._provider_adapters.get(broker)
        if adapter is not None:
            for operation in adapter.operations():
                if operation.operation_id == operation_kind:
                    return operation.capability.value
        key = (broker, operation_kind)
        if key not in warned:
            warned.add(key)
            logger.warning(
                "Audit read surface found no catalog operation for a routing "
                "receipt's operation_kind; capability is unresolved.",
                extra={
                    "action": "audit_capability_unresolved",
                    "broker": broker,
                    "operation_kind": operation_kind,
                },
            )
        return None

    def list_routing_receipts(
        self,
        *,
        since_ms: int,
        clerk_id: str | None = None,
        limit: int = 100,
        before_ms: int | None = None,
        before_correlation_id: str | None = None,
    ) -> dict[str, object]:
        """The routing-receipt audit trail, at or after ``since_ms``.

        Read-only: no idempotency key, no command envelope, nothing is
        opened or settled. ``since_ms`` is an inclusive lower bound on
        ``created_at_ms``; the store already orders newest first.

        ``before_ms``/``before_correlation_id`` continue a previous page's
        keyset (#2133): a truncated page's oldest entry names them back to
        the caller as ``next_before_ms``/``next_before_correlation_id``, and
        passing them back here resumes exactly where that page ended — with
        the correlation-id tiebreak, so a page boundary landing mid-timestamp
        neither skips nor repeats a row. Without them the newest ``limit``
        receipts are unreachable-past truncation: lowering ``since_ms`` alone
        only re-selects the same newest rows.

        The projection carries every field a reconciliation needs to match an
        ``outcome_unknown`` receipt back to the provider's durable record and
        the exact process that attempted it (#2133 P1-b): the pinned attempt
        context (epoch, binding generation, agent instance), the caller's
        idempotency and target identity, and the provider's own receipt
        reference. Every field here is nonsecret by ``records.py``'s
        contract; ``worker_key`` never appears.

        A supplied ``clerk_id`` is validated against the registry and raises
        :class:`ClerkNotFound` when unknown (#2133 P2-c): an unscoped-looking
        empty result for a mistyped or stale id is indistinguishable from a
        real clerk with no routing history. Omitting ``clerk_id`` entirely
        keeps the unscoped, every-lane read working exactly as before.
        """
        if clerk_id is not None:
            self._require_clerk(clerk_id)
        now = self._clock()
        fetched = self._store.list_routing_receipts(
            clerk_id=clerk_id,
            since_ms=since_ms,
            before_ms=before_ms,
            before_correlation_id=before_correlation_id,
            limit=limit + 1,
        )
        has_more = len(fetched) > limit
        page = fetched[:limit]
        next_before_ms = page[-1].created_at_ms if has_more else None
        next_before_correlation_id = page[-1].correlation_id if has_more else None
        unresolved_capability_warned: set[tuple[str, str]] = set()
        return {
            "observed_at_ms": now,
            "receipts": [
                {
                    "correlation_id": receipt.correlation_id,
                    "clerk_id": receipt.clerk_id,
                    "broker": receipt.broker,
                    "operation_kind": receipt.operation_kind,
                    "capability": self._capability_for_operation_kind(
                        broker=receipt.broker,
                        operation_kind=receipt.operation_kind,
                        warned=unresolved_capability_warned,
                    ),
                    "routing_state": receipt.state.value,
                    "nonsecret_target_ref": receipt.nonsecret_target_ref,
                    "idempotency_key": receipt.idempotency_key,
                    "upstream_receipt_ref": receipt.upstream_receipt_ref,
                    "pinned_routing_epoch": receipt.pinned_routing_epoch,
                    "pinned_binding_generation": receipt.pinned_binding_generation,
                    "pinned_agent_instance_id": receipt.pinned_agent_instance_id,
                    "created_at_ms": receipt.created_at_ms,
                    "dispatched_at_ms": receipt.dispatched_at_ms,
                    "updated_at_ms": receipt.updated_at_ms,
                }
                for receipt in page
            ],
            "has_more": has_more,
            "next_before_ms": next_before_ms,
            "next_before_correlation_id": next_before_correlation_id,
        }

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
            effective = self._store.list_effective_assignments_for_clerk(clerk.clerk_id)
            entries.append(
                self._descriptor(clerk, session, effective, now).public_fields()
            )
        return {"observed_at_ms": now, "clerks": entries}

    def describe_clerk(self, clerk_id: str) -> ClerkDescriptor:
        """The directory projection for one clerk (operator ceremony reads)."""
        clerk = self._require_clerk(clerk_id)
        session = self._store.read_session(clerk_id)
        effective = self._store.list_effective_assignments_for_clerk(clerk_id)
        return self._descriptor(clerk, session, effective, self._clock())

    def _descriptor(
        self,
        clerk: ClerkRecord,
        session: ClerkSessionRecord | None,
        effective: Sequence[AccountAssignmentRecord],
        now: int,
    ) -> ClerkDescriptor:
        """Project one clerk, session and confirmed assignment into its entry."""
        adapter = self._provider_adapters.get(clerk.broker)
        capabilities: tuple[str, ...] = ()
        provider_summary: dict[str, object] | None = None
        confirmed_generation: int | None = None
        confirmed_account: str | None = None
        # A registry that says one clerk holds several effective assignments
        # is a corrupted state: it is surfaced (degraded lifecycle, flagged
        # observation), never averaged or silently first-row-projected.
        multiple_effective = len(effective) > 1
        confirmed_by_current_session = bool(
            effective
            and session is not None
            and effective[0].confirmed_agent_instance_id == session.agent_instance_id
            and effective[0].confirmed_routing_epoch == session.routing_epoch
        )
        if effective:
            confirmed_generation = effective[0].confirmed_binding_generation
            confirmed_account = effective[0].canonical_external_account_id
        # A confirmed observation that belongs to a superseded session does
        # not make the replacement session ready: the replacement must
        # re-confirm, and until then the lane projects starting.
        projected_generation = (
            confirmed_generation
            if (confirmed_by_current_session and not multiple_effective)
            else None
        )
        effective_state = _project_lifecycle(
            clerk,
            session,
            projected_generation,
            now,
            self._session_stale_after_ms,
            multiple_effective_assignments=multiple_effective,
        )
        if adapter is not None:
            capabilities = tuple(sorted(cap.value for cap in adapter.capabilities))
            if session is not None:
                observation: dict[str, object] = {
                    "reported_state": session.reported_state,
                    "reported_account_id": session.reported_account_id,
                    "reported_binding_generation": session.reported_binding_generation,
                    "confirmed_binding_generation": confirmed_generation,
                    "confirmed_by_current_session": confirmed_by_current_session,
                    "confirmed_account_id": confirmed_account,
                    "lifecycle_state": str(effective_state),
                    "multiple_effective_assignments": multiple_effective,
                }
                reported_summary = ProviderSummaryObservation.parse(
                    session.reported_summary_json
                )
                if reported_summary is not None:
                    observation["reported_summary"] = {
                        "endpoint_mode": str(reported_summary.endpoint_mode),
                        "authority_state": reported_summary.authority_state,
                        "detail": reported_summary.detail,
                        "account_nickname": reported_summary.account_nickname,
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
            effective_binding_generation=projected_generation,
            capabilities=capabilities,
            provider_summary=provider_summary,
            observed_at_ms=now,
            draining_since_ms=clerk.draining_since_ms,
            drain_deadline_at_ms=clerk.drain_deadline_at_ms,
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

    async def aggregate_lane_reads_async(
        self,
        lane_reads: Sequence[
            tuple[str, str, Callable[[], Awaitable[Mapping[str, object]]]]
        ],
        *,
        lane_timeout_s: float = 5.0,
    ) -> dict[str, object]:
        """Provenance-preserving partial aggregation over async lane readers.

        The async twin of :meth:`aggregate_lane_reads`, for reads that leave
        the process — a lane-scoped read through the lane router, never a
        local descriptor projection. The per-lane contract is unchanged
        (PRD FR-083/084): one lane's exception is that lane's explicit
        ``ok: False`` entry, never an omission, a substitution, or a failure
        for every other lane. A lane that exceeds ``lane_timeout_s`` is the
        same explicit failure — surfaced as ``LaneReadTimeout`` so a slow
        lane is never mistaken for a quiet one — and lanes are awaited
        concurrently, so one slow lane cannot delay the others' answers
        (FR-093's transport half). The coordinator still combines no values:
        each lane's mapping returns untouched.
        """
        now = self._clock()

        async def run_one(
            broker: str, clerk_id: str, reader: Callable[[], Awaitable[Mapping[str, object]]]
        ) -> dict[str, object]:
            try:
                payload = dict(await asyncio.wait_for(reader(), timeout=lane_timeout_s))
            except TimeoutError:
                return {
                    "broker": broker,
                    "clerk_id": clerk_id,
                    "ok": False,
                    "error_reason": "LaneReadTimeout",
                    "error_message": (
                        f"The lane read exceeded {lane_timeout_s:g}s and was abandoned."
                    ),
                }
            except Exception as exc:
                return {
                    "broker": broker,
                    "clerk_id": clerk_id,
                    "ok": False,
                    "error_reason": getattr(exc, "reason", type(exc).__name__),
                    "error_message": str(exc),
                }
            return {"broker": broker, "clerk_id": clerk_id, "ok": True, "value": payload}

        results = await asyncio.gather(
            *(run_one(broker, clerk_id, reader) for broker, clerk_id, reader in lane_reads)
        )
        return {"observed_at_ms": now, "lanes": list(results)}

    def aggregate_directory_reads(
        self, *, include_retired: bool = False
    ) -> dict[str, object]:
        """The resilient twin of :meth:`directory` (PRD FR-083/084).

        ``directory()`` projects every registered clerk in a plain ``for``
        loop with no per-lane exception isolation -- one clerk's descriptor
        projection throwing fails the whole roster. This reads the same
        per-lane data (``describe_clerk(...).public_fields()``) through
        :meth:`aggregate_lane_reads`'s partial aggregation instead, so one
        lane's failure surfaces as that lane's own ``ok: False`` entry and
        every other lane still reports.
        """
        clerks = self._store.list_clerks(include_retired=include_retired)
        lane_reads = [
            (
                clerk.broker,
                clerk.clerk_id,
                lambda clerk_id=clerk.clerk_id: self.describe_clerk(
                    clerk_id
                ).public_fields(),
            )
            for clerk in clerks
        ]
        return self.aggregate_lane_reads(lane_reads)

    # ---- internal ------------------------------------------------------------

    def _require_clerk(self, clerk_id: str) -> ClerkRecord:
        # A malformed identity cannot exist, so it is reported as not found —
        # callers learn nothing about any real clerk from the distinction.
        """Resolve one clerk or refuse; malformed ids are simply absent."""
        if not is_clerk_id(clerk_id):
            raise ClerkNotFound(
                f"No clerk carries identity {clerk_id!r}.",
                next_step=_UNKNOWN_CLERK_NEXT_STEP,
            )
        clerk = self._store.read_clerk(clerk_id)
        if clerk is None:
            raise ClerkNotFound(
                f"No clerk carries identity {clerk_id!r}.",
                next_step=_UNKNOWN_CLERK_NEXT_STEP,
            )
        return clerk

    def _refuse_registration_for_closed_lane(self, clerk: ClerkRecord) -> None:
        """The registration lifecycle gate (#2155), shared by its two checks.

        Runs once on the lock-free pre-read for a cheap refusal and again on
        the in-transaction re-read that serializes with a concurrently
        committing drain or retirement — one gate, so the two checks cannot
        drift apart.
        """
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkLaneRetired(
                f"Clerk {clerk.clerk_id} is retired; a retired lane never "
                "returns to service.",
                next_step="Provision a new clerk; a retired lane's identity is "
                "never reinstated.",
            )
        if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
            # The typed refusal is the lesson (#2155): the lane reads this
            # exact code, durably marks its confirmation evidence as drained,
            # and never boots that binding offline again. A generic refusal
            # here would be indistinguishable from unreachability and send
            # the lane down the FR-066 offline path instead.
            raise ClerkLaneDraining(
                f"Clerk {clerk.clerk_id} is draining; a drained lane never "
                "returns to service.",
                next_step="Finish the drain ceremony on the coordinator; this "
                "lane marks its own evidence drained and stays down.",
            )

    def _refuse_confirmation_for_closed_lane(self, clerk: ClerkRecord) -> None:
        """The confirmation lifecycle gate (#2155), shared by its two checks.

        The same double-check shape as registration's gate, for the same
        reason: the pre-read refuses cheaply, the in-transaction re-read is
        the one that cannot be raced by a mid-flight drain.
        """
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkLaneRetired(
                f"Clerk {clerk.clerk_id} is retired; a retired lane confirms "
                "nothing.",
                next_step="Provision a new clerk; a retired lane's identity is "
                "never reinstated.",
            )
        if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
            # A confirmation while draining is the resurrection this gate
            # exists to prevent (#2155): the drain closed the door, and no
            # later heartbeat or repair path may reopen it from the lane side.
            raise ClerkLaneDraining(
                f"Clerk {clerk.clerk_id} is draining; a drained lane confirms "
                "no binding.",
                next_step="Finish the drain ceremony on the coordinator; this "
                "grant is not re-presentable.",
            )

    def _clerk_on_or_unknown(
        self, conn: sqlite3.Connection, clerk_id: str
    ) -> ClerkRecord:
        """The clerk as the caller's write transaction sees it.

        The lock-free ``_require_clerk`` read races a concurrent drain or
        retirement committing between that read and the caller's
        ``BEGIN IMMEDIATE``; this re-read inside the transaction is the one
        that excludes the rival transition — the same fence ``drain_clerk``
        and ``reserve_assignment`` already use.
        """
        live = self._store.read_clerk_on(conn, clerk_id)
        if live is None:
            raise ClerkNotFound(
                f"No clerk carries identity {clerk_id!r}.",
                next_step=_UNKNOWN_CLERK_NEXT_STEP,
            )
        return live


def _bounded_attestation(operator: str, change_ref: str) -> tuple[str, str]:
    """Validate and normalize one ceremony's operator attribution (ADR 0063 Decision 4).

    The shape ``closeout_empty_registry_recovery`` established: a bounded,
    non-empty ``operator`` (≤128) and ``change_ref`` (≤512), stripped. This
    buys attribution, not proof — a change reference is unique per ceremony
    and names who acted and why; a phrase published in the repository that
    checks it is replayable and names nobody, which is exactly the failure
    of the token this replaces. A malformed attribution is a malformed
    invocation (``ValueError``, CLI exit 1), not a ceremony state refusal.
    """
    bounded_operator = operator.strip()
    bounded_change_ref = change_ref.strip()
    if (
        not bounded_operator
        or len(bounded_operator) > _OPERATOR_MAX_CHARS
        or not bounded_change_ref
        or len(bounded_change_ref) > _CHANGE_REF_MAX_CHARS
    ):
        raise ValueError(
            "A release, reassignment or force-retirement requires a non-empty "
            f"operator (≤{_OPERATOR_MAX_CHARS} chars) and change reference "
            f"(≤{_CHANGE_REF_MAX_CHARS} chars), attributing who acted and why."
        )
    return bounded_operator, bounded_change_ref


def _validate_internal_base_url(base_url: str) -> str:
    """Normalize and bound one internal agent destination.

    Internal destinations are plain ``http(s)://host[:port][/prefix]`` values:
    no user info, query, fragment or parameters — a credential or injection
    channel does not travel in a placement field.
    """
    if not base_url:
        raise ValueError("an internal base URL must not be empty")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(
            f"internal base URL {base_url!r} must be an absolute http(s) URL with a host"
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            f"internal base URL {base_url!r} carries user info, query or fragment"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"internal base URL {base_url!r} carries an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"internal base URL {base_url!r} carries an out-of-range port")
    if parts.scheme == "http":
        # Host names are boundary-checked again where they are dialed (the
        # transport seam resolves them); an explicit public IP literal is
        # refused at approval time, when the deployment owns the row.
        try:
            literal = ipaddress.ip_address(parts.hostname)
        except ValueError:
            literal = None
        if literal is not None and not address_is_private(literal):
            raise ValueError(
                f"internal base URL {base_url!r} names a public address; "
                "cleartext fleet traffic never leaves the private network"
            )
    normalized = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    if len(normalized) > 200:
        raise ValueError("an internal base URL is a short placement value")
    return normalized


def _project_lifecycle(
    clerk: ClerkRecord,
    session: ClerkSessionRecord | None,
    confirmed_binding_generation: int | None,
    now: int,
    stale_after_ms: int,
    *,
    multiple_effective_assignments: bool = False,
) -> ClerkLifecycleState:
    """Durable states pass through; live states project from observations.

    A stored flag would let a historical acknowledgement present itself as
    current liveness (PRD FR-081), so readiness is recomputed from the
    session's freshness and the *confirmed* assignment every time. Readiness
    requires a confirmed binding observation — a heartbeat alone projects at
    most ``starting`` (audit 2026-09-13, finding 1). A clerk the registry
    says holds several effective assignments is corrupted and projects
    ``degraded`` rather than presenting an arbitrary one as healthy.
    """
    if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
        return ClerkLifecycleState.RETIRED
    if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
        return ClerkLifecycleState.DRAINING
    if session is None:
        return ClerkLifecycleState.PROVISIONED
    if now - session.last_seen_at_ms > stale_after_ms:
        return ClerkLifecycleState.UNREACHABLE
    if multiple_effective_assignments:
        return ClerkLifecycleState.DEGRADED
    if session.reported_state == "degraded":
        return ClerkLifecycleState.DEGRADED
    if confirmed_binding_generation is None:
        return ClerkLifecycleState.STARTING
    return ClerkLifecycleState.READY


__all__ = [
    "ATTESTATION_KINDS",
    "DEFAULT_DEPLOYMENT_NAMESPACE",
    "DEFAULT_DRAIN_DEADLINE_MS",
    "DEFAULT_SESSION_STALE_AFTER_MS",
    "FLEET_PROTOCOL_VERSION",
    "FleetControlService",
    "ProvisionedClerk",
]

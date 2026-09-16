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
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from app.broker.fleet import volume as volume_module
from app.broker.fleet.errors import (
    BrokerAndClerkRequired,
    BrokerClerkCapabilityUnavailable,
    ClerkAccountMismatch,
    ClerkAssignmentConflict,
    ClerkBindingGenerationConflict,
    ClerkBrokerMismatch,
    ClerkEndpointNotApproved,
    ClerkIdentityMismatch,
    ClerkNotFound,
    ClerkRoutingAttemptConflict,
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
    ProviderSummaryObservation,
    RoutingReceiptRecord,
    RoutingReceiptState,
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

#: The release ceremony's explicit attestation token (PRD FR-055). The host
#: operator types exactly this to release an assignment, so a release can
#: never happen by accident or by an unattributed caller. The *proof chain*
#: behind the attestation — the old agent and volume verifiably offline,
#: credentials isolated, provider obligations clear — is enforced by the
#: host ceremony checklist and gains machine-checked proof with the Phase 2
#: agent liveness surface; the spine refuses everything weaker than the token.
RELEASE_PROOF_TOKEN = "old-clerk-offline-and-obligations-clear"

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
            # re-provisioned cleanly instead of wedging on a ghost.
            with self._store.transaction() as conn:
                self._store.update_clerk_lifecycle(
                    conn,
                    clerk_id=clerk_id,
                    lifecycle_state=StoredLifecycleState.RETIRED,
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

    def retire_clerk(self, *, clerk_id: str) -> ClerkRecord:
        """Retirement is terminal; IDs are never recycled (PRD FR-013).

        Refuses while the clerk still holds an effective assignment — the
        obligations the assignment represents must be resolved by the release
        ceremony first, not silently orphaned. The assignment check and the
        lifecycle transition share one write transaction, so a concurrent
        reservation cannot observe "provisioned" while retirement observes
        "no assignments" and leave a retired clerk owning an active
        assignment.
        """
        self._require_recovery_hold_clear()
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            return clerk
        now = self._clock()
        updated = False
        with self._store.transaction() as conn:
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
            updated = self._store.update_clerk_lifecycle(
                conn,
                clerk_id=clerk_id,
                lifecycle_state=StoredLifecycleState.RETIRED,
                retired_at_ms=now,
            )
        if not updated:
            raise ClerkNotFound(
                f"No clerk carries identity {clerk_id!r}.",
                next_step="Confirm the clerk id; a clerk that never existed "
                "cannot be retired.",
            )
        logger.info("fleet clerk retired", extra={"broker": clerk.broker, "clerk_id": clerk_id})
        retired = self._store.read_clerk(clerk_id)
        assert retired is not None
        return retired

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
        for good.

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
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired; a retired lane never returns to service.",
                next_step="Provision a new clerk; a retired lane's identity is "
                "never reinstated.",
            )
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
    ) -> bool:
        """Record a heartbeat and the worker's *observed* binding facts.

        An observation updates liveness projections only; it can never move
        an assignment, a lifecycle state, a confirmed binding observation or
        ownership of anything (audit 2026-09-13, finding 1). The optional
        summary must be the bounded typed observation; agent-authored
        free-form JSON refuses.
        """
        clerk = self._require_clerk(clerk_id)
        if clerk.lifecycle_state == StoredLifecycleState.RETIRED:
            raise ClerkNotFound(
                f"Clerk {clerk_id} is retired.",
                next_step="Stop sending heartbeats for a retired clerk; its "
                "identity is never reinstated.",
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
                    "authority_state and a short detail line.",
                ) from exc
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
                reported_summary_json=summary_json,
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
        ownership (FR-054). Re-reserving is defined for the same owner in
        both live states: a reserved row returns as-is, and an effective row
        is the same-owner resume of a restarted clerk, returning the
        confirmed facts untouched (audit 2026-09-13, finding 1).

        ``volume_root`` is the co-located caller's re-proof of the mounted
        root. It is optional because the coordinator process may not have
        the volume mounted at all; the agent's own gate
        (``fleet_boot.open_fleet_lane``) is the authority, and
        ``LocalPresence`` — the one transport that does share the filesystem
        — always supplies it.
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
                    # Reuse after the release ceremony: a fresh reservation at
                    # a higher generation, compare-and-swapped on the prior
                    # generation *and* state so a racing third writer cannot
                    # also land.
                    outcome = AccountAssignmentRecord(
                        broker=broker,
                        canonical_external_account_id=canonical,
                        clerk_id=clerk_id,
                        assignment_generation=existing.assignment_generation + 1,
                        state=AssignmentState.RESERVED,
                        recorded_at_ms=now,
                        updated_at_ms=now,
                    )
                    accepted = self._store.cas_update_assignment(
                        conn,
                        outcome,
                        previous_generation=existing.assignment_generation,
                        previous_state=AssignmentState.RELEASED,
                    )
                    if not accepted:
                        raise ClerkAssignmentConflict(
                            f"Account {canonical} under broker {broker!r} changed "
                            "while re-reserving; re-read and retry.",
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
            # read and the commit.
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
        proof: str,
    ) -> AccountAssignmentRecord:
        """The host-only release ceremony (PRD FR-055).

        Terminal for the generation: the row records ``released`` and stays
        in the append-only history, which is what makes "never expire into
        takeover" auditable. Reuse of the account starts a fresh reservation
        with a higher generation. The caller pins the assignment generation
        its evidence was prepared against, so release evidence prepared for
        generation N cannot silently release generation N+1's new owner.
        """
        self._require_recovery_hold_clear()
        if not proof or proof.strip() != RELEASE_PROOF_TOKEN:
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
        if (
            existing.state == AssignmentState.RELEASED
            and existing.assignment_generation == expected_assignment_generation
        ):
            return existing
        if existing.assignment_generation != expected_assignment_generation:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} is at assignment "
                f"generation {existing.assignment_generation}, not the pinned "
                f"{expected_assignment_generation}; release evidence cannot cross "
                "a reassignment.",
                next_step="Re-read the assignment and re-prepare the release evidence.",
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
            confirmed_binding_generation=existing.confirmed_binding_generation,
            confirmed_profile_id=existing.confirmed_profile_id,
            confirmed_revision=existing.confirmed_revision,
            confirmed_at_ms=existing.confirmed_at_ms,
            confirmed_agent_instance_id=existing.confirmed_agent_instance_id,
            confirmed_routing_epoch=existing.confirmed_routing_epoch,
            recorded_at_ms=existing.recorded_at_ms,
            updated_at_ms=now,
        )
        accepted = False
        with self._store.transaction() as conn:
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
            extra={"broker": broker},
        )
        return released

    def reassign_assignment(
        self,
        *,
        broker: str,
        external_account_id: str,
        expected_assignment_generation: int,
        proof: str,
        successor_clerk_id: str,
        successor_volume_root: Path,
    ) -> AccountAssignmentRecord:
        """Transfer an account only through the proof-driven host ceremony.

        The successor's marked volume is verified before the old assignment is
        released. The successor is reserved, not confirmed or routed: its
        agent must still pass its provider-owned binding and arming gates.
        """
        self._require_recovery_hold_clear()
        successor = self._require_clerk(successor_clerk_id)
        if successor.broker != broker:
            raise ClerkBrokerMismatch(
                f"Successor clerk {successor_clerk_id} belongs to broker "
                f"{successor.broker!r}, not {broker!r}.",
                next_step=_BROKER_IMMUTABLE_NEXT_STEP,
            )
        if not proof or proof.strip() != RELEASE_PROOF_TOKEN:
            raise ClerkAssignmentConflict(
                "The reassignment ceremony requires the offline-and-obligations-clear proof.",
                next_step="Prove the old agent and volume are offline and the provider's "
                "obligations are clear, then re-run with the proof token.",
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
                active_successor_assignments = [
                    assignment
                    for assignment in self._store.list_assignments_for_clerk_on(
                        conn, successor_clerk_id
                    )
                    if assignment.state != AssignmentState.RELEASED
                ]
                if active_successor_assignments:
                    raise ClerkAssignmentConflict(
                        f"Successor clerk {successor_clerk_id} already holds an active "
                        "assignment; one lane cannot acquire a second account during "
                        "reassignment.",
                    )
                if existing.assignment_generation != expected_assignment_generation:
                    raise ClerkAssignmentConflict(
                        f"Account {canonical} under broker {broker!r} is at assignment "
                        f"generation {existing.assignment_generation}, not the pinned "
                        f"{expected_assignment_generation}; reassignment evidence cannot cross "
                        "an ownership change.",
                    )
                if existing.state == AssignmentState.RELEASED:
                    raise ClerkAssignmentConflict(
                        f"Account {canonical} under broker {broker!r} is already released; "
                        "a completed ceremony is never silently continued.",
                    )
                released = replace(existing, state=AssignmentState.RELEASED, updated_at_ms=now)
                reserved = AccountAssignmentRecord(
                    broker=broker,
                    canonical_external_account_id=canonical,
                    clerk_id=successor_clerk_id,
                    assignment_generation=existing.assignment_generation + 1,
                    state=AssignmentState.RESERVED,
                    recorded_at_ms=now,
                    updated_at_ms=now,
                )
                self._store.reassign_assignment(
                    conn,
                    released=released,
                    reserved_successor=reserved,
                    previous_state=existing.state,
                )
        except sqlite3.IntegrityError as exc:
            raise ClerkAssignmentConflict(
                f"Account {canonical} under broker {broker!r} changed while reassignment "
                "was preparing the successor; the original ownership remains intact.",
            ) from exc
        logger.info(
            "fleet account assignment reassigned",
            extra={"broker": broker, "clerk_id": successor_clerk_id},
        )
        return reserved

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
    ) -> tuple[ClerkRecord, ClerkSessionRecord, AccountAssignmentRecord | None]:
        """Resolve a routed operation's clerk, verifying path identities.

        ``clerk_id`` is resolved first, then the path broker must equal the
        clerk's immutable broker (PRD FR-071). A retired or draining clerk and
        a clerk with no live session are not routable. When the caller pins an
        epoch or a binding generation, a mismatch refuses rather than silently
        retargeting (FR-073/078).

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
        if clerk.lifecycle_state == StoredLifecycleState.DRAINING:
            raise ClerkUnreachable(
                f"Clerk {clerk_id} is draining and accepts no routed operations.",
                next_step="Wait for the clerk to finish draining, or route to "
                "its successor if one is assigned.",
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
        """Record that the attempt was handed to the provider clerk.

        One-way: once dispatched, an attempt can never present as
        definitively un-sent. Idempotent for a repeated marking.
        """
        now = self._clock()
        with self._store.transaction() as conn:
            updated = self._store.mark_routing_receipt_dispatched(
                conn, correlation_id=correlation_id, dispatched_at_ms=now
            )
        receipt = self._store.read_routing_receipt(correlation_id)
        if not updated or receipt is None:
            raise ClerkRoutingAttemptConflict(
                f"No routing attempt carries correlation {correlation_id!r}.",
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
    "DEFAULT_SESSION_STALE_AFTER_MS",
    "FLEET_PROTOCOL_VERSION",
    "RELEASE_PROOF_TOKEN",
    "FleetControlService",
    "ProvisionedClerk",
]

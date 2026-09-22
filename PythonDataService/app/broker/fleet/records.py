"""Fleet record shapes — storage rows and public projections.

Frozen dataclasses only. Every timestamp is ``int64 ms UTC`` (ADR 0022); no
record carries a credential value, fragment, length, environment name or
secret-derived hash. ``worker_key`` is durable registry identity and never
appears in a public projection (PRD FR-012), and — since the 2026-09-13
audit — it is never a transport credential either.

Schema v2 separates *observed* facts (heartbeats, session rows) from the
*confirmed* binding observation the coordinator keeps on the assignment row:
the confirmed columns are the routing fence, and only a confirming worker
fenced by its instance and epoch can move them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class StoredLifecycleState(StrEnum):
    """The durable states the registry persists for a clerk.

    ``starting`` / ``ready`` / ``degraded`` / ``unreachable`` are *projections*
    over the current session and assignment (PRD FR-080/081): a stored flag
    would let a historical acknowledgement present itself as current liveness.
    """

    PROVISIONED = "provisioned"
    DRAINING = "draining"
    RETIRED = "retired"


class ClerkLifecycleState(StrEnum):
    """The full public lifecycle vocabulary (PRD FR-080)."""

    PROVISIONED = "provisioned"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    DRAINING = "draining"
    RETIRED = "retired"


class AssignmentState(StrEnum):
    """An assignment is reserved, effective, or terminally released.

    There is no expiry transition: ``released`` is reached only through the
    host ceremony, never through heartbeat loss or coordinator restart
    (PRD FR-054/056).
    """

    RESERVED = "reserved"
    EFFECTIVE = "effective"
    RELEASED = "released"


class LaneConfirmationState(StrEnum):
    """Whether a lane-quiet confirmation covered a lifecycle exit (ADR 0063).

    ``absent`` records that the exit proceeded without one — every release,
    reassignment and force-retirement today, which is the countable fact an
    auditor reads. ``present`` is the value the normal retirement path writes
    once a provider answers lane quiet (#2154). NULL, not a member, means the
    row predates the ceremony or its transition carried no confirmation.
    """

    ABSENT = "absent"
    PRESENT = "present"


class RoutingReceiptState(StrEnum):
    """The four-way outcome vocabulary of one routing attempt.

    ``not_dispatched`` is provably un-sent; ``provider_refused`` is a
    definitive provider refusal; ``delivered`` carries the provider's durable
    receipt reference and is terminal; ``outcome_unknown`` means the attempt
    may have executed and must be reconciled by identity, never resubmitted
    blindly (audit 2026-09-13, finding 7).
    """

    NOT_DISPATCHED = "not_dispatched"
    PROVIDER_REFUSED = "provider_refused"
    DELIVERED = "delivered"
    OUTCOME_UNKNOWN = "outcome_unknown"


class SummaryEndpointMode(StrEnum):
    """The closed endpoint-mode vocabulary of a lane summary observation."""

    PAPER = "paper"
    LIVE = "live"
    UNIDENTIFIED = "unidentified"


_AUTHORITY_STATE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DETAIL_MAX_CHARS = 200
#: PRD #2182: the account nickname an agent may report alongside its lane
#: summary, bounded the same way ``detail`` is — a short string, refused when
#: oversized or not a string at all. Must equal ``NicknamePutRequest``'s
#: ``max_length`` (``app/schemas/broker_configuration.py``) — the writer's own
#: bound on what a nickname can be set to. Not derived from one shared
#: constant (the two modules sit on either side of a boundary this package
#: does not import across); ``test_account_nickname_bound_matches_the_writers_own_bound``
#: (test_admission_probes_2026_09_13.py) pins the two together instead. Raise
#: both together, never just one.
_ACCOUNT_NICKNAME_MAX_CHARS = 120


@dataclass(frozen=True, slots=True)
class ProviderSummaryObservation:
    """The bounded, typed summary an agent may report about its lane.

    Provider-authored, but never arbitrary agent JSON (audit 2026-09-13,
    finding 6): the endpoint mode comes from a closed vocabulary, the
    authority state matches a bounded snake-case pattern, and the detail line
    and account nickname are length-capped prose. The provider adapter
    authors the public ``provider_summary`` projection from this observation
    plus registry facts.
    """

    endpoint_mode: SummaryEndpointMode
    authority_state: str
    detail: str | None = None
    #: The confirmed account's nickname, when the agent's lane has one set
    #: (ADR 0064 Decision 5). Optional and rollout-safe: the coordinator
    #: accepts this key before any agent sends it, and an absent key parses
    #: exactly as it did before this field existed.
    account_nickname: str | None = None

    def to_json(self) -> str:
        """The strict storage encoding for the session row."""
        payload: dict[str, str] = {
            "endpoint_mode": str(self.endpoint_mode),
            "authority_state": self.authority_state,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.account_nickname is not None:
            payload["account_nickname"] = self.account_nickname
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def parse(cls, raw: object) -> ProviderSummaryObservation | None:
        """Parse stored or reported JSON into the typed observation.

        Returns ``None`` for ``None``; raises ``ValueError`` for anything that
        is present but not a valid bounded observation, so ingestion can
        refuse agent-authored free-form JSON instead of projecting it.
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"a lane summary must be valid JSON: {exc}") from exc
        elif isinstance(raw, Mapping):
            payload = dict(raw)
        else:
            raise ValueError("a lane summary must be a JSON object")
        if set(payload) - {"endpoint_mode", "authority_state", "detail", "account_nickname"}:
            raise ValueError(
                "a lane summary carries only endpoint_mode, authority_state, "
                "detail and account_nickname"
            )
        mode = payload.get("endpoint_mode")
        if mode not in tuple(item.value for item in SummaryEndpointMode):
            raise ValueError(f"unknown endpoint_mode {mode!r}")
        authority = payload.get("authority_state")
        if not isinstance(authority, str) or _AUTHORITY_STATE_PATTERN.fullmatch(authority) is None:
            raise ValueError(f"authority_state {authority!r} is not a bounded snake-case token")
        detail = payload.get("detail")
        if detail is not None and (
            not isinstance(detail, str) or len(detail) > _DETAIL_MAX_CHARS
        ):
            raise ValueError("summary detail must be a short string")
        account_nickname = payload.get("account_nickname")
        if account_nickname is not None and (
            not isinstance(account_nickname, str)
            or len(account_nickname) > _ACCOUNT_NICKNAME_MAX_CHARS
        ):
            raise ValueError("summary account_nickname must be a short string")
        return cls(
            endpoint_mode=SummaryEndpointMode(mode),
            authority_state=authority,
            detail=detail,
            account_nickname=account_nickname,
        )


@dataclass(frozen=True, slots=True)
class ClerkRecord:
    clerk_id: str
    broker: str
    worker_key: str
    display_label: str
    volume_id: str
    volume_root: str
    deployment_namespace: str
    volume_attestation_kind: str
    volume_attestation_id: str
    lifecycle_state: StoredLifecycleState
    created_at_ms: int
    retired_at_ms: int | None = None
    #: ADR 0063 Decision 1/5 — the drain start instant and the absolute
    #: deadline computed at drain time; both persist into retirement.
    draining_since_ms: int | None = None
    drain_deadline_at_ms: int | None = None
    #: ADR 0063 Decision 5 — ``absent`` when force-retired past an
    #: unanswered lane-quiet gate, ``present`` when the normal path retires
    #: on a lane confirmation, ``None`` until retired (and for a never-served
    #: clerk's direct retirement).
    lane_confirmation: LaneConfirmationState | None = None
    retire_operator: str | None = None
    retire_change_ref: str | None = None


#: ADR 0063 Decision 2, as amended 2026-09-19 (#2154). Condition 1 — the
#: lane is draining — is the registry's own fact and is never taken from the
#: lane; these are the four the lane answers about itself. The mapping is a
#: closed vocabulary: the refusal names which condition is outstanding, never
#: its contents, so no quantity, symbol or identifier crosses the seam.
LANE_QUIET_CONDITIONS: tuple[tuple[str, str], ...] = (
    ("runner_idle", "a bot is still running"),
    ("broker_work_ended", "a working order on the account has not ended"),
    ("account_flat", "the account is not flat"),
    ("intents_resolved", "an order intent is unresolved"),
)


@dataclass(frozen=True, slots=True)
class LaneQuietConfirmationRecord:
    """One lane's answer about its own quiescence, at one observed instant.

    A *confirmation*, not an observation: it carries the session instance and
    epoch it was prepared under, and the gate reads it only while that session
    is still the current one. An answer that leaves a condition outstanding is
    recorded exactly as a quiet one is — a lane saying "not yet" is evidence,
    and is what lets the retirement gate name what is outstanding instead of
    reporting the silence of a lane that never answered at all.
    """

    clerk_id: str
    agent_instance_id: str
    routing_epoch: int
    observed_at_ms: int
    recorded_at_ms: int
    runner_idle: bool
    broker_work_ended: bool
    account_flat: bool
    intents_resolved: bool

    @property
    def outstanding(self) -> tuple[str, ...]:
        """The conditions this answer leaves unsatisfied, in declared order.

        Every one, not the first: an operator clearing them one at a time
        would otherwise walk the ceremony once per item.
        """
        return tuple(
            phrase
            for field_name, phrase in LANE_QUIET_CONDITIONS
            if not getattr(self, field_name)
        )

    @property
    def is_quiet(self) -> bool:
        """True only when every condition the lane answers holds.

        Derived, never stored and never sent: a lane cannot assert quiet
        while reporting a condition it failed.
        """
        return not self.outstanding


@dataclass(frozen=True, slots=True)
class ClerkSessionRecord:
    broker: str
    clerk_id: str
    agent_instance_id: str
    routing_epoch: int
    started_at_ms: int
    last_seen_at_ms: int
    reported_binding_generation: int | None = None
    reported_account_id: str | None = None
    reported_state: str | None = None
    endpoint_ref: str | None = None
    adapter_version: str | None = None
    reported_summary_json: str | None = None


@dataclass(frozen=True, slots=True)
class SessionObservation:
    """One heartbeat's answer: what landed, and what the lane must learn.

    ``touched`` is the pre-#2155 fact (the beat reached the current session).
    ``lifecycle_state`` is the lane's own durable state as the coordinator
    holds it — the channel by which a live lane learns it was drained and
    marks its evidence before any offline boot can present that binding as
    effective again (#2155). A lane that never observes (or never hears) its
    drain is the residual window the ADR names, not a state this record can
    repair after the fact.
    """

    touched: bool
    lifecycle_state: StoredLifecycleState


@dataclass(frozen=True, slots=True)
class AccountAssignmentRecord:
    broker: str
    canonical_external_account_id: str
    clerk_id: str
    assignment_generation: int
    state: AssignmentState
    effective_profile_id: str | None = None
    effective_revision: int | None = None
    confirmed_binding_generation: int | None = None
    confirmed_profile_id: str | None = None
    confirmed_revision: int | None = None
    confirmed_at_ms: int | None = None
    confirmed_agent_instance_id: str | None = None
    confirmed_routing_epoch: int | None = None
    recorded_at_ms: int = 0
    updated_at_ms: int = 0
    #: ADR 0063 Decision 4/4.1 — the release and reassignment attestation
    #: facts. They live only on the append-only history row the ceremony
    #: inserts; the current-row writer leaves them ``None``, exactly as the
    #: confirmed observation lives only on the current row. A ``None`` on a
    #: row recorded before schema v5 means "no such fact existed then",
    #: never "absent confirmation".
    lane_confirmation: LaneConfirmationState | None = None
    attested_operator: str | None = None
    attested_change_ref: str | None = None
    attested_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RoutingReceiptRecord:
    correlation_id: str
    broker: str
    clerk_id: str
    operation_kind: str
    nonsecret_target_ref: str
    idempotency_key: str
    state: RoutingReceiptState
    upstream_receipt_ref: str | None = None
    pinned_routing_epoch: int | None = None
    pinned_binding_generation: int | None = None
    pinned_agent_instance_id: str | None = None
    dispatched_at_ms: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class ApprovedEndpointRecord:
    """One deployment-owned internal destination for a clerk's agent.

    Nonsecret placement data, written only by the host ceremony; a session
    registration may cite ``endpoint_ref`` but can never change what it
    points at (audit 2026-09-13, finding 4).
    """

    endpoint_ref: str
    clerk_id: str
    base_url: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class VolumeMarker:
    """The versioned identity marker written at a clerk volume's root.

    Nonsecret by construction: it names identities and the external mount
    attestation, never credentials (PRD FR-024).
    """

    marker_version: int
    broker: str
    clerk_id: str
    volume_id: str
    attestation_kind: str
    attestation_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ClerkDescriptor:
    """One directory entry — the broker-neutral public projection of a clerk.

    Carries identity, lifecycle, generations, declared capabilities and the
    provider-authored summary (PRD FR-033). Deliberately absent: ``worker_key``,
    internal endpoints, internal credentials, and every financial quantity —
    the directory computes no balances, positions, P&L, exposure or risk
    (PRD FR-034).
    """

    broker: str
    clerk_id: str
    display_label: str
    lifecycle_state: ClerkLifecycleState
    volume_id: str
    last_seen_at_ms: int | None
    routing_epoch: int | None
    effective_binding_generation: int | None
    capabilities: tuple[str, ...]
    provider_summary: Mapping[str, object] | None
    observed_at_ms: int
    #: ADR 0063 — the drain window's durable endpoints, so an operator
    #: holding a draining lane can see when the deadline that bounds
    #: ``force-retire`` actually elapses. ``None`` outside a drain.
    draining_since_ms: int | None = None
    drain_deadline_at_ms: int | None = None

    def public_fields(self) -> dict[str, object]:
        """The wire shape; ``provider_summary`` is provider-authored and typed."""
        return {
            "broker": self.broker,
            "clerk_id": self.clerk_id,
            "display_label": self.display_label,
            "lifecycle_state": str(self.lifecycle_state),
            "volume_id": self.volume_id,
            "last_seen_at_ms": self.last_seen_at_ms,
            "routing_epoch": self.routing_epoch,
            "effective_binding_generation": self.effective_binding_generation,
            "capabilities": list(self.capabilities),
            "provider_summary": dict(self.provider_summary) if self.provider_summary else None,
            "observed_at_ms": self.observed_at_ms,
            "draining_since_ms": self.draining_since_ms,
            "drain_deadline_at_ms": self.drain_deadline_at_ms,
        }


__all__ = [
    "LANE_QUIET_CONDITIONS",
    "AccountAssignmentRecord",
    "ApprovedEndpointRecord",
    "AssignmentState",
    "ClerkDescriptor",
    "ClerkLifecycleState",
    "ClerkRecord",
    "ClerkSessionRecord",
    "LaneConfirmationState",
    "LaneQuietConfirmationRecord",
    "ProviderSummaryObservation",
    "RoutingReceiptRecord",
    "RoutingReceiptState",
    "StoredLifecycleState",
    "SummaryEndpointMode",
    "VolumeMarker",
]

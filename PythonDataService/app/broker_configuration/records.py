"""The stored records of the broker-configuration profiles database.

One frozen dataclass per record shape in
``docs/architecture/broker-configuration-profile-contract.md`` §2. These are
the domain values the store reads and writes and the service reasons over; the
Pydantic DTOs in ``app/schemas/broker_configuration.py`` are built from them
and never replace them.

Every timestamp is ``int64 ms UTC`` and every such field ends ``_at_ms``
(`.claude/rules/temporal-rigor.md`). No record carries a secret, a secret
fragment, a secret length, or an environment variable name — a profile
references an opaque credential *slot* and nothing else (contract §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker_configuration.envelope import ValidatedLiveEnvelope

EndpointMode = Literal["paper", "live"]
ApplyOutcome = Literal["applied", "refused"]
DeskActivationState = Literal[
    "no_selection",
    "staged_not_applied",
    "apply_requested_restart_required",
    "effective_selection",
]
DeskActionKind = Literal[
    "review_configuration",
    "review_staged_configuration",
    "view_restart_steps",
]
DeskLifecycleKey = Literal[
    "effective_configuration",
    "selected_configuration",
    "worker_handoff",
]
DeskLifecycleStatus = Literal["complete", "current", "pending"]

# The only broker this delivery admits. The column exists so a second broker
# does not need a migration (contract §2.2).
ALPACA_BROKER = "alpaca"

# The revision payload's own version, for forward migration of the *content*
# (contract §2.3 ``schema_version``) — distinct from the database schema
# version in ``schema.py``.
REVISION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LocalOwner:
    """The one local owner (ADR 0060 Decision 3). Never client-supplied."""

    owner_id: str
    display_label: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class BrokerProfile:
    profile_id: str
    owner_id: str
    broker: str
    display_name: str
    archived: bool
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class ProfileRevision:
    """One immutable revision.

    ``account_pin``/``account_pinned_at_ms`` are the single binding transition
    the contract's ``POST .../account-pin`` route writes: ``None`` becomes an
    observed account exactly once and is never rewritten. They are deliberately
    *outside* ``content_sha256`` — the hash covers the configured content, so
    pinning cannot change the identity a stale-edit check compares against.
    """

    profile_id: str
    revision: int
    schema_version: int
    credential_slot: str
    endpoint_mode: EndpointMode
    account_pin: str | None
    account_pinned_at_ms: int | None
    live_envelope: ValidatedLiveEnvelope | None
    content_sha256: str
    complete: bool
    author_owner_id: str
    created_at_ms: int


@dataclass(frozen=True)
class AccountNickname:
    """A label keyed to the observed broker account ID, never to a profile."""

    account_id: str
    nickname: str
    updated_at_ms: int


@dataclass(frozen=True)
class InstallationSelection:
    """The one selection row (ADR 0060 Decision 5 — no worker identity).

    ``apply_requested_generation`` records which generation the one-shot Apply
    was recorded against, so a retried Apply naming that generation is a no-op
    success rather than a second request (contract §5's idempotency rule). The
    contract's §2.6 field list does not name it; the alternative was inferring
    it from ``selection_generation - 1``, which is true only while nothing else
    can advance the generation.

    ``selection_generation`` advances on each stage and each apply request, as
    §2.6 says, **and** on each of the two transitions that consume the one-shot
    request — the worker's acknowledgement and a recorded refusal. Otherwise a
    caller still holding the pre-consumption generation could re-arm the Apply
    that was just refused, or a second worker could overwrite the effective
    binding; the generation is the only fence, since Decision 5 ships no worker
    identity.

    The worker establishes ``effective_*`` and ``last_apply_*`` only after
    construction succeeds and it owns the required execution lease. Offline
    Paper reset may clear the removed binding and advances the generation;
    it never installs another binding. No route writes them.
    """

    staged_profile_id: str | None
    staged_revision: int | None
    apply_requested: bool
    apply_requested_at_ms: int | None
    apply_requested_generation: int | None
    selection_generation: int
    effective_profile_id: str | None
    effective_revision: int | None
    effective_account_id: str | None
    effective_acknowledged_at_ms: int | None
    last_apply_outcome: ApplyOutcome | None
    last_apply_refusal_reason: str | None


@dataclass(frozen=True)
class ConfigurationEvent:
    """One append-only audit row — the only home for profile provenance.

    No field is added to any custody, activation or arming record to carry it
    (ADR 0060 Decision 2). Carries no secret value and no secret-derived hash.
    """

    event_id: str
    actor_owner_id: str
    action: str
    profile_id: str | None
    revision: int | None
    previous_ref: str | None
    next_ref: str | None
    result: str
    recorded_at_ms: int


@dataclass(frozen=True)
class ProfileWithRevision:
    """What ``GET /profiles/{profile_id}`` answers: the profile and its latest."""

    profile: BrokerProfile
    latest_revision: ProfileRevision | None


@dataclass(frozen=True)
class CredentialSlotStatus:
    """A slot label plus availability — never a value, fragment, or var name."""

    slot: str
    label: str
    available: bool
    verified_account_id: str | None = None
    verified_at_ms: int | None = None


@dataclass(frozen=True)
class ObservedAccount:
    """One account a read-only verification observed under a revision's mode."""

    account_id: str
    account_mode: EndpointMode
    account_status: str | None = None


@dataclass(frozen=True)
class DeskSelectionSummary:
    """Safe account/profile identity for the Alpaca desk read model."""

    selection_id: str
    profile_id: str
    revision: int
    profile_label: str
    account_id: str | None
    nickname: str | None
    account_label: str
    endpoint_mode: EndpointMode
    badge_label: str
    description: str


@dataclass(frozen=True)
class DeskAccountChoice(DeskSelectionSummary):
    """One explicitly pinned account the configuration page can review."""

    action_kind: DeskActionKind
    action_label: str
    is_staged: bool
    is_effective: bool


@dataclass(frozen=True)
class DeskLifecycleStep:
    key: DeskLifecycleKey
    label: str
    status: DeskLifecycleStatus
    status_label: str


@dataclass(frozen=True)
class DeskAction:
    kind: DeskActionKind
    label: str
    enabled: bool


@dataclass(frozen=True)
class AlpacaDeskState:
    """Backend-authored account-selection UX; the browser only renders it."""

    activation_state: DeskActivationState
    headline: str
    detail: str
    lifecycle: tuple[DeskLifecycleStep, ...]
    selection_label: str
    consequence: str
    action: DeskAction
    selection_generation: int
    staged_choice: DeskSelectionSummary | None
    effective_choice: DeskSelectionSummary | None
    choices: tuple[DeskAccountChoice, ...]
    empty_choices_message: str | None
    profiles_requiring_setup: int
    setup_required_message: str | None


__all__ = [
    "ALPACA_BROKER",
    "REVISION_SCHEMA_VERSION",
    "AccountNickname",
    "AlpacaDeskState",
    "ApplyOutcome",
    "BrokerProfile",
    "ConfigurationEvent",
    "CredentialSlotStatus",
    "DeskAccountChoice",
    "DeskAction",
    "DeskActionKind",
    "DeskActivationState",
    "DeskLifecycleKey",
    "DeskLifecycleStatus",
    "DeskLifecycleStep",
    "DeskSelectionSummary",
    "EndpointMode",
    "InstallationSelection",
    "LocalOwner",
    "ObservedAccount",
    "ProfileRevision",
    "ProfileWithRevision",
]

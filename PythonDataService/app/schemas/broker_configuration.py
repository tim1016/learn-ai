"""The shared DTOs for ``/api/brokers/alpaca/configuration``.

Package B owns these and the generated OpenAPI artifacts until handoff (plan
§6 dependency schedule); packages C, D and E build against them rather than
each declaring their own. The shapes come from
``docs/architecture/broker-configuration-profile-contract.md`` §2 and §4.

**This surface is provisional while ADR 0060 is Proposed** (contract §4): its
open questions 2 and 5 can still change ``/credential-slots`` and the ``PATCH``
semantics. The committed OpenAPI snapshot is regenerated with this change like
any other endpoint addition and asserts nothing about permanence.

Conventions that are not negotiable here:

* Every timestamp is ``int64 ms UTC`` and every such field ends ``_at_ms``,
  bounded by ``MAX_TIMESTAMP_MS`` — never ``2**63 - 1``
  (`.claude/rules/temporal-rigor.md`).
* Every mutating request model is closed (``extra="forbid"``). Owner and actor
  are server-resolved; a body naming them is refused with
  ``owner_field_not_accepted``, never silently dropped.
* No field carries a secret, a secret fragment, a secret length, or an
  environment-variable name. ``credential_slot`` is an opaque slot label.

Two shapes here go beyond the contract's enumerated fields, both recorded
rather than assumed:

* ``SelectionResponse.apply_requested_generation`` — which generation the
  one-shot Apply was recorded against, so contract §5's "a repeated apply
  against an already-recorded generation is a no-op success" has something to
  compare. The alternative was inferring it from ``selection_generation - 1``.
* ``ApplyRequest.expected_selection_generation`` — §5 names the fence only on
  ``PUT /selection``; an Apply that names no generation cannot be idempotent
  or fenced, so it carries the same field.

Two refusal reasons also go beyond §6's table, both documented at their
definitions in ``app/broker_configuration/errors.py``:
``display_name_conflict`` (409) and ``live_envelope_invalid`` (422).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.records import (
    AccountNickname,
    AlpacaDeskState,
    BrokerProfile,
    ConfigurationEvent,
    CredentialSlotStatus,
    DeskAccountChoice,
    DeskAction,
    DeskLifecycleStep,
    DeskSelectionSummary,
    InstallationSelection,
    LocalOwner,
    ObservedAccount,
    ProfileRevision,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

# The three request-body names that would claim an identity the server owns.
SERVER_RESOLVED_FIELDS: tuple[str, ...] = ("owner_id", "actor", "user_id")

_NAME = Field(min_length=1, max_length=120)
_SLOT = Field(min_length=1, max_length=120)


class _ClosedRequest(BaseModel):
    """Every mutating body: closed, so an unknown field is refused."""

    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LiveEnvelopePayload(BaseModel):
    """The six values, named exactly as ``LiveEnvelopeValues`` names them.

    The mapping to the dataclass is an identity, so no rename layer can drift
    (contract §2.4). The bounds restate ``AlpacaSettings``' domain for an early,
    field-level 422; ``ValidatedLiveEnvelope`` enforces the same domain again on
    every path into storage, which is where the rule actually lives.

    ``strict=True`` is load-bearing, not tidiness. In Pydantic's default lax
    mode this DTO sits *in front* of ``ValidatedLiveEnvelope`` and normalises
    before it: ``{"shadow_sessions": true}`` would arrive as ``1`` and the
    by-name ``int`` check downstream would never see the boolean it exists to
    refuse — a real-money session count silently minted from ``true``. Strict
    ``int`` refuses ``True``, ``1.0`` and ``"3"``; strict ``float`` still
    accepts an ``int`` and widens it, which is exactly what ``AlpacaSettings``'
    ``float`` annotation does with ``5000``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    loss_fraction: float = Field(gt=0, lt=1, allow_inf_nan=False)
    loss_usd: float = Field(gt=0, allow_inf_nan=False)
    shadow_sessions: int = Field(ge=1)
    arming_max_sessions: int = Field(ge=1)
    xh_entry_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)
    xh_exit_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)

    @classmethod
    def from_record(cls, envelope: ValidatedLiveEnvelope | None) -> LiveEnvelopePayload | None:
        return None if envelope is None else cls(**envelope.to_mapping())


class OwnerResponse(_Response):
    owner_id: str
    display_label: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    updated_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_record(cls, owner: LocalOwner) -> OwnerResponse:
        return cls(
            owner_id=owner.owner_id,
            display_label=owner.display_label,
            created_at_ms=owner.created_at_ms,
            updated_at_ms=owner.updated_at_ms,
        )


class OwnerPatchRequest(_ClosedRequest):
    display_label: str = _NAME


class CredentialSlotResponse(_Response):
    """Labels and availability only — never a value, fragment, or var name."""

    slot: str
    label: str
    available: bool
    verified_account_id: str | None = None
    verified_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_record(cls, status: CredentialSlotStatus) -> CredentialSlotResponse:
        return cls(
            slot=status.slot,
            label=status.label,
            available=status.available,
            verified_account_id=status.verified_account_id,
            verified_at_ms=status.verified_at_ms,
        )


class CredentialSlotsResponse(_Response):
    slots: tuple[CredentialSlotResponse, ...]


class DeskLifecycleStepResponse(_Response):
    key: Literal["effective_configuration", "selected_configuration", "worker_handoff"]
    label: str
    status: Literal["complete", "current", "pending"]
    status_label: str

    @classmethod
    def from_record(cls, step: DeskLifecycleStep) -> DeskLifecycleStepResponse:
        return cls(
            key=step.key,
            label=step.label,
            status=step.status,
            status_label=step.status_label,
        )


class DeskActionResponse(_Response):
    kind: Literal[
        "review_configuration",
        "review_staged_configuration",
        "view_restart_steps",
    ]
    label: str
    enabled: bool

    @classmethod
    def from_record(cls, action: DeskAction) -> DeskActionResponse:
        return cls(kind=action.kind, label=action.label, enabled=action.enabled)


class DeskSelectionSummaryResponse(_Response):
    selection_id: str
    profile_id: str
    revision: int = Field(ge=1)
    profile_label: str
    account_id: str | None
    nickname: str | None
    account_label: str
    endpoint_mode: Literal["paper", "live"]
    badge_label: str
    description: str

    @classmethod
    def from_record(cls, summary: DeskSelectionSummary) -> DeskSelectionSummaryResponse:
        return cls(
            selection_id=summary.selection_id,
            profile_id=summary.profile_id,
            revision=summary.revision,
            profile_label=summary.profile_label,
            account_id=summary.account_id,
            nickname=summary.nickname,
            account_label=summary.account_label,
            endpoint_mode=summary.endpoint_mode,
            badge_label=summary.badge_label,
            description=summary.description,
        )


class DeskAccountChoiceResponse(DeskSelectionSummaryResponse):
    account_id: str
    action_kind: Literal["review_configuration", "review_staged_configuration"]
    action_label: str
    is_staged: bool
    is_effective: bool

    @classmethod
    def from_record(cls, choice: DeskAccountChoice) -> DeskAccountChoiceResponse:
        return cls(
            selection_id=choice.selection_id,
            profile_id=choice.profile_id,
            revision=choice.revision,
            profile_label=choice.profile_label,
            account_id=choice.account_id,
            nickname=choice.nickname,
            account_label=choice.account_label,
            endpoint_mode=choice.endpoint_mode,
            badge_label=choice.badge_label,
            description=choice.description,
            action_kind=choice.action_kind,
            action_label=choice.action_label,
            is_staged=choice.is_staged,
            is_effective=choice.is_effective,
        )


class AlpacaDeskStateResponse(_Response):
    activation_state: Literal[
        "no_selection",
        "staged_not_applied",
        "apply_requested_restart_required",
        "effective_selection",
    ]
    headline: str
    detail: str
    lifecycle: tuple[DeskLifecycleStepResponse, ...]
    selection_label: str
    consequence: str
    action: DeskActionResponse
    selection_generation: int = Field(ge=0)
    staged_choice: DeskSelectionSummaryResponse | None
    effective_choice: DeskSelectionSummaryResponse | None
    choices: tuple[DeskAccountChoiceResponse, ...]
    empty_choices_message: str | None
    profiles_requiring_setup: int = Field(ge=0)
    setup_required_message: str | None

    @classmethod
    def from_record(cls, state: AlpacaDeskState) -> AlpacaDeskStateResponse:
        return cls(
            activation_state=state.activation_state,
            headline=state.headline,
            detail=state.detail,
            lifecycle=tuple(
                DeskLifecycleStepResponse.from_record(step) for step in state.lifecycle
            ),
            selection_label=state.selection_label,
            consequence=state.consequence,
            action=DeskActionResponse.from_record(state.action),
            selection_generation=state.selection_generation,
            staged_choice=(
                None
                if state.staged_choice is None
                else DeskSelectionSummaryResponse.from_record(state.staged_choice)
            ),
            effective_choice=(
                None
                if state.effective_choice is None
                else DeskSelectionSummaryResponse.from_record(state.effective_choice)
            ),
            choices=tuple(
                DeskAccountChoiceResponse.from_record(choice) for choice in state.choices
            ),
            empty_choices_message=state.empty_choices_message,
            profiles_requiring_setup=state.profiles_requiring_setup,
            setup_required_message=state.setup_required_message,
        )


class ProfileResponse(_Response):
    profile_id: str
    owner_id: str
    broker: Literal["alpaca"]
    display_name: str
    archived: bool
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    updated_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_record(cls, profile: BrokerProfile) -> ProfileResponse:
        return cls(
            profile_id=profile.profile_id,
            owner_id=profile.owner_id,
            broker="alpaca",
            display_name=profile.display_name,
            archived=profile.archived,
            created_at_ms=profile.created_at_ms,
            updated_at_ms=profile.updated_at_ms,
        )


class ProfileListResponse(_Response):
    profiles: tuple[ProfileResponse, ...]


class RevisionResponse(_Response):
    profile_id: str
    revision: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    credential_slot: str
    endpoint_mode: Literal["paper", "live"]
    account_pin: str | None
    account_pinned_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    live_envelope: LiveEnvelopePayload | None
    content_sha256: str
    complete: bool
    author_owner_id: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_record(cls, revision: ProfileRevision) -> RevisionResponse:
        return cls(
            profile_id=revision.profile_id,
            revision=revision.revision,
            schema_version=revision.schema_version,
            credential_slot=revision.credential_slot,
            endpoint_mode=revision.endpoint_mode,
            account_pin=revision.account_pin,
            account_pinned_at_ms=revision.account_pinned_at_ms,
            live_envelope=LiveEnvelopePayload.from_record(revision.live_envelope),
            content_sha256=revision.content_sha256,
            complete=revision.complete,
            author_owner_id=revision.author_owner_id,
            created_at_ms=revision.created_at_ms,
        )


class RevisionListResponse(_Response):
    revisions: tuple[RevisionResponse, ...]


class ProfileDetailResponse(_Response):
    profile: ProfileResponse
    latest_revision: RevisionResponse | None


class RevisionContentRequest(_ClosedRequest):
    """The configured content of a revision. ``live`` requires the envelope."""

    credential_slot: str = _SLOT
    endpoint_mode: Literal["paper", "live"]
    live_envelope: LiveEnvelopePayload | None = None


class ProfileCreateRequest(RevisionContentRequest):
    display_name: str = _NAME


class ProfilePatchRequest(_ClosedRequest):
    """Metadata only. A rename never invalidates an arming (ADR 0060 D4)."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


class ProfileCloneRequest(_ClosedRequest):
    display_name: str = _NAME


class RevisionCreateRequest(RevisionContentRequest):
    expected_revision: int = Field(ge=0)


class AccountPinRequest(_ClosedRequest):
    account_id: str = Field(min_length=1, max_length=120)


class ObservedAccountResponse(_Response):
    account_id: str
    account_mode: Literal["paper", "live"]
    account_status: str | None = None

    @classmethod
    def from_record(cls, account: ObservedAccount) -> ObservedAccountResponse:
        return cls(
            account_id=account.account_id,
            account_mode=account.account_mode,
            account_status=account.account_status,
        )


class AccountVerificationResponse(_Response):
    observed_accounts: tuple[ObservedAccountResponse, ...]


class NicknameResponse(_Response):
    account_id: str
    nickname: str
    updated_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_record(cls, nickname: AccountNickname) -> NicknameResponse:
        return cls(
            account_id=nickname.account_id,
            nickname=nickname.nickname,
            updated_at_ms=nickname.updated_at_ms,
        )


class NicknameListResponse(_Response):
    nicknames: tuple[NicknameResponse, ...]


class NicknamePutRequest(_ClosedRequest):
    nickname: str = _NAME


class SelectionResponse(_Response):
    """Staged **and** effective, always both (contract §4).

    ``effective_acknowledged_at_ms`` is a historical acknowledgement: it says a
    worker once bound this revision, never that one is running now.
    """

    staged_profile_id: str | None
    staged_revision: int | None
    apply_requested: bool
    apply_requested_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    apply_requested_generation: int | None = Field(default=None, ge=0)
    selection_generation: int = Field(ge=0)
    effective_profile_id: str | None
    effective_revision: int | None
    effective_account_id: str | None
    effective_acknowledged_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    last_apply_outcome: Literal["applied", "refused"] | None
    last_apply_refusal_reason: str | None

    @classmethod
    def from_record(cls, selection: InstallationSelection) -> SelectionResponse:
        return cls(
            staged_profile_id=selection.staged_profile_id,
            staged_revision=selection.staged_revision,
            apply_requested=selection.apply_requested,
            apply_requested_at_ms=selection.apply_requested_at_ms,
            apply_requested_generation=selection.apply_requested_generation,
            selection_generation=selection.selection_generation,
            effective_profile_id=selection.effective_profile_id,
            effective_revision=selection.effective_revision,
            effective_account_id=selection.effective_account_id,
            effective_acknowledged_at_ms=selection.effective_acknowledged_at_ms,
            last_apply_outcome=selection.last_apply_outcome,
            last_apply_refusal_reason=selection.last_apply_refusal_reason,
        )


class SelectionPutRequest(_ClosedRequest):
    """Staging, not switching. Navigating the UI is not staging."""

    profile_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    expected_selection_generation: int = Field(ge=0)


class ApplyRequest(_ClosedRequest):
    expected_selection_generation: int = Field(ge=0)


class ConfigurationEventResponse(_Response):
    event_id: str
    actor_owner_id: str
    action: str
    profile_id: str | None
    revision: int | None
    previous_ref: str | None
    next_ref: str | None
    result: str
    recorded_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_record(cls, event: ConfigurationEvent) -> ConfigurationEventResponse:
        return cls(
            event_id=event.event_id,
            actor_owner_id=event.actor_owner_id,
            action=event.action,
            profile_id=event.profile_id,
            revision=event.revision,
            previous_ref=event.previous_ref,
            next_ref=event.next_ref,
            result=event.result,
            recorded_at_ms=event.recorded_at_ms,
        )


class ConfigurationEventListResponse(_Response):
    events: tuple[ConfigurationEventResponse, ...]


__all__ = [
    "SERVER_RESOLVED_FIELDS",
    "AccountPinRequest",
    "AccountVerificationResponse",
    "AlpacaDeskStateResponse",
    "ApplyRequest",
    "ConfigurationEventListResponse",
    "ConfigurationEventResponse",
    "CredentialSlotResponse",
    "CredentialSlotsResponse",
    "LiveEnvelopePayload",
    "NicknameListResponse",
    "NicknamePutRequest",
    "NicknameResponse",
    "ObservedAccountResponse",
    "OwnerPatchRequest",
    "OwnerResponse",
    "ProfileCloneRequest",
    "ProfileCreateRequest",
    "ProfileDetailResponse",
    "ProfileListResponse",
    "ProfilePatchRequest",
    "ProfileResponse",
    "RevisionContentRequest",
    "RevisionCreateRequest",
    "RevisionListResponse",
    "RevisionResponse",
    "SelectionPutRequest",
    "SelectionResponse",
]

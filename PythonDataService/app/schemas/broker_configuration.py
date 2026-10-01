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

Three refusal reasons also go beyond §6's table, each documented at its
definition in ``app/broker_configuration/errors.py``:
``display_name_conflict`` (409), ``live_envelope_invalid`` (422) and
``paper_allowances_invalid`` (422).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.broker_configuration.envelope import (
    InvalidLiveEnvelope,
    ValidatedLiveEnvelope,
    ValidatedPaperAllowances,
    require_whole_cent_loss_cap,
)
from app.broker_configuration.records import (
    AccountNickname,
    AlpacaDeskState,
    BrokerProfile,
    CredentialSlotStatus,
    DeskAccountChoice,
    DeskAction,
    DeskLifecycleStep,
    DeskSelectionSummary,
    InstallationSelection,
    ObservedAccount,
    ProfileRevision,
)
from app.schemas.exit_terms import ExitTermsInput
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
    """The four current account bounds; retired session counts are rejected.

    Historical revisions retain their full hash-bearing representation in storage.
    The configuration API projects only the fields a trader can currently edit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    loss_fraction: float = Field(gt=0, lt=1, allow_inf_nan=False)
    loss_usd: float = Field(gt=0, allow_inf_nan=False)
    xh_entry_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)
    xh_exit_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)

    @classmethod
    def from_record(cls, envelope: ValidatedLiveEnvelope | None) -> LiveEnvelopePayload | None:
        return None if envelope is None else cls(
            **{field: getattr(envelope, field) for field in cls.model_fields}
        )


class PaperXhAllowancesPayload(BaseModel):
    """A paper revision's own extended-hours allowances, in basis points.

    How far from the price an extended-hours or after-close limit is placed:
    an entry by ``xh_entry_bps`` and an exit by ``xh_exit_bps``, above the
    price for a buy and below it for a sell (a short entry sells the entry
    allowance below; a cover buys the exit allowance above). The price is the
    decision bar's close for a leg placed as the program decides, and the live
    bid (sell) or ask (buy) for an exit priced later — the automatic re-drive,
    or the send-time re-price of an exit sent after its session. A
    regular-hours run's exits outside the session -- a manual Flatten, the
    watchdog's re-drive of a refused exit, an exit that reaches the broker
    after the close -- are such limits, so Start of a regular-hours run refuses
    ``EXTENDED_HOURS_ALLOWANCE_UNSET`` until both are set. A held position is
    never carried into a new deployment; resolving it requires Flatten before
    a fresh Deploy (#2504).

    Paper only, and only the two: a live revision carries its pair inside
    ``live_envelope``, sealed at Deploy. ``extra="forbid"`` refuses a live-only
    value (``loss_usd``, a session count) offered here rather than dropping it,
    and ``strict=True`` refuses ``true`` or ``"5"`` for the reason
    ``LiveEnvelopePayload`` states. The bounds restate the envelope's; the
    domain itself lives in ``ValidatedPaperAllowances``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    xh_entry_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)
    xh_exit_bps: float = Field(ge=0, lt=10_000, allow_inf_nan=False)

    @classmethod
    def from_record(
        cls, allowances: ValidatedPaperAllowances | None
    ) -> PaperXhAllowancesPayload | None:
        return None if allowances is None else cls(**allowances.to_mapping())


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
    action_kind: Literal[
        "review_configuration",
        "review_staged_configuration",
        "view_restart_steps",
    ]
    action_label: str
    action_consequence: str
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
            action_consequence=choice.action_consequence,
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
    restart_command: str | None

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
            restart_command=state.restart_command,
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
    default_exit_terms: ExitTermsInput | None = None
    paper_xh_allowances: PaperXhAllowancesPayload | None
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
            paper_xh_allowances=PaperXhAllowancesPayload.from_record(revision.paper_xh_allowances),
            default_exit_terms=revision.default_exit_terms,
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
    """The configured content of a revision. ``live`` requires the envelope.

    ``paper_xh_allowances`` is for a paper revision without an envelope only;
    the service refuses it beside an envelope or on a live revision
    (``paper_allowances_invalid``), so a revision's allowances live in one place.
    """

    credential_slot: str = _SLOT
    endpoint_mode: Literal["paper", "live"]
    live_envelope: LiveEnvelopePayload | None = None
    default_exit_terms: ExitTermsInput | None = None
    paper_xh_allowances: PaperXhAllowancesPayload | None = None


class ProfileCreateRequest(RevisionContentRequest):
    display_name: str = _NAME


class ProfilePatchRequest(_ClosedRequest):
    """Metadata only: a rename or an archive changes no revision's values (ADR 0060 D4)."""

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
    # This field's max_length (120, via `_NAME`) must equal
    # `app/broker/fleet/records.py`'s `_ACCOUNT_NICKNAME_MAX_CHARS` — the
    # fleet lane summary's reader-side bound on the same value. Pinned by
    # test_account_nickname_bound_matches_the_writers_own_bound
    # (test_admission_probes_2026_09_13.py); raise both together.
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


__all__ = [
    "SERVER_RESOLVED_FIELDS",
    "AccountPinRequest",
    "AccountVerificationResponse",
    "AlpacaDeskStateResponse",
    "ApplyRequest",
    "CredentialSlotResponse",
    "CredentialSlotsResponse",
    "LiveEnvelopePayload",
    "NicknameListResponse",
    "NicknamePutRequest",
    "NicknameResponse",
    "ObservedAccountResponse",
    "PaperXhAllowancesPayload",
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


class AccountRiskApplyRequest(_ClosedRequest):
    expected_risk_revision: int = Field(ge=0, strict=True)
    expected_selection_generation: int = Field(ge=0, strict=True)
    loss_fraction: float = Field(gt=0, lt=1, strict=True, allow_inf_nan=False)
    loss_usd: float = Field(gt=0, strict=True, allow_inf_nan=False)

    @field_validator("loss_usd")
    @classmethod
    def whole_cent_cap(cls, value: float) -> float:
        # Delegates to the canonical whole-cent check (app/broker_configuration/envelope.py)
        # so this boundary never disagrees with the domain object it feeds.
        try:
            require_whole_cent_loss_cap(value)
        except InvalidLiveEnvelope as exc:
            raise ValueError(str(exc)) from exc
        return value


class AccountRiskStateResponse(_Response):
    account_id: str
    risk_revision: int
    selection_generation: int
    loss_fraction: float | None
    loss_usd: float | None
    applied_at_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    entry_state: Literal["ready", "held", "unknown"]
    detail: str
    # No limit is set at all, so setting one is the fix and re-reading cannot
    # settle an `unknown` state; otherwise it waits on account evidence.
    limit_missing: bool
    # The standing hold's limit as a Python-authored dollar string (the sealed
    # float each normalized on its own, #2612); the browser renders it verbatim
    # and does no money arithmetic.
    hold_loss_limit_usd: str | None = None
    hold_session_start_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    hold_policy_revision: int | None = None


class AccountRiskClearRequest(_ClosedRequest):
    expected_risk_revision: int = Field(ge=0, strict=True)
    expected_selection_generation: int = Field(ge=0, strict=True)

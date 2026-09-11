"""The backend-authored Alpaca desk account-selection read model."""

from __future__ import annotations

from pathlib import Path

from app.broker_configuration.desk_state import project_desk_state
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.records import CredentialSlotStatus
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    OPERATOR_IDENTITY,
    FakeAccountVerifier,
    FrozenClock,
    paper_profile,
)


class _SingleUseSlotDirectory:
    """Allow profile creation, then prove the read model never probes slots."""

    def __init__(self) -> None:
        self.calls = 0

    def list_slots(self) -> tuple[CredentialSlotStatus, ...]:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("desk-state must not resolve credential availability")
        return (
            CredentialSlotStatus(
                slot="alpaca_paper_primary",
                label="Paper — primary",
                available=True,
            ),
        )


async def _pinned_paper_profile(
    service: BrokerConfigurationService, *, display_name: str = "Alpaca-Paper"
) -> str:
    created = paper_profile(service, display_name=display_name)
    await service.pin_account(
        created.profile.profile_id,
        1,
        account_id="PA000PAPER",
    )
    return created.profile.profile_id


def test_desk_state_does_not_probe_credential_availability(
    clerk_dir: Path, clock: FrozenClock, verifier: FakeAccountVerifier
) -> None:
    slots = _SingleUseSlotDirectory()
    service = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=slots,
        account_verifier=verifier,
    )
    try:
        paper_profile(service)

        state = service.desk_state()

        assert state.activation_state == "no_selection"
        assert slots.calls == 1
    finally:
        service.close()


async def test_desk_state_offers_only_complete_explicitly_pinned_revisions(
    service: BrokerConfigurationService,
) -> None:
    pinned_profile_id = await _pinned_paper_profile(service)
    paper_profile(service, display_name="Unverified paper")
    service.create_profile(
        display_name="Incomplete live",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=None,
    )
    service.set_nickname("PA000PAPER", nickname="Primary paper")

    state = service.desk_state()

    assert len(state.choices) == 1
    choice = state.choices[0]
    assert choice.selection_id == f"{pinned_profile_id}@1"
    assert choice.profile_id == pinned_profile_id
    assert choice.revision == 1
    assert choice.account_id == "PA000PAPER"
    assert choice.nickname == "Primary paper"
    assert choice.account_label == "Primary paper"
    assert choice.endpoint_mode == "paper"
    assert choice.badge_label == "Paper account"
    assert choice.action_kind == "review_configuration"
    assert choice.action_label == "Review Alpaca-Paper"
    assert choice.action_consequence == (
        "Reviewing this saved revision does not stage or apply it, restart a worker, or arm "
        "live trading."
    )
    assert choice.is_staged is False
    assert choice.is_effective is False
    assert state.empty_choices_message is None
    assert state.profiles_requiring_setup == 2
    assert state.setup_required_message == (
        "2 saved profiles still need configuration or account verification before they can "
        "be selected."
    )


async def test_desk_state_keeps_an_approved_revision_after_a_new_draft_is_saved(
    service: BrokerConfigurationService,
) -> None:
    profile_id = await _pinned_paper_profile(service)
    service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )

    state = service.desk_state()

    assert [choice.selection_id for choice in state.choices] == [f"{profile_id}@1"]
    assert state.profiles_requiring_setup == 0
    assert state.setup_required_message is None


async def test_projector_authors_the_live_choice_safety_copy(
    service: BrokerConfigurationService,
) -> None:
    created = service.create_profile(
        display_name="Live candidate",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )
    profile_id = created.profile.profile_id
    pinned = await service.pin_account(profile_id, 1, account_id="9LIVE0001")

    state = project_desk_state(
        selection=service.selection(),
        profiles=service.list_profiles(),
        revisions_by_profile={profile_id: [pinned]},
        staged_revision=None,
        effective_revision=None,
        nicknames=service.list_nicknames(),
        has_archived_profiles=False,
    )

    assert state.choices[0].description == (
        "Live candidate uses the verified live account. Selecting this profile does not arm "
        "live trading."
    )


async def test_desk_state_tracks_stage_apply_and_effective_without_claiming_connectivity(
    service: BrokerConfigurationService,
) -> None:
    profile_id = await _pinned_paper_profile(service)
    alternate_profile_id = await _pinned_paper_profile(
        service,
        display_name="Paper alternate",
    )

    staged = service.stage_selection(
        profile_id=profile_id,
        revision=1,
        expected_selection_generation=0,
    )
    staged_state = service.desk_state()

    assert staged_state.activation_state == "staged_not_applied"
    assert staged_state.selection_generation == staged.selection_generation
    assert staged_state.staged_choice is not None
    assert staged_state.staged_choice.selection_id == f"{profile_id}@1"
    assert staged_state.effective_choice is None
    assert staged_state.action.kind == "review_staged_configuration"
    assert staged_state.action.label == "Review & apply Alpaca-Paper"
    assert staged_state.choices[0].action_kind == "review_staged_configuration"
    assert staged_state.choices[0].action_consequence == staged_state.consequence

    requested = service.request_apply(
        expected_selection_generation=staged.selection_generation,
    )
    restart_state = service.desk_state()

    assert restart_state.activation_state == "apply_requested_restart_required"
    assert restart_state.selection_generation == requested.selection_generation
    assert restart_state.action.kind == "view_restart_steps"
    assert restart_state.choices[0].action_kind == "view_restart_steps"
    assert restart_state.choices[0].action_label == "View restart steps"
    assert restart_state.choices[0].action_consequence == restart_state.consequence
    alternate_choice = next(
        choice for choice in restart_state.choices if choice.profile_id == alternate_profile_id
    )
    assert alternate_choice.action_kind == "review_configuration"
    assert alternate_choice.action_consequence == (
        "Reviewing this saved revision does not stage or apply it, restart a worker, or arm "
        "live trading."
    )
    assert "controlled restart" in restart_state.detail
    assert "does not arm live trading" in restart_state.consequence

    acknowledged = service.acknowledge_effective(
        profile_id=profile_id,
        revision=1,
        account_id="PA000PAPER",
        expected_selection_generation=requested.selection_generation,
    )
    effective_state = service.desk_state()

    assert effective_state.activation_state == "effective_selection"
    assert effective_state.selection_generation == acknowledged.selection_generation
    assert effective_state.effective_choice is not None
    assert effective_state.effective_choice.account_id == "PA000PAPER"
    assert effective_state.staged_choice == effective_state.effective_choice
    assert effective_state.choices[0].action_kind == "review_configuration"
    assert effective_state.choices[0].action_label == "Review Alpaca-Paper"
    assert "not a connectivity claim" in effective_state.consequence

    service.request_apply(
        expected_selection_generation=acknowledged.selection_generation,
    )
    repeat_restart_state = service.desk_state()

    assert repeat_restart_state.activation_state == "apply_requested_restart_required"
    assert repeat_restart_state.choices[0].is_staged is True
    assert repeat_restart_state.choices[0].is_effective is True
    assert repeat_restart_state.choices[0].action_kind == "view_restart_steps"
    assert repeat_restart_state.choices[0].action_label == "View restart steps"


def test_desk_state_distinguishes_archived_profiles_from_a_fresh_installation(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service, display_name="Archived paper")
    service.update_profile(created.profile.profile_id, archived=True)

    state = service.desk_state()

    assert state.choices == ()
    assert state.profiles_requiring_setup == 0
    assert state.empty_choices_message == (
        "All saved account configurations are archived. Restore one on the Configuration "
        "page, or set up a new account."
    )
    assert state.action.kind == "review_configuration"
    assert state.action.label == "Review account configurations"


async def test_desk_state_keeps_effective_and_staged_drift_visibly_separate(
    service: BrokerConfigurationService,
) -> None:
    effective_profile_id = await _pinned_paper_profile(
        service,
        display_name="Paper primary",
    )
    service.stage_selection(
        profile_id=effective_profile_id,
        revision=1,
        expected_selection_generation=0,
    )
    pending = service.request_apply(expected_selection_generation=1)
    applied = service.acknowledge_effective(
        profile_id=effective_profile_id,
        revision=1,
        account_id="PA000PAPER",
        expected_selection_generation=pending.selection_generation,
    )

    candidate = service.create_profile(
        display_name="Live candidate",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )
    await service.pin_account(
        candidate.profile.profile_id,
        1,
        account_id="9LIVE0001",
    )
    service.stage_selection(
        profile_id=candidate.profile.profile_id,
        revision=1,
        expected_selection_generation=applied.selection_generation,
    )

    state = service.desk_state()

    assert state.activation_state == "staged_not_applied"
    assert state.effective_choice is not None
    assert state.effective_choice.profile_id == effective_profile_id
    assert state.staged_choice is not None
    assert state.staged_choice.profile_id == candidate.profile.profile_id
    assert state.staged_choice.description == (
        "Live candidate uses the verified live account. Selecting this profile does not arm "
        "live trading."
    )
    assert state.headline == "Paper primary remains the effective selection"
    assert state.lifecycle[0].status == "complete"
    assert state.lifecycle[0].status_label == "Complete"
    assert state.lifecycle[1].status == "current"
    assert state.lifecycle[1].status_label == "Current step"

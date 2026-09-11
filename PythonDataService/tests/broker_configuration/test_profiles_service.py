"""Profiles, immutable revisions, archive rules, and the pin.

The behaviour half of package B's "done when": create/edit/clone/archive,
concurrent stale edits return conflicts, archive rules preserve references.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import (
    AccountModeDisagreement,
    AccountPinMismatch,
    AccountVerificationFailed,
    DisplayNameConflict,
    ProfileArchived,
    ProfileInUse,
    ProfileNotFound,
    RevisionConflict,
    RevisionNotFound,
)
from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.seams import UnconfiguredAccountVerifier
from app.broker_configuration.service import BrokerConfigurationService, revision_content_sha256
from app.broker_configuration.store import ProfilesStore
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    OPERATOR_IDENTITY,
    FakeAccountVerifier,
    FrozenClock,
    paper_profile,
)


def test_the_owner_is_generated_once_and_seeded_from_the_operator_identity(
    service: BrokerConfigurationService,
) -> None:
    first = service.owner()
    second = service.owner()

    assert first == second
    assert first.display_label == OPERATOR_IDENTITY
    assert first.owner_id.startswith("owner_")


def test_renaming_the_owner_keeps_the_owner_id(service: BrokerConfigurationService) -> None:
    before = service.owner()

    renamed = service.rename_owner(display_label="Desk operator")

    assert renamed.owner_id == before.owner_id
    assert service.owner().display_label == "Desk operator"


def test_creating_a_profile_creates_revision_one(service: BrokerConfigurationService) -> None:
    created = paper_profile(service)

    assert created.profile.broker == "alpaca"
    assert created.profile.owner_id == service.owner().owner_id
    assert created.latest_revision is not None
    assert created.latest_revision.revision == 1
    assert created.latest_revision.complete is True
    assert created.latest_revision.account_pin is None


def test_a_live_revision_without_an_envelope_is_an_incomplete_draft(
    service: BrokerConfigurationService,
) -> None:
    created = service.create_profile(
        display_name="Live — draft",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=None,
    )

    assert created.latest_revision is not None
    assert created.latest_revision.complete is False


def test_two_live_profiles_cannot_share_a_display_name(service: BrokerConfigurationService) -> None:
    paper_profile(service, display_name="Paper — testing")

    with pytest.raises(DisplayNameConflict):
        paper_profile(service, display_name="Paper — testing")


def test_an_archived_profile_releases_its_display_name(service: BrokerConfigurationService) -> None:
    created = paper_profile(service, display_name="Paper — testing")
    service.update_profile(created.profile.profile_id, archived=True)

    reused = paper_profile(service, display_name="Paper — testing")

    assert reused.profile.profile_id != created.profile.profile_id


def test_listing_hides_archived_profiles_unless_asked(service: BrokerConfigurationService) -> None:
    kept = paper_profile(service, display_name="Kept")
    archived = paper_profile(service, display_name="Archived")
    service.update_profile(archived.profile.profile_id, archived=True)

    assert [profile.profile_id for profile in service.list_profiles()] == [kept.profile.profile_id]
    assert len(service.list_profiles(include_archived=True)) == 2


def test_archiving_preserves_every_revision_and_event(service: BrokerConfigurationService) -> None:
    created = paper_profile(service)
    service.create_revision(
        created.profile.profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )

    service.update_profile(created.profile.profile_id, archived=True)

    assert len(service.list_revisions(created.profile.profile_id)) == 2
    assert service.read_revision(created.profile.profile_id, 1).revision == 1
    assert any(event.action == "profile_archived" for event in service.events())


def test_archiving_a_staged_profile_is_refused(service: BrokerConfigurationService) -> None:
    created = paper_profile(service)
    service.stage_selection(
        profile_id=created.profile.profile_id, revision=1, expected_selection_generation=0
    )

    with pytest.raises(ProfileInUse):
        service.update_profile(created.profile.profile_id, archived=True)


@pytest.mark.parametrize("renamed", [None, "Restored profile"])
def test_restoring_a_profile_records_restoration_in_the_audit_history(
    service: BrokerConfigurationService, renamed: str | None,
) -> None:
    created = paper_profile(service)
    profile_id = created.profile.profile_id
    service.update_profile(profile_id, archived=True)
    before = service.events()

    restored = service.update_profile(profile_id, archived=False, display_name=renamed)

    assert not restored.archived
    added = [event for event in service.events() if event not in before]
    assert len(added) == 1
    assert added[0].action == "profile_restored"
    assert added[0].profile_id == profile_id
    assert added[0].previous_ref == created.profile.display_name
    assert added[0].next_ref == restored.display_name


def test_a_new_revision_is_appended_and_the_old_one_is_untouched(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service)
    original = created.latest_revision
    assert original is not None

    second = service.create_revision(
        created.profile.profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )

    assert second.revision == 2
    assert service.read_revision(created.profile.profile_id, 1) == original


def test_a_stale_edit_conflicts_instead_of_overwriting(service: BrokerConfigurationService) -> None:
    created = paper_profile(service)
    service.create_revision(
        created.profile.profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )

    with pytest.raises(RevisionConflict):
        service.create_revision(
            created.profile.profile_id,
            expected_revision=1,
            credential_slot="alpaca_paper_tertiary",
            endpoint_mode="paper",
            live_envelope=None,
        )
    assert len(service.list_revisions(created.profile.profile_id)) == 2


def test_a_second_connection_holding_a_stale_revision_conflicts(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    """Two tabs, two connections, one winner — not a silent clobber.

    Sequential on purpose: this is the ordinary two-tab case, where the second
    tab's read simply predates the first tab's write. The *interleaved* case —
    a rival landing between this caller's read and its write — is
    ``test_store_concurrency.py``.
    """
    first_tab = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
    )
    second_tab = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
    )
    try:
        created = paper_profile(first_tab)
        profile_id = created.profile.profile_id
        assert second_tab.read_revision(profile_id, 1).revision == 1

        first_tab.create_revision(
            profile_id,
            expected_revision=1,
            credential_slot="from-first-tab",
            endpoint_mode="paper",
            live_envelope=None,
        )
        with pytest.raises(RevisionConflict):
            second_tab.create_revision(
                profile_id,
                expected_revision=1,
                credential_slot="from-second-tab",
                endpoint_mode="paper",
                live_envelope=None,
            )

        assert second_tab.read_revision(profile_id, 2).credential_slot == "from-first-tab"
    finally:
        first_tab.close()
        second_tab.close()


def test_repeating_a_create_with_identical_content_returns_the_existing_revision(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service)
    second = service.create_revision(
        created.profile.profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )

    # The retry a client sends when the first response was lost.
    replay = service.create_revision(
        created.profile.profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    # And a re-save of unchanged content against the current revision.
    resave = service.create_revision(
        created.profile.profile_id,
        expected_revision=2,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )

    assert replay == second
    assert resave == second
    assert len(service.list_revisions(created.profile.profile_id)) == 2


def test_a_revision_cannot_be_added_to_an_archived_profile(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service)
    service.update_profile(created.profile.profile_id, archived=True)

    with pytest.raises(ProfileArchived):
        service.create_revision(
            created.profile.profile_id,
            expected_revision=1,
            credential_slot="alpaca_paper_primary",
            endpoint_mode="paper",
            live_envelope=None,
        )


async def test_cloning_copies_content_and_carries_no_pin(
    service: BrokerConfigurationService, verifier: FakeAccountVerifier
) -> None:
    created = paper_profile(service)
    profile_id = created.profile.profile_id

    await service.pin_account(profile_id, 1, account_id="PA000PAPER")
    source = service.read_revision(profile_id, 1)
    assert source.account_pin == "PA000PAPER"

    clone = service.clone_profile(profile_id, display_name="Paper — copy")

    assert clone.latest_revision is not None
    assert clone.latest_revision.account_pin is None
    assert clone.latest_revision.content_sha256 == source.content_sha256
    assert clone.profile.profile_id != profile_id
    assert verifier.calls == [("alpaca_paper_primary", "paper")]


async def test_the_pin_is_outside_the_content_hash(service: BrokerConfigurationService) -> None:
    """Binding an account must not change the identity a stale-edit check uses."""
    created = paper_profile(service)
    before = service.read_revision(created.profile.profile_id, 1)

    await service.pin_account(created.profile.profile_id, 1, account_id="PA000PAPER")
    after = service.read_revision(created.profile.profile_id, 1)

    assert after.content_sha256 == before.content_sha256
    assert after.content_sha256 == revision_content_sha256(
        credential_slot="alpaca_paper_primary", endpoint_mode="paper", live_envelope=None
    )


async def test_pinning_an_unobserved_account_is_refused(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service)

    with pytest.raises(AccountPinMismatch):
        await service.pin_account(created.profile.profile_id, 1, account_id="TYPED-BY-HAND")
    assert service.read_revision(created.profile.profile_id, 1).account_pin is None


async def test_pinning_an_account_of_the_wrong_mode_is_refused(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    mismatched = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id="9LIVE0001", account_mode="live")
        ),
    )
    try:
        created = paper_profile(mismatched)

        with pytest.raises(AccountModeDisagreement):
            await mismatched.pin_account(created.profile.profile_id, 1, account_id="9LIVE0001")
    finally:
        mismatched.close()


async def test_repinning_a_different_account_is_refused_and_keeps_the_previous_pin(
    service: BrokerConfigurationService, verifier: FakeAccountVerifier
) -> None:
    created = paper_profile(service)
    await service.pin_account(created.profile.profile_id, 1, account_id="PA000PAPER")
    verifier.accounts.append(ObservedAccount(account_id="PA111OTHER", account_mode="paper"))

    with pytest.raises(AccountPinMismatch):
        await service.pin_account(created.profile.profile_id, 1, account_id="PA111OTHER")
    assert service.read_revision(created.profile.profile_id, 1).account_pin == "PA000PAPER"


async def test_repinning_the_same_account_is_idempotent(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service)
    first = await service.pin_account(created.profile.profile_id, 1, account_id="PA000PAPER")

    second = await service.pin_account(created.profile.profile_id, 1, account_id="PA000PAPER")

    assert second == first


async def test_verification_refuses_when_no_verifier_is_installed(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    """Package B ships the seam, not the broker call (contract §7)."""
    unwired = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        account_verifier=UnconfiguredAccountVerifier(),
    )
    try:
        created = paper_profile(unwired)

        with pytest.raises(AccountVerificationFailed):
            await unwired.verify_account(created.profile.profile_id, 1)
    finally:
        unwired.close()


def test_an_unknown_profile_or_revision_is_not_found(service: BrokerConfigurationService) -> None:
    with pytest.raises(ProfileNotFound):
        service.read_profile("profile_missing")

    created = paper_profile(service)
    with pytest.raises(RevisionNotFound):
        service.read_revision(created.profile.profile_id, 7)


def test_the_event_log_records_every_change_newest_first(
    service: BrokerConfigurationService,
) -> None:
    created = paper_profile(service)
    service.rename_owner(display_label="Desk operator")
    service.update_profile(created.profile.profile_id, display_name="Paper — renamed")

    events = service.events(limit=10)

    assert [event.action for event in events] == [
        "profile_renamed",
        "owner_renamed",
        "profile_created",
    ]
    assert all(event.actor_owner_id == service.owner().owner_id for event in events)


def test_the_event_log_pages_backwards(service: BrokerConfigurationService) -> None:
    created = paper_profile(service)
    service.update_profile(created.profile.profile_id, display_name="Second")
    service.update_profile(created.profile.profile_id, display_name="Third")

    newest = service.events(limit=1)
    older = service.events(limit=10, before_event_id=newest[0].event_id)

    assert len(newest) == 1
    assert len(older) == 2
    assert newest[0].event_id not in {event.event_id for event in older}

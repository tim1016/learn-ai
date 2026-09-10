"""Staging, the one-shot Apply, and the generation fence.

Staged, effective and sealed are three different things (ADR 0060 Decision 4).
These pin the first two: staging governs nothing, only an Apply moves a
revision toward effective, only the worker writes the effective fields, and a
refused Apply consumes its one-shot request rather than leaving it pending for
a later unattended restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker_configuration.errors import (
    ProfileArchived,
    RevisionIncomplete,
    RevisionNotFound,
    SelectionGenerationConflict,
)
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from tests.broker_configuration.conftest import OPERATOR_IDENTITY, FrozenClock, paper_profile


def _staged(service: BrokerConfigurationService) -> str:
    created = paper_profile(service)
    service.stage_selection(
        profile_id=created.profile.profile_id, revision=1, expected_selection_generation=0
    )
    return created.profile.profile_id


def test_a_fresh_installation_has_nothing_staged_and_nothing_effective(
    service: BrokerConfigurationService,
) -> None:
    selection = service.selection()

    assert selection.staged_profile_id is None
    assert selection.effective_profile_id is None
    assert selection.apply_requested is False
    assert selection.selection_generation == 0


def test_staging_records_the_exact_revision_and_advances_the_generation(
    service: BrokerConfigurationService,
) -> None:
    profile_id = _staged(service)

    selection = service.selection()

    assert selection.staged_profile_id == profile_id
    assert selection.staged_revision == 1
    assert selection.selection_generation == 1
    # Staging changed no runtime and made nothing effective.
    assert selection.effective_profile_id is None
    assert selection.apply_requested is False


def test_restaging_the_same_revision_is_a_no_op(service: BrokerConfigurationService) -> None:
    profile_id = _staged(service)

    repeated = service.stage_selection(
        profile_id=profile_id, revision=1, expected_selection_generation=1
    )

    assert repeated.selection_generation == 1


def test_staging_with_a_stale_generation_conflicts(service: BrokerConfigurationService) -> None:
    profile_id = _staged(service)

    with pytest.raises(SelectionGenerationConflict):
        service.stage_selection(
            profile_id=profile_id, revision=1, expected_selection_generation=0
        )


def test_two_tabs_cannot_silently_clobber_a_staged_selection(
    clerk_dir: Path, clock: FrozenClock
) -> None:
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
        first = paper_profile(first_tab, display_name="First")
        second = paper_profile(first_tab, display_name="Second")
        first_tab.stage_selection(
            profile_id=first.profile.profile_id, revision=1, expected_selection_generation=0
        )

        with pytest.raises(SelectionGenerationConflict):
            second_tab.stage_selection(
                profile_id=second.profile.profile_id, revision=1, expected_selection_generation=0
            )

        assert second_tab.selection().staged_profile_id == first.profile.profile_id
    finally:
        first_tab.close()
        second_tab.close()


def test_an_incomplete_revision_cannot_be_staged(service: BrokerConfigurationService) -> None:
    draft = service.create_profile(
        display_name="Live — draft",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=None,
    )

    with pytest.raises(RevisionIncomplete):
        service.stage_selection(
            profile_id=draft.profile.profile_id, revision=1, expected_selection_generation=0
        )


def test_an_archived_profile_cannot_be_staged(service: BrokerConfigurationService) -> None:
    created = paper_profile(service)
    service.update_profile(created.profile.profile_id, archived=True)

    with pytest.raises(ProfileArchived):
        service.stage_selection(
            profile_id=created.profile.profile_id, revision=1, expected_selection_generation=0
        )


def test_apply_records_intent_and_changes_no_effective_binding(
    service: BrokerConfigurationService,
) -> None:
    _staged(service)

    requested = service.request_apply(expected_selection_generation=1)

    assert requested.apply_requested is True
    assert requested.apply_requested_generation == 1
    assert requested.selection_generation == 2
    assert requested.effective_profile_id is None


def test_apply_without_a_staged_revision_is_refused(service: BrokerConfigurationService) -> None:
    with pytest.raises(RevisionNotFound):
        service.request_apply(expected_selection_generation=0)


def test_repeating_an_apply_against_the_recorded_generation_is_a_no_op(
    service: BrokerConfigurationService,
) -> None:
    _staged(service)
    requested = service.request_apply(expected_selection_generation=1)

    replay = service.request_apply(expected_selection_generation=1)
    resend = service.request_apply(expected_selection_generation=2)

    assert replay == requested
    assert resend == requested


def test_staging_a_different_revision_retires_a_pending_apply(
    service: BrokerConfigurationService,
) -> None:
    first = paper_profile(service, display_name="First")
    second = paper_profile(service, display_name="Second")
    service.stage_selection(
        profile_id=first.profile.profile_id, revision=1, expected_selection_generation=0
    )
    service.request_apply(expected_selection_generation=1)

    restaged = service.stage_selection(
        profile_id=second.profile.profile_id, revision=1, expected_selection_generation=2
    )

    assert restaged.apply_requested is False
    assert restaged.apply_requested_at_ms is None
    assert restaged.staged_profile_id == second.profile.profile_id


def test_the_worker_acknowledgement_is_the_only_thing_that_makes_a_revision_effective(
    service: BrokerConfigurationService,
) -> None:
    profile_id = _staged(service)
    service.request_apply(expected_selection_generation=1)

    acknowledged = service.acknowledge_effective(
        profile_id=profile_id,
        revision=1,
        account_id="PA000PAPER",
        expected_selection_generation=2,
    )

    assert acknowledged.effective_profile_id == profile_id
    assert acknowledged.effective_revision == 1
    assert acknowledged.effective_account_id == "PA000PAPER"
    assert acknowledged.last_apply_outcome == "applied"
    # The one-shot request is consumed, so a later restart does not re-apply it.
    assert acknowledged.apply_requested is False


def test_a_stale_worker_cannot_publish_itself_as_effective(
    service: BrokerConfigurationService,
) -> None:
    profile_id = _staged(service)
    service.request_apply(expected_selection_generation=1)

    with pytest.raises(SelectionGenerationConflict):
        service.acknowledge_effective(
            profile_id=profile_id,
            revision=1,
            account_id="PA000PAPER",
            expected_selection_generation=1,
        )
    assert service.selection().effective_profile_id is None


def test_a_refused_apply_consumes_the_request_and_keeps_the_last_effective_binding(
    service: BrokerConfigurationService,
) -> None:
    first = paper_profile(service, display_name="First")
    second = paper_profile(service, display_name="Second")
    service.stage_selection(
        profile_id=first.profile.profile_id, revision=1, expected_selection_generation=0
    )
    service.request_apply(expected_selection_generation=1)
    service.acknowledge_effective(
        profile_id=first.profile.profile_id,
        revision=1,
        account_id="PA000PAPER",
        expected_selection_generation=2,
    )
    service.stage_selection(
        profile_id=second.profile.profile_id, revision=1, expected_selection_generation=2
    )
    service.request_apply(expected_selection_generation=3)

    refused = service.record_apply_refusal(
        reason="apply_preflight_refused", expected_selection_generation=4
    )

    assert refused.apply_requested is False
    assert refused.last_apply_outcome == "refused"
    assert refused.last_apply_refusal_reason == "apply_preflight_refused"
    # The worker boots the last-effective revision, which is untouched.
    assert refused.effective_profile_id == first.profile.profile_id
    assert refused.staged_profile_id == second.profile.profile_id


def test_a_crash_with_a_staged_selection_still_reads_the_last_effective_revision(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    """A restart is close-and-reopen; the staged revision is still merely staged."""
    before_crash = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
    )
    effective = paper_profile(before_crash, display_name="Effective")
    staged = paper_profile(before_crash, display_name="Staged")
    before_crash.stage_selection(
        profile_id=effective.profile.profile_id, revision=1, expected_selection_generation=0
    )
    before_crash.request_apply(expected_selection_generation=1)
    before_crash.acknowledge_effective(
        profile_id=effective.profile.profile_id,
        revision=1,
        account_id="PA000PAPER",
        expected_selection_generation=2,
    )
    before_crash.stage_selection(
        profile_id=staged.profile.profile_id, revision=1, expected_selection_generation=2
    )
    before_crash.close()

    after_crash = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
    )
    try:
        selection = after_crash.selection()
    finally:
        after_crash.close()

    assert selection.effective_profile_id == effective.profile.profile_id
    assert selection.staged_profile_id == staged.profile.profile_id
    assert selection.apply_requested is False

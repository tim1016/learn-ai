"""The write-side guards that make a stale change conflict instead of landing.

The service checks its preconditions against a value it read; these pin the
guards in the ``WHERE`` clauses that hold when something changes between that
read and the write. Without them a second writer's change would silently
overwrite the first — the outcome contract §5 exists to prevent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import Context
from dataclasses import replace
from pathlib import Path

import pytest

from app.broker_configuration.errors import (
    AccountPinMismatch,
    ProfileArchived,
    RevisionConflict,
    SelectionGenerationConflict,
)
from app.broker_configuration.records import EndpointMode, ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from tests.broker_configuration.conftest import OPERATOR_IDENTITY, FrozenClock, paper_profile


def _service(clerk_dir: Path, clock: FrozenClock, **kwargs: object) -> BrokerConfigurationService:
    return BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


def _before_next_transaction(
    service: BrokerConfigurationService,
    monkeypatch: pytest.MonkeyPatch,
    rival_write: Callable[[], None],
) -> None:
    """Commit the other connection's write immediately before this one locks."""
    original = service._store.transaction

    @contextmanager
    def interleaved() -> Iterator[sqlite3.Connection]:
        monkeypatch.setattr(service._store, "transaction", original)
        rival_write()
        with original() as conn:
            yield conn

    monkeypatch.setattr(service._store, "transaction", interleaved)


def test_a_revision_preserves_a_concurrent_profile_rename(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        profile_id = paper_profile(service).profile.profile_id

        def rename() -> None:
            rival.update_profile(profile_id, display_name="Renamed in another tab")

        _before_next_transaction(service, monkeypatch, rename)
        service.create_revision(
            profile_id,
            expected_revision=1,
            credential_slot="different-slot",
            endpoint_mode="paper",
            live_envelope=None,
        )

        assert service.read_profile(profile_id).profile.display_name == "Renamed in another tab"
    finally:
        service.close()
        rival.close()


def test_a_revision_refuses_a_concurrently_archived_profile(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        profile_id = paper_profile(service).profile.profile_id

        def archive() -> None:
            rival.update_profile(profile_id, archived=True)

        _before_next_transaction(service, monkeypatch, archive)
        with pytest.raises(ProfileArchived):
            service.create_revision(
                profile_id,
                expected_revision=1,
                credential_slot="different-slot",
                endpoint_mode="paper",
                live_envelope=None,
            )

        assert service.read_profile(profile_id).profile.archived is True
        assert len(service.list_revisions(profile_id)) == 1
    finally:
        service.close()
        rival.close()


@pytest.mark.parametrize("first_patch", [{"archived": True}, {"display_name": "Another tab"}])
def test_metadata_patch_preserves_fields_changed_by_another_writer(
    clerk_dir: Path,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    first_patch: dict[str, str | bool],
) -> None:
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        profile_id = paper_profile(service).profile.profile_id

        def rival_patch() -> None:
            rival.update_profile(profile_id, **first_patch)

        _before_next_transaction(service, monkeypatch, rival_patch)
        if "archived" in first_patch:
            updated = service.update_profile(profile_id, display_name="Another tab")
        else:
            updated = service.update_profile(profile_id, archived=True)

        assert updated.archived is True
        assert updated.display_name == "Another tab"
        assert service.read_profile(profile_id).profile == updated
    finally:
        service.close()
        rival.close()


@pytest.mark.parametrize("operation", ["stage", "apply", "acknowledge", "refuse"])
@pytest.mark.parametrize("same_service", [False, True])
def test_worker_handover_excludes_other_selection_writers(
    clerk_dir: Path, clock: FrozenClock, operation: str, same_service: bool
) -> None:
    service = _service(clerk_dir, clock)
    rival = service if same_service else _service(clerk_dir, clock)
    try:
        first = paper_profile(service, display_name="First")
        second = paper_profile(service, display_name="Second")
        service.stage_selection(
            profile_id=first.profile.profile_id, revision=1, expected_selection_generation=0
        )
        before = service.selection()

        def mutate() -> None:
            expected = before.selection_generation
            if operation == "stage":
                rival.stage_selection(
                    profile_id=second.profile.profile_id, revision=1,
                    expected_selection_generation=expected,
                )
            elif operation == "apply":
                rival.request_apply(expected_selection_generation=expected)
            elif operation == "acknowledge":
                rival.acknowledge_effective(
                    profile_id=first.profile.profile_id, revision=1, account_id="PA-TEST",
                    expected_selection_generation=expected,
                )
            else:
                rival.record_apply_refusal(reason="rival", expected_selection_generation=expected)

        with service.selection_handover():
            with pytest.raises(SelectionGenerationConflict, match="handover"):
                Context().run(mutate)
            assert service.selection() == before
            service.request_apply(expected_selection_generation=before.selection_generation)
        assert service.selection().apply_requested
    finally:
        if rival is not service:
            rival.close()
        service.close()


def test_staging_refuses_a_profile_archived_before_its_write_transaction(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        profile_id = paper_profile(service).profile.profile_id

        def archive() -> None:
            rival.update_profile(profile_id, archived=True)

        _before_next_transaction(service, monkeypatch, archive)
        with pytest.raises(ProfileArchived):
            service.stage_selection(
                profile_id=profile_id, revision=1, expected_selection_generation=0
            )
        assert service.selection().staged_profile_id is None
    finally:
        service.close()
        rival.close()


def test_desk_state_keeps_one_snapshot_during_reselection_and_archive(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read cannot combine an old selection with a newly archived profile."""
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        first = paper_profile(service, display_name="First")
        second = paper_profile(service, display_name="Second")
        first_profile_id = first.profile.profile_id
        second_profile_id = second.profile.profile_id
        service.stage_selection(
            profile_id=first_profile_id,
            revision=1,
            expected_selection_generation=0,
        )
        original_list_profiles = service._store.list_profiles
        rival_wrote = False

        def list_profiles_after_rival_write(*, include_archived: bool):
            nonlocal rival_wrote
            if not rival_wrote:
                rival_wrote = True
                rival.stage_selection(
                    profile_id=second_profile_id,
                    revision=1,
                    expected_selection_generation=1,
                )
                rival.update_profile(first_profile_id, archived=True)
            return original_list_profiles(include_archived=include_archived)

        monkeypatch.setattr(service._store, "list_profiles", list_profiles_after_rival_write)

        state = service.desk_state()

        assert state.selection_generation == 1
        assert state.staged_choice is not None
        assert state.staged_choice.profile_id == first_profile_id
        assert service.selection().staged_profile_id == second_profile_id
        assert service.read_profile(first_profile_id).profile.archived is True
    finally:
        service.close()
        rival.close()


def test_write_selection_refuses_a_generation_that_moved(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    service = _service(clerk_dir, clock)
    try:
        created = paper_profile(service)
        service.stage_selection(
            profile_id=created.profile.profile_id, revision=1, expected_selection_generation=0
        )
        store = ProfilesStore.open(clerk_dir=clerk_dir)
        try:
            current = store.read_selection()
            stale = replace(current, selection_generation=current.selection_generation + 1)

            with store.transaction() as conn:
                accepted = store.write_selection(conn, stale, previous_generation=0)

            assert accepted is False
            assert store.read_selection() == current
        finally:
            store.close()
    finally:
        service.close()


def test_write_account_pin_binds_exactly_once(clerk_dir: Path, clock: FrozenClock) -> None:
    service = _service(clerk_dir, clock)
    try:
        created = paper_profile(service)
        profile_id = created.profile.profile_id
        store = ProfilesStore.open(clerk_dir=clerk_dir)
        try:
            with store.transaction() as conn:
                first = store.write_account_pin(
                    conn,
                    profile_id=profile_id,
                    revision=1,
                    account_id="PA000PAPER",
                    pinned_at_ms=clock(),
                )
            with store.transaction() as conn:
                second = store.write_account_pin(
                    conn,
                    profile_id=profile_id,
                    revision=1,
                    account_id="PA111OTHER",
                    pinned_at_ms=clock(),
                )

            assert first is True
            assert second is False
            assert store.read_revision(profile_id, 1).account_pin == "PA000PAPER"
        finally:
            store.close()
    finally:
        service.close()


def test_a_stage_that_lands_mid_call_makes_this_one_conflict(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interleaved case, forced — not two callers taking turns.

    A sequential second caller is caught by the service's own generation check.
    This commits a rival stage *between* this caller's read and its write, which
    is the window contract §8's obligation is actually about.
    """
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        mine = paper_profile(service, display_name="Mine")
        theirs = paper_profile(service, display_name="Theirs")
        original_read = service._store.read_selection

        def read_then_let_the_rival_win() -> object:
            current = original_read()
            monkeypatch.undo()
            rival.stage_selection(
                profile_id=theirs.profile.profile_id,
                revision=1,
                expected_selection_generation=current.selection_generation,
            )
            return current

        monkeypatch.setattr(service._store, "read_selection", read_then_let_the_rival_win)

        with pytest.raises(SelectionGenerationConflict):
            service.stage_selection(
                profile_id=mine.profile.profile_id, revision=1, expected_selection_generation=0
            )

        assert service.selection().staged_profile_id == theirs.profile.profile_id
        assert service.selection().selection_generation == 1
    finally:
        service.close()
        rival.close()


def test_a_revision_that_lands_mid_call_makes_this_one_conflict(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same window on ``create_revision``, which must not surface a 500.

    The hook runs just before the write transaction opens. Once it opens,
    ``BEGIN IMMEDIATE`` excludes the rival outright — that exclusion is why
    the revision and metadata lookups belong inside the transaction.
    """
    service = _service(clerk_dir, clock)
    rival = _service(clerk_dir, clock)
    try:
        created = paper_profile(service)
        profile_id = created.profile.profile_id
        def rival_revision() -> None:
            rival.create_revision(
                profile_id,
                expected_revision=1,
                credential_slot="from-the-rival",
                endpoint_mode="paper",
                live_envelope=None,
            )
        _before_next_transaction(service, monkeypatch, rival_revision)

        with pytest.raises(RevisionConflict):
            service.create_revision(
                profile_id,
                expected_revision=1,
                credential_slot="from-me",
                endpoint_mode="paper",
                live_envelope=None,
            )

        assert [r.credential_slot for r in service.list_revisions(profile_id)] == [
            "alpaca_paper_primary",
            "from-the-rival",
        ]
    finally:
        service.close()
        rival.close()


async def test_a_pin_recorded_during_verification_is_not_replaced(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    """The race the write-once ``WHERE`` clause exists for, driven end to end.

    A second writer binds the revision while this request is still verifying.
    The request must refuse rather than overwrite a recorded pin — "a wrong
    account never replaces a pin" (contract §3).
    """
    owner_service = _service(clerk_dir, clock)
    created = paper_profile(owner_service)
    profile_id = created.profile.profile_id
    rival = ProfilesStore.open(clerk_dir=clerk_dir)

    class PinsDuringVerification:
        """A second writer binds the revision while verification is in flight."""

        async def observe_accounts(
            self,
            *,
            credential_slot: str,
            endpoint_mode: EndpointMode,
            live_envelope: Mapping[str, object] | None = None,
        ) -> tuple[ObservedAccount, ...]:
            del credential_slot, endpoint_mode, live_envelope
            with rival.transaction() as conn:
                rival.write_account_pin(
                    conn,
                    profile_id=profile_id,
                    revision=1,
                    account_id="PA999RIVAL",
                    pinned_at_ms=clock(),
                )
            return (ObservedAccount(account_id="PA000PAPER", account_mode="paper"),)

    service = _service(clerk_dir, clock, account_verifier=PinsDuringVerification())
    try:
        with pytest.raises(AccountPinMismatch):
            await service.pin_account(profile_id, 1, account_id="PA000PAPER")

        assert service.read_revision(profile_id, 1).account_pin == "PA999RIVAL"
    finally:
        service.close()
        rival.close()
        owner_service.close()

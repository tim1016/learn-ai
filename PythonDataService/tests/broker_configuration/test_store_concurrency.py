"""The write-side guards that make a stale change conflict instead of landing.

The service checks its preconditions against a value it read; these pin the
guards in the ``WHERE`` clauses that hold when something changes between that
read and the write. Without them a second writer's change would silently
overwrite the first — the outcome contract §5 exists to prevent.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.broker_configuration.errors import AccountPinMismatch
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
            self, *, credential_slot: str, endpoint_mode: EndpointMode
        ) -> tuple[ObservedAccount, ...]:
            del credential_slot, endpoint_mode
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

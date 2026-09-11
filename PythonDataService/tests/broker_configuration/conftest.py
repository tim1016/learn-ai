"""Fixtures for the broker-configuration package.

Every fixture roots the profiles database in ``tmp_path``. Nothing here touches
a running container, a real Clerk volume, or a credential.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from app.broker_configuration.records import CredentialSlotStatus, EndpointMode, ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore

# One in-domain envelope every test that needs live values reuses, so a test
# asserting sha parity and one asserting storage round-trip describe the same
# six numbers.
#
# Every value is distinct, deliberately. The obvious fixture gives the two bps
# fields the same number and the two counts nearby ones, and a transposition of
# two columns between the record and the INSERT would then round-trip
# identically through every test while changing the sha in production.
LIVE_ENVELOPE_PAYLOAD: dict[str, float | int] = {
    "loss_fraction": 0.05,
    "loss_usd": 5_000.0,
    "shadow_sessions": 3,
    "arming_max_sessions": 20,
    "xh_entry_bps": 11.0,
    "xh_exit_bps": 17.5,
}

OPERATOR_IDENTITY = "test-operator"


class FrozenClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, now_ms: int = 1_757_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


class FakeSlotDirectory:
    def __init__(self, *slots: CredentialSlotStatus) -> None:
        self._slots = slots

    def list_slots(self) -> tuple[CredentialSlotStatus, ...]:
        return self._slots


# Every slot name the tests in this package save a revision against. Package D
# made the installed directory the allowlist a saved slot is checked against
# (``service._require_known_credential_slot``), so a slot missing from here is
# refused at save time — which is the point, but it means the fixtures must
# name the slots they use in one place rather than per file.
TEST_CREDENTIAL_SLOTS: tuple[CredentialSlotStatus, ...] = (
    CredentialSlotStatus(slot="alpaca_paper_primary", label="Paper — primary", available=True),
    CredentialSlotStatus(slot="alpaca_paper_secondary", label="Paper — secondary", available=True),
    CredentialSlotStatus(slot="alpaca_paper_tertiary", label="Paper — tertiary", available=True),
    CredentialSlotStatus(slot="alpaca_live_primary", label="Live — primary", available=True),
)


def slot_directory_for_tests() -> FakeSlotDirectory:
    """A directory listing every slot this package's tests use."""
    return FakeSlotDirectory(*TEST_CREDENTIAL_SLOTS)


class FakeAccountVerifier:
    """Records what it was asked and answers with pre-set observations."""

    def __init__(self, *accounts: ObservedAccount) -> None:
        self.accounts = list(accounts)
        self.calls: list[tuple[str, str]] = []
        # What the last call carried as the revision's envelope, so a test can
        # assert a live revision's six values actually reach the verifier —
        # without them, a live binding cannot be constructed at all.
        self.envelopes: list[Mapping[str, object] | None] = []

    async def observe_accounts(
        self,
        *,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: Mapping[str, object] | None = None,
    ) -> tuple[ObservedAccount, ...]:
        self.calls.append((credential_slot, endpoint_mode))
        self.envelopes.append(live_envelope)
        return tuple(self.accounts)


@pytest.fixture
def clerk_dir(tmp_path: Path) -> Path:
    root = tmp_path / "alpaca_clerk"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def verifier() -> FakeAccountVerifier:
    return FakeAccountVerifier(
        ObservedAccount(account_id="PA000PAPER", account_mode="paper", account_status="ACTIVE"),
        ObservedAccount(account_id="9LIVE0001", account_mode="live", account_status="ACTIVE"),
    )


@pytest.fixture
def service(
    clerk_dir: Path, clock: FrozenClock, verifier: FakeAccountVerifier
) -> Iterator[BrokerConfigurationService]:
    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=slot_directory_for_tests(),
        account_verifier=verifier,
    )
    yield built
    built.close()


def paper_profile(service: BrokerConfigurationService, *, display_name: str = "Paper — testing"):
    """One saved paper profile at revision 1, the shape most tests start from."""
    return service.create_profile(
        display_name=display_name,
        credential_slot="alpaca_paper_primary",
        endpoint_mode="paper",
        live_envelope=None,
    )

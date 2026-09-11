"""The Alpaca-backed implementations of package B's two seams.

These are the adapters package D built so an installation can actually list its
credential slots and observe its broker account, instead of the fail-closed
defaults package B ships. What is worth asserting here is the *boundary*: what
crosses it, what never does, and that a live revision can be verified at all.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.profile import (
    CREDENTIAL_SLOTS,
    AccountModeDisagreement,
    CredentialSlotUnavailable,
    CredentialSlotUnknown,
    RevisionIncomplete,
)
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker_configuration.alpaca_seams import (
    CREDENTIAL_SLOT_LABELS,
    AlpacaAccountVerifier,
    AlpacaCredentialSlotDirectory,
)
from app.broker_configuration.seams import AccountVerifier, CredentialSlotDirectory
from tests.broker.alpaca.profile.conftest import (
    COMPLETE_ENVELOPE,
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    EVERY_FIXTURE_SECRET,
    LIVE_SLOT_KEY,
    LIVE_SLOT_SECRET,
    make_environment,
)


@pytest.fixture
def both_slots_injected() -> AlpacaCredentialEnvironment:
    """Both allowlisted slots provisioned with distinct pairs.

    Restated here rather than imported: package C's fixture lives in its own
    conftest, which pytest does not share across test packages.
    """
    return make_environment(
        api_key_id=DEFAULT_SLOT_KEY,
        api_secret_key=DEFAULT_SLOT_SECRET,
        credential_live_key_id=LIVE_SLOT_KEY,
        credential_live_secret_key=LIVE_SLOT_SECRET,
    )


def _snapshot(
    *, account_id: str = "PA000PAPER", account_mode: str = "paper"
) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode=account_mode,
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1000.0,
        buying_power=1000.0,
        portfolio_value=1000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=None,
        observed_at_ms=1_757_000_000_000,
    )


class _Discovery:
    """One read method, and an assertion on anything else being reached for."""

    def __init__(self, result: BrokerAccountSnapshot | Exception) -> None:
        self._result = result
        self.calls = 0

    async def get_account(self) -> BrokerAccountSnapshot:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def __getattr__(self, name: str) -> object:  # pragma: no cover - must not run
        raise AssertionError(f"the verifier reached for {name!r} on the broker")


class _ExplodingEnvironment:
    """Any attribute read is a failed test: no slot lookup may reach here."""

    def __getattr__(self, name: str) -> object:  # pragma: no cover - must not run
        raise AssertionError(f"an unknown slot reached an environment lookup for {name!r}")


# ---- the adapters satisfy the protocols they were written for -------------


def test_the_adapters_satisfy_the_protocols_package_b_declared() -> None:
    assert isinstance(AlpacaCredentialSlotDirectory(), CredentialSlotDirectory)
    assert isinstance(AlpacaAccountVerifier(), AccountVerifier)


# ---- the slot directory ---------------------------------------------------


def test_every_allowlisted_slot_has_an_operator_facing_label() -> None:
    """The fallback in ``_label`` is a safety net, not a routine path."""
    assert set(CREDENTIAL_SLOTS) <= set(CREDENTIAL_SLOT_LABELS)


def test_the_directory_lists_every_allowlisted_slot_with_its_availability() -> None:
    directory = AlpacaCredentialSlotDirectory(
        environment=make_environment(api_key_id="dfltkey0-x", api_secret_key="dfltsec0-x")
    )

    listed = directory.list_slots()

    assert tuple(status.slot for status in listed) == CREDENTIAL_SLOTS
    availability = {status.slot: status.available for status in listed}
    assert availability == {"default": True, "live": False}


def test_an_unavailable_slot_is_still_listed_rather_than_hidden() -> None:
    """"This deployment has no live pair injected" is what the operator needs."""
    directory = AlpacaCredentialSlotDirectory(environment=make_environment())

    listed = directory.list_slots()

    assert tuple(status.slot for status in listed) == CREDENTIAL_SLOTS
    assert not any(status.available for status in listed)


def test_the_directory_never_reports_a_verified_account_it_did_not_observe(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    """Answering these would mean a broker call per slot from a listing route."""
    listed = AlpacaCredentialSlotDirectory(environment=both_slots_injected).list_slots()

    assert all(status.verified_account_id is None for status in listed)
    assert all(status.verified_at_ms is None for status in listed)


def test_no_secret_value_fragment_or_variable_name_reaches_the_listing(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    rendered = repr(AlpacaCredentialSlotDirectory(environment=both_slots_injected).list_slots())

    for secret in EVERY_FIXTURE_SECRET:
        assert secret not in rendered
        assert secret[:8] not in rendered
    assert "ALPACA_" not in rendered


# ---- the account verifier -------------------------------------------------


async def test_a_paper_revision_observes_its_account(both_slots_injected: object) -> None:
    discovery = _Discovery(_snapshot())
    verifier = AlpacaAccountVerifier(environment=both_slots_injected, discovery=discovery)

    observed = await verifier.observe_accounts(credential_slot="default", endpoint_mode="paper")

    assert [account.account_id for account in observed] == ["PA000PAPER"]
    assert observed[0].account_mode == "paper"
    assert observed[0].account_status == "ACTIVE"


async def test_a_live_revision_can_be_verified_when_it_carries_its_envelope(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    """The reason the protocol carries ``live_envelope`` at all.

    A live binding cannot be constructed without all six values, so a verifier
    handed only a slot and a mode could never observe a live revision's account.
    """
    discovery = _Discovery(_snapshot(account_id="9LIVE0001", account_mode="live"))
    verifier = AlpacaAccountVerifier(environment=both_slots_injected, discovery=discovery)

    observed = await verifier.observe_accounts(
        credential_slot="live",
        endpoint_mode="live",
        live_envelope=COMPLETE_ENVELOPE,
    )

    assert [account.account_id for account in observed] == ["9LIVE0001"]
    assert observed[0].account_mode == "live"


async def test_a_live_revision_without_its_envelope_is_refused_not_fabricated(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    """No placeholder envelope is invented to get past mode agreement."""
    discovery = _Discovery(_snapshot(account_id="9LIVE0001", account_mode="live"))
    verifier = AlpacaAccountVerifier(environment=both_slots_injected, discovery=discovery)

    with pytest.raises(RevisionIncomplete):
        await verifier.observe_accounts(credential_slot="live", endpoint_mode="live")

    assert discovery.calls == 0


async def test_an_observed_account_contradicting_the_mode_is_refused(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    from app.broker.contract.errors import BrokerAccountModeDisagreement

    discovery = _Discovery(
        BrokerAccountModeDisagreement("configured live, observed a paper account")
    )
    verifier = AlpacaAccountVerifier(environment=both_slots_injected, discovery=discovery)

    with pytest.raises(AccountModeDisagreement):
        await verifier.observe_accounts(
            credential_slot="live", endpoint_mode="live", live_envelope=COMPLETE_ENVELOPE
        )


async def test_a_slot_off_the_allowlist_never_reaches_an_environment_lookup() -> None:
    verifier = AlpacaAccountVerifier(environment=_ExplodingEnvironment())

    with pytest.raises(CredentialSlotUnknown):
        await verifier.observe_accounts(
            credential_slot="POLYGON_API_KEY", endpoint_mode="paper"
        )


async def test_an_allowlisted_slot_with_no_injected_pair_is_refused() -> None:
    verifier = AlpacaAccountVerifier(environment=make_environment())

    with pytest.raises(CredentialSlotUnavailable):
        await verifier.observe_accounts(credential_slot="live", endpoint_mode="paper")


async def test_each_observation_resolves_a_fresh_binding(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    """Nothing is cached between calls, so one revision cannot answer for another."""
    discovery = _Discovery(_snapshot())
    verifier = AlpacaAccountVerifier(environment=both_slots_injected, discovery=discovery)

    await verifier.observe_accounts(credential_slot="default", endpoint_mode="paper")
    await verifier.observe_accounts(credential_slot="live", endpoint_mode="paper")

    assert discovery.calls == 2

"""The credential slot allowlist and resolver (contract §3, ADR 0060 OQ2).

The properties under test are the ones that make a profile unable to reach a
secret it was not given: the allowlist is closed and code-owned, an unknown
name never becomes an environment lookup, and only a label plus a boolean ever
leaves the module.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.broker.alpaca.profile.credentials import (
    CREDENTIAL_SLOT_DEFAULT,
    CREDENTIAL_SLOT_LIVE,
    CREDENTIAL_SLOTS,
    AlpacaCredentialEnvironment,
    CredentialSlotAvailability,
    credential_slot_available,
    describe_credential_slots,
    is_known_credential_slot,
    resolve_credentials,
)
from app.broker.alpaca.profile.errors import (
    CredentialSlotUnavailable,
    CredentialSlotUnknown,
)
from tests.broker.alpaca.profile.conftest import (
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    LIVE_SLOT_KEY,
    LIVE_SLOT_SECRET,
    make_environment,
)


class _RefusingEnvironment:
    """An environment double that fails loudly if anything reads a field.

    Proves the allowlist gate runs *before* a lookup, which is what stops a
    profile-supplied string from selecting an arbitrary secret.
    """

    def __getattr__(self, name: str) -> object:  # pragma: no cover - must not run
        raise AssertionError(f"an unknown slot reached an environment lookup for {name!r}")


def test_the_allowlist_is_exactly_the_two_owner_decided_slots() -> None:
    assert CREDENTIAL_SLOTS == (CREDENTIAL_SLOT_DEFAULT, CREDENTIAL_SLOT_LIVE)
    assert (CREDENTIAL_SLOT_DEFAULT, CREDENTIAL_SLOT_LIVE) == ("default", "live")


def test_resolve_credentials_default_slot_returns_the_legacy_pair(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    resolved = resolve_credentials("default", environment=both_slots_injected)

    assert resolved.slot == "default"
    assert resolved.api_key_id.get_secret_value() == DEFAULT_SLOT_KEY
    assert resolved.api_secret_key.get_secret_value() == DEFAULT_SLOT_SECRET


def test_resolve_credentials_live_slot_returns_the_dedicated_pair(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    resolved = resolve_credentials("live", environment=both_slots_injected)

    assert resolved.slot == "live"
    assert resolved.api_key_id.get_secret_value() == LIVE_SLOT_KEY
    assert resolved.api_secret_key.get_secret_value() == LIVE_SLOT_SECRET


def test_two_slots_resolve_distinct_pairs_without_cross_contamination(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    default = resolve_credentials("default", environment=both_slots_injected)
    live = resolve_credentials("live", environment=both_slots_injected)

    assert {default.api_key_id.get_secret_value(), live.api_key_id.get_secret_value()} == {DEFAULT_SLOT_KEY, LIVE_SLOT_KEY}
    assert default.api_key_id.get_secret_value() != live.api_key_id.get_secret_value()
    assert default.api_secret_key.get_secret_value() != live.api_secret_key.get_secret_value()


def test_resolve_credentials_reads_the_slot_pair_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_CREDENTIAL_LIVE_KEY_ID", LIVE_SLOT_KEY)
    monkeypatch.setenv("ALPACA_CREDENTIAL_LIVE_SECRET_KEY", LIVE_SLOT_SECRET)

    # ``_env_file=None`` keeps a developer's real .env off this assertion.
    resolved = resolve_credentials(
        "live", environment=AlpacaCredentialEnvironment(_env_file=None)
    )

    assert resolved.api_key_id.get_secret_value() == LIVE_SLOT_KEY
    assert resolved.api_secret_key.get_secret_value() == LIVE_SLOT_SECRET


@pytest.mark.parametrize(
    "slot",
    [
        "POLYGON_API_KEY",
        "ALPACA_API_SECRET_KEY",
        "alpaca_paper_primary",
        "../../secrets",
        "DEFAULT",
        "",
        None,
        3,
    ],
)
def test_a_slot_off_the_allowlist_never_reaches_an_environment_lookup(slot: object) -> None:
    with pytest.raises(CredentialSlotUnknown) as info:
        resolve_credentials(slot, environment=_RefusingEnvironment())

    assert info.value.reason == "credential_slot_unknown"
    assert info.value.http_status == 422
    assert info.value.known_slots == CREDENTIAL_SLOTS


def test_an_unknown_slot_is_a_refusal_for_availability_too() -> None:
    with pytest.raises(CredentialSlotUnknown):
        credential_slot_available("POLYGON_API_KEY", environment=_RefusingEnvironment())


def test_is_known_credential_slot_reads_no_environment() -> None:
    assert is_known_credential_slot("default") is True
    assert is_known_credential_slot("live") is True
    assert is_known_credential_slot("POLYGON_API_KEY") is False
    assert is_known_credential_slot(None) is False


def test_an_allowlisted_slot_with_no_injected_pair_is_unavailable(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    with pytest.raises(CredentialSlotUnavailable) as info:
        resolve_credentials("live", environment=only_default_slot_injected)

    assert info.value.reason == "credential_slot_unavailable"
    assert info.value.http_status == 409
    assert info.value.slot == "live"


@pytest.mark.parametrize(
    ("key_id", "secret_key"),
    [
        (LIVE_SLOT_KEY, None),
        (None, LIVE_SLOT_SECRET),
        (LIVE_SLOT_KEY, ""),
        ("   ", LIVE_SLOT_SECRET),
    ],
    ids=["secret-missing", "key-missing", "secret-blank", "key-whitespace"],
)
def test_a_half_injected_slot_is_unavailable_not_half_resolved(
    key_id: str | None, secret_key: str | None
) -> None:
    environment = make_environment(
        credential_live_key_id=key_id, credential_live_secret_key=secret_key
    )

    assert credential_slot_available("live", environment=environment) is False
    with pytest.raises(CredentialSlotUnavailable):
        resolve_credentials("live", environment=environment)


def test_describe_credential_slots_lists_every_slot_with_its_availability(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    described = describe_credential_slots(environment=only_default_slot_injected)

    assert described == (
        CredentialSlotAvailability(slot="default", available=True),
        CredentialSlotAvailability(slot="live", available=False),
    )


def test_the_availability_shape_carries_a_label_and_a_boolean_and_nothing_else() -> None:
    fields = {field.name for field in dataclasses.fields(CredentialSlotAvailability)}

    assert fields == {"slot", "available"}

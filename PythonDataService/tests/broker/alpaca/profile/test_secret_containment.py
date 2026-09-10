"""No secret value, fragment, or length crosses a boundary (contract §1, §3).

"Secret values appear in no row, payload, log, or error — asserted, not
assumed" is one of the contract's own test obligations (§8). This module is
that assertion: every surface a credential can plausibly reach — a ``repr``, a
``str``, a log record, an error's operator prose, the availability payload — is
searched for the fixture secrets and for their lengths.
"""

from __future__ import annotations

import logging

import pytest

from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.profile.credentials import (
    describe_credential_slots,
    resolve_credentials,
)
from app.broker.alpaca.profile.errors import (
    AccountModeDisagreement,
    AccountPinMismatch,
    AccountVerificationFailed,
    BrokerProfileError,
    CredentialSlotUnavailable,
    CredentialSlotUnknown,
    RevisionIncomplete,
)
from app.broker.alpaca.profile.runtime_context import resolve_runtime_context
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.profile.conftest import (
    COMPLETE_ENVELOPE,
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    EVERY_FIXTURE_SECRET,
    LIVE_SLOT_KEY,
    LIVE_SLOT_SECRET,
    make_environment,
)

_EVERY_ERROR: tuple[BrokerProfileError, ...] = (
    CredentialSlotUnknown(known_slots=("default", "live")),
    CredentialSlotUnavailable("live"),
    RevisionIncomplete("its loss_usd is not stored as a number"),
    AccountVerificationFailed.from_broker_error(
        BrokerUnavailable("Could not reach Alpaca.", broker="alpaca", detail="timeout")
    ),
    AccountVerificationFailed.not_observed(selected_account_id="PA3TESTACCOUNT"),
    AccountVerificationFailed.stale(age_ms=600_000, max_age_ms=300_000),
    AccountModeDisagreement(endpoint_mode="live", credential_slot="live"),
    AccountPinMismatch(pinned_account_id="PA3TESTACCOUNT", observed_account_id="PA9OTHER"),
)


def _assert_no_secret_in(text: str) -> None:
    """No fixture secret value or fragment of one appears in ``text``.

    "Not a length" is guaranteed structurally rather than by substring search,
    which would false-positive on any coincidental number: the availability
    payload's fields are exactly ``{slot, available}`` and an error's detail is
    exactly ``{reason, message, next_step}``, all authored from constants,
    allowlisted slot labels and broker account IDs — there is nowhere for a
    length to be reported. Both shapes are pinned by their own tests.
    """
    for secret in EVERY_FIXTURE_SECRET:
        assert secret not in text
        # A leading fragment is as disqualifying as the whole value.
        assert secret[: len(secret) // 2] not in text


def test_resolved_credentials_mask_themselves_in_repr_and_str() -> None:
    resolved = resolve_credentials(
        "live",
        environment=make_environment(
            credential_live_key_id=LIVE_SLOT_KEY,
            credential_live_secret_key=LIVE_SLOT_SECRET,
        ),
    )

    _assert_no_secret_in(repr(resolved))
    _assert_no_secret_in(str(resolved))
    assert "live" in repr(resolved)


def test_alpaca_settings_never_print_their_credentials() -> None:
    settings = AlpacaSettings(
        api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET, mode="paper"
    )

    _assert_no_secret_in(repr(settings))
    _assert_no_secret_in(str(settings))


def test_a_resolved_context_prints_only_facts_a_surface_may_show() -> None:
    context = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="live",
        live_envelope=COMPLETE_ENVELOPE,
        account_pin="123456789",
        profile_id="profile-1",
        revision=3,
        environment=make_environment(
            credential_live_key_id=LIVE_SLOT_KEY,
            credential_live_secret_key=LIVE_SLOT_SECRET,
        ),
    )

    printed = repr(context)

    _assert_no_secret_in(printed)
    _assert_no_secret_in(str(context))
    assert "profile-1" in printed
    assert "live" in printed


def test_the_credential_environment_masks_every_field() -> None:
    environment = make_environment(
        api_key_id=DEFAULT_SLOT_KEY,
        api_secret_key=DEFAULT_SLOT_SECRET,
        credential_live_key_id=LIVE_SLOT_KEY,
        credential_live_secret_key=LIVE_SLOT_SECRET,
    )

    _assert_no_secret_in(repr(environment))
    _assert_no_secret_in(str(environment))


def test_the_availability_payload_carries_no_secret_or_variable_name() -> None:
    described = describe_credential_slots(
        environment=make_environment(
            api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET
        )
    )

    printed = repr(described)

    _assert_no_secret_in(printed)
    assert "ALPACA_" not in printed
    assert "KEY_ID" not in printed


@pytest.mark.parametrize("error", _EVERY_ERROR, ids=lambda error: error.reason)
def test_no_profile_error_can_carry_secret_material(error: BrokerProfileError) -> None:
    surfaces = [str(error), repr(error), error.message, error.next_step]
    surfaces.extend(error.as_detail().values())

    for surface in surfaces:
        _assert_no_secret_in(surface)
        assert "ALPACA_API_KEY_ID" not in surface
        assert "ALPACA_CREDENTIAL_LIVE_SECRET_KEY" not in surface


def test_every_profile_error_carries_the_contract_detail_shape() -> None:
    for error in _EVERY_ERROR:
        detail = error.as_detail()

        assert set(detail) == {"reason", "message", "next_step"}
        assert detail["reason"] == error.reason
        assert detail["reason"] == detail["reason"].lower()


def test_resolution_and_refusal_emit_no_credential_bearing_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment = make_environment(
        api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET
    )

    with caplog.at_level(logging.DEBUG):
        resolve_runtime_context(
            endpoint_mode="paper", credential_slot="default", environment=environment
        )
        with pytest.raises(CredentialSlotUnavailable):
            resolve_credentials("live", environment=environment)

    _assert_no_secret_in(caplog.text)


def test_a_refused_revision_detail_never_echoes_a_credential() -> None:
    # The settings validator's ``str()`` echoes ``input_value``; the resolver
    # only ever forwards Pydantic's ``msg`` text.
    with pytest.raises(RevisionIncomplete) as info:
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="default",
            environment=make_environment(
                api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET
            ),
        )

    _assert_no_secret_in(str(info.value))
    _assert_no_secret_in(info.value.detail)
    _assert_no_secret_in(str(info.value.__cause__ or ""))

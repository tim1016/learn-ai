"""Store → load → ``LiveEnvelopeValues.sha`` returns today's sha, bit for bit.

The obligation ADR 0060 Decision 6 states as a test, and the one way to
silently break every arming record already in an operator's ledger. Covers
every read path a stored envelope can travel: the store's own row mapping, the
service's profile/revision reads, and the HTTP response DTO.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import LiveArmingRecord
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import InvalidLiveEnvelope
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore, profiles_database_path
from app.schemas.broker_configuration import LiveEnvelopePayload
from tests.broker_configuration.conftest import LIVE_ENVELOPE_PAYLOAD

_ENVIRONMENT_SETTINGS = {
    "api_key_id": "not-a-real-key",
    "api_secret_key": "not-a-real-secret",
    "mode": "live",
    "live_loss_fraction": 0.05,
    "live_loss_usd": 5_000.0,
    "live_shadow_sessions": 1,
    "live_arming_max_sessions": 20,
    "live_xh_entry_bps": 10.0,
    "live_xh_exit_bps": 10.0,
}


def _settings_envelope() -> LiveEnvelopeValues:
    """The envelope exactly as ``AlpacaSettings`` builds it today."""
    return LiveEnvelopeValues.from_settings(AlpacaSettings(**_ENVIRONMENT_SETTINGS))


def _live_profile(service: BrokerConfigurationService) -> str:
    created = service.create_profile(
        display_name="Live — envelope parity",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )
    return created.profile.profile_id


def test_stored_envelope_sha_matches_settings_envelope_on_every_read_path(
    service: BrokerConfigurationService,
) -> None:
    expected = _settings_envelope()
    profile_id = _live_profile(service)

    from_detail = service.read_profile(profile_id).latest_revision
    from_revision = service.read_revision(profile_id, 1)
    from_history = service.list_revisions(profile_id)[0]

    assert from_detail is not None
    for read in (from_detail, from_revision, from_history):
        assert read.live_envelope is not None
        assert read.live_envelope.to_values() == expected
        assert read.live_envelope.sha == expected.sha


def test_stored_envelope_sha_survives_closing_and_reopening_the_database(
    service: BrokerConfigurationService, clerk_dir: Path
) -> None:
    expected = _settings_envelope()
    profile_id = _live_profile(service)
    service.close()

    reopened = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        stored = reopened.read_revision(profile_id, 1)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.live_envelope is not None
    assert stored.live_envelope.sha == expected.sha


def test_stored_envelope_preserves_float_and_int_types(
    service: BrokerConfigurationService,
) -> None:
    profile_id = _live_profile(service)

    stored = service.read_revision(profile_id, 1).live_envelope

    assert stored is not None
    for field in ("loss_fraction", "loss_usd", "xh_entry_bps", "xh_exit_bps"):
        assert type(getattr(stored, field)) is float, field
    for field in ("shadow_sessions", "arming_max_sessions"):
        assert type(getattr(stored, field)) is int, field


def test_historical_arming_seal_still_verifies_against_the_stored_envelope(
    service: BrokerConfigurationService,
) -> None:
    """A record sealed before the migration verifies over the reconstructed envelope."""
    sealed = LiveArmingRecord.create(
        live_account_id="9LIVE0001",
        strategy_instance_id="ema-shadow-1",
        seal_hash="a" * 64,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="c" * 64,
        envelope=_settings_envelope(),
        armed_at_ms=1_757_000_000_000,
        max_sessions=20,
    )
    profile_id = _live_profile(service)

    stored = service.read_revision(profile_id, 1).live_envelope
    reread = LiveArmingRecord.from_payload(asdict(sealed))

    assert stored is not None
    assert reread.record_sha256 == sealed.record_sha256
    assert reread.envelope_sha256 == stored.sha
    assert reread.envelope == stored.to_values()


def test_envelope_sha_distinguishes_an_integer_from_a_float_of_equal_value() -> None:
    """``5000`` and ``5000.0`` are different envelope documents (ADR 0060 D6)."""
    as_float = ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD)
    integer_valued = replace(as_float.to_values(), loss_usd=5_000)

    assert integer_valued.sha != as_float.sha
    # ...and the validated type never produces the integer-valued document.
    assert ValidatedLiveEnvelope.from_mapping({**LIVE_ENVELOPE_PAYLOAD, "loss_usd": 5_000}).sha == as_float.sha


def test_decimal_is_refused_before_it_can_reach_the_hash() -> None:
    with pytest.raises(InvalidLiveEnvelope):
        ValidatedLiveEnvelope.from_mapping({**LIVE_ENVELOPE_PAYLOAD, "loss_usd": Decimal("5000")})


@pytest.mark.parametrize("value", [1.0, True])
def test_a_session_count_that_is_not_exactly_an_int_is_refused(value: object) -> None:
    with pytest.raises(InvalidLiveEnvelope):
        ValidatedLiveEnvelope.from_mapping({**LIVE_ENVELOPE_PAYLOAD, "shadow_sessions": value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("loss_fraction", 0.0),
        ("loss_fraction", 1.0),
        ("loss_usd", 0.0),
        ("shadow_sessions", 0),
        ("arming_max_sessions", 0),
        ("xh_entry_bps", -1.0),
        ("xh_exit_bps", 10_000.0),
        ("loss_usd", float("inf")),
        ("loss_usd", float("nan")),
    ],
)
def test_out_of_domain_values_are_refused(field: str, value: object) -> None:
    with pytest.raises(InvalidLiveEnvelope):
        ValidatedLiveEnvelope.from_mapping({**LIVE_ENVELOPE_PAYLOAD, field: value})


def test_the_schema_stores_floats_as_real_and_counts_as_integer(clerk_dir: Path) -> None:
    """REAL for the four floats, INTEGER for the two counts; NUMERIC banned."""
    ProfilesStore.open(clerk_dir=clerk_dir).close()
    connection = sqlite3.connect(profiles_database_path(clerk_dir))
    try:
        columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(profile_revisions)")}
        declared_types = {
            row[2]
            for table in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            for row in connection.execute(f"PRAGMA table_info({table[0]})")
        }
    finally:
        connection.close()

    assert "NUMERIC" not in declared_types

    for column in ("live_loss_fraction", "live_loss_usd", "live_xh_entry_bps", "live_xh_exit_bps"):
        assert columns[column] == "REAL", column
    for column in ("live_shadow_sessions", "live_arming_max_sessions"):
        assert columns[column] == "INTEGER", column


def test_response_dto_carries_the_same_six_values(service: BrokerConfigurationService) -> None:
    profile_id = _live_profile(service)

    stored = service.read_revision(profile_id, 1).live_envelope
    payload = LiveEnvelopePayload.from_record(stored)

    assert payload is not None
    assert ValidatedLiveEnvelope.from_mapping(payload.model_dump()).sha == _settings_envelope().sha

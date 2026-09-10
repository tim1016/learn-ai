"""Fixtures for the profile-resolution tests.

Every credential string here is deliberately **not** key-shaped: no ``PK``/``AK``
prefix, no 20/40-character token, nothing that could be mistaken for a real
Alpaca credential in a log grep. Each also opens with its own eight-character
token, so a containment assertion can search for a *short* fragment — Pydantic
truncates an echoed input to a handful of characters, and a check that only
looked for long prefixes would miss exactly that leak.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment

DEFAULT_SLOT_KEY = "dfltkey0-fixture-not-a-real-credential"
DEFAULT_SLOT_SECRET = "dfltsec0-fixture-not-a-real-credential"
LIVE_SLOT_KEY = "livekey0-fixture-not-a-real-credential"
LIVE_SLOT_SECRET = "livesec0-fixture-not-a-real-credential"
ROTATED_LIVE_SLOT_SECRET = "livesec1-fixture-not-a-real-credential"

EVERY_FIXTURE_SECRET = (
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    LIVE_SLOT_KEY,
    LIVE_SLOT_SECRET,
    ROTATED_LIVE_SLOT_SECRET,
)

COMPLETE_ENVELOPE: dict[str, float | int] = {
    "loss_fraction": 0.02,
    "loss_usd": 500.0,
    "shadow_sessions": 5,
    "arming_max_sessions": 20,
    "xh_entry_bps": 10.0,
    "xh_exit_bps": 12.5,
}


def make_environment(**overrides: str | None) -> AlpacaCredentialEnvironment:
    """A credential environment with every field explicit.

    Explicit ``None`` beats the environment and any ``.env`` on disk, so an
    "absent slot" test cannot be turned green by a developer's real file.
    """
    fields: dict[str, str | None] = {
        "api_key_id": None,
        "api_secret_key": None,
        "credential_live_key_id": None,
        "credential_live_secret_key": None,
    }
    fields.update(overrides)
    return AlpacaCredentialEnvironment(**fields)


@pytest.fixture
def both_slots_injected() -> AlpacaCredentialEnvironment:
    """Both allowlisted slots provisioned with distinct pairs."""
    return make_environment(
        api_key_id=DEFAULT_SLOT_KEY,
        api_secret_key=DEFAULT_SLOT_SECRET,
        credential_live_key_id=LIVE_SLOT_KEY,
        credential_live_secret_key=LIVE_SLOT_SECRET,
    )


@pytest.fixture
def only_default_slot_injected() -> AlpacaCredentialEnvironment:
    """Today's deployment: the compatibility slot alone."""
    return make_environment(
        api_key_id=DEFAULT_SLOT_KEY,
        api_secret_key=DEFAULT_SLOT_SECRET,
    )

"""Secret values appear in no row, payload, log, or error — asserted, not assumed.

Contract §8's obligation, driven end to end: a full profile lifecycle over
HTTP with real-looking Alpaca credentials in the environment, then a search for
those exact values in the database file's raw bytes, in every response body,
and in everything the configuration module logged. A profile references an
opaque credential *slot*; nothing about a credential — value, fragment, length,
or environment-variable name — is stored or returned.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.runtime import reset_broker_configuration_service_for_testing
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore, profiles_database_path
from app.routers.broker_configuration import PREFIX
from app.routers.broker_configuration import (
    get_broker_configuration_service as service_dependency,
)
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    OPERATOR_IDENTITY,
    FakeAccountVerifier,
    FrozenClock,
    slot_directory_for_tests,
)

# Distinctive enough that a substring search cannot match by accident, and
# shaped like the real thing so a leak would look exactly like a real leak.
FAKE_KEY_ID = "PKZZTESTKEYIDZZ0001"
FAKE_SECRET = "sk-zzz-test-secret-zzz-9f3a1c7e"
CREDENTIAL_VARIABLE_NAMES = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")


@pytest.fixture
async def client(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    from app.main import app

    monkeypatch.setenv("ALPACA_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", FAKE_SECRET)
    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=slot_directory_for_tests(),
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id="9LIVE0001", account_mode="live", account_status="ACTIVE")
        ),
    )
    app.dependency_overrides[service_dependency] = lambda: built
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.pop(service_dependency, None)
    built.close()
    reset_broker_configuration_service_for_testing()


async def _full_lifecycle(client: AsyncClient) -> list[str]:
    """Drive every mutating route once; return every response body seen."""
    bodies: list[str] = []

    def _record(response) -> dict:  # type: ignore[no-untyped-def]
        bodies.append(response.text)
        return response.json()

    created = _record(
        await client.post(
            f"{PREFIX}/profiles",
            json={
                "display_name": "Live — secrets check",
                "credential_slot": "alpaca_live_primary",
                "endpoint_mode": "live",
                "live_envelope": LIVE_ENVELOPE_PAYLOAD,
            },
        )
    )
    profile_id = created["profile"]["profile_id"]
    _record(await client.get(f"{PREFIX}/owner"))
    _record(await client.patch(f"{PREFIX}/owner", json={"display_label": "Desk operator"}))
    _record(await client.get(f"{PREFIX}/credential-slots"))
    _record(await client.get(f"{PREFIX}/profiles"))
    _record(await client.get(f"{PREFIX}/profiles/{profile_id}"))
    _record(await client.post(f"{PREFIX}/profiles/{profile_id}/revisions/1/verify-account"))
    _record(
        await client.post(
            f"{PREFIX}/profiles/{profile_id}/revisions/1/account-pin",
            json={"account_id": "9LIVE0001"},
        )
    )
    _record(await client.get(f"{PREFIX}/profiles/{profile_id}/revisions"))
    _record(await client.put(f"{PREFIX}/account-nicknames/9LIVE0001", json={"nickname": "Real money"}))
    _record(
        await client.put(
            f"{PREFIX}/selection",
            json={"profile_id": profile_id, "revision": 1, "expected_selection_generation": 0},
        )
    )
    _record(await client.post(f"{PREFIX}/selection/apply", json={"expected_selection_generation": 1}))
    _record(await client.get(f"{PREFIX}/selection"))
    _record(await client.get(f"{PREFIX}/events"))
    # A refusal path too: an error message must not echo a credential either.
    _record(
        await client.post(
            f"{PREFIX}/profiles/{profile_id}/revisions",
            json={
                "expected_revision": 99,
                "credential_slot": "alpaca_live_primary",
                "endpoint_mode": "paper",
            },
        )
    )
    return bodies


async def test_no_secret_reaches_a_row_a_payload_or_a_log(
    client: AsyncClient, clerk_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        bodies = await _full_lifecycle(client)

    database_bytes = profiles_database_path(clerk_dir).read_bytes()
    wal = profiles_database_path(clerk_dir).with_suffix(".db-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    logged = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)

    for secret in (FAKE_KEY_ID, FAKE_SECRET):
        assert secret.encode("utf-8") not in database_bytes
        assert secret not in logged
        for body in bodies:
            assert secret not in body

    # Not even the *names* of the credential variables cross the API — a
    # profile names a slot, never a variable (contract §3).
    for variable in CREDENTIAL_VARIABLE_NAMES:
        for body in bodies:
            assert variable not in body


async def test_the_stored_revision_holds_only_the_opaque_slot(
    client: AsyncClient, clerk_dir: Path
) -> None:
    await _full_lifecycle(client)

    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        profile_id = store.list_profiles(include_archived=True)[0].profile_id
        revision = store.read_revision(profile_id, 1)
    finally:
        store.close()

    assert revision is not None
    assert revision.credential_slot == "alpaca_live_primary"
    assert not any(
        field.startswith("api_") or "secret" in field or "key" in field
        for field in vars(revision)
        if field != "content_sha256"
    )

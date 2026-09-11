"""The HTTP surface at ``/api/brokers/alpaca/configuration``.

Contract shape, not business rules — those are pinned against the service in
``test_profiles_service.py`` and ``test_selection_service.py``. What these
assert is that the routes exist at the contract's paths and statuses, that a
typed refusal arrives with its code-like ``reason``, that a body naming an
identity the server resolves is refused rather than ignored, and that the
control secret gates every route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.runtime import reset_broker_configuration_service_for_testing
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from app.routers.broker_configuration import PREFIX
from app.routers.broker_configuration import (
    get_broker_configuration_service as service_dependency,
)
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    OPERATOR_IDENTITY,
    TEST_CREDENTIAL_SLOTS,
    FakeAccountVerifier,
    FrozenClock,
    slot_directory_for_tests,
)

PAPER_BODY = {"credential_slot": "alpaca_paper_primary", "endpoint_mode": "paper"}


@pytest.fixture
async def client(clerk_dir: Path, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    from app.main import app

    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=slot_directory_for_tests(),
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id="PA000PAPER", account_mode="paper", account_status="ACTIVE")
        ),
    )
    app.dependency_overrides[service_dependency] = lambda: built
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.pop(service_dependency, None)
    built.close()
    reset_broker_configuration_service_for_testing()


async def _create_paper_profile(client: AsyncClient, name: str = "Paper — testing") -> str:
    response = await client.post(f"{PREFIX}/profiles", json={**PAPER_BODY, "display_name": name})
    assert response.status_code == 201, response.text
    return response.json()["profile"]["profile_id"]


async def test_owner_is_readable_and_renameable(client: AsyncClient) -> None:
    read = await client.get(f"{PREFIX}/owner")
    assert read.status_code == 200
    assert read.json()["display_label"] == OPERATOR_IDENTITY

    renamed = await client.patch(f"{PREFIX}/owner", json={"display_label": "Desk operator"})

    assert renamed.status_code == 200
    assert renamed.json()["owner_id"] == read.json()["owner_id"]
    assert renamed.json()["display_label"] == "Desk operator"


async def test_credential_slots_report_labels_and_availability_only(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/credential-slots")

    assert response.status_code == 200
    assert response.json()["slots"] == [
        {
            "slot": slot.slot,
            "label": slot.label,
            "available": slot.available,
            "verified_account_id": None,
            "verified_at_ms": None,
        }
        for slot in TEST_CREDENTIAL_SLOTS
    ]


async def test_desk_state_reports_no_active_account_through_one_read_model(
    client: AsyncClient,
) -> None:
    response = await client.get(f"{PREFIX}/desk-state")

    assert response.status_code == 200
    assert response.json() == {
        "activation_state": "no_selection",
        "headline": "No Alpaca account is active for this installation",
        "detail": "Choose a verified account configuration for this worker.",
        "lifecycle": [
            {
                "key": "effective_configuration",
                "label": "Effective configuration",
                "status": "current",
            },
            {
                "key": "selected_configuration",
                "label": "Selected configuration",
                "status": "pending",
            },
            {"key": "worker_handoff", "label": "Worker handoff", "status": "pending"},
        ],
        "selection_label": "Choose an account configuration",
        "consequence": (
            "Choosing here only opens the saved configuration for review. Stage and Apply "
            "remain explicit actions on the Configuration page."
        ),
        "action": {
            "kind": "review_configuration",
            "label": "Set up an account",
            "enabled": True,
        },
        "selection_generation": 0,
        "staged_choice": None,
        "effective_choice": None,
        "choices": [],
        "empty_choices_message": "No account configurations are saved yet. Set one up to continue.",
        "profiles_requiring_setup": 0,
        "setup_required_message": None,
    }


async def test_desk_state_returns_verified_choices_without_configuration_secrets(
    client: AsyncClient,
) -> None:
    profile_id = await _create_paper_profile(client, name="Alpaca-Paper")
    pinned = await client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions/1/account-pin",
        json={"account_id": "PA000PAPER"},
    )
    assert pinned.status_code == 200, pinned.text

    response = await client.get(f"{PREFIX}/desk-state")

    assert response.status_code == 200
    body = response.json()
    assert body["selection_generation"] == 0
    assert body["choices"] == [
        {
            "selection_id": f"{profile_id}@1",
            "profile_id": profile_id,
            "revision": 1,
            "profile_label": "Alpaca-Paper",
            "account_id": "PA000PAPER",
            "nickname": None,
            "account_label": "Alpaca-Paper",
            "endpoint_mode": "paper",
            "badge_label": "Paper account",
            "description": "Alpaca-Paper uses the verified paper account.",
            "action_kind": "review_configuration",
            "action_label": "Review Alpaca-Paper",
            "is_staged": False,
            "is_effective": False,
        }
    ]
    assert "credential_slot" not in response.text
    assert "api_secret" not in response.text.lower()


async def test_profile_lifecycle_over_http(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)

    listed = await client.get(f"{PREFIX}/profiles")
    detail = await client.get(f"{PREFIX}/profiles/{profile_id}")
    renamed = await client.patch(
        f"{PREFIX}/profiles/{profile_id}", json={"display_name": "Paper — renamed"}
    )
    cloned = await client.post(
        f"{PREFIX}/profiles/{profile_id}/clone", json={"display_name": "Paper — copy"}
    )

    assert [profile["profile_id"] for profile in listed.json()["profiles"]] == [profile_id]
    assert detail.json()["latest_revision"]["revision"] == 1
    assert renamed.json()["display_name"] == "Paper — renamed"
    assert cloned.status_code == 201
    assert cloned.json()["latest_revision"]["account_pin"] is None


async def test_archived_profiles_are_hidden_unless_requested(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)
    await client.patch(f"{PREFIX}/profiles/{profile_id}", json={"archived": True})

    default = await client.get(f"{PREFIX}/profiles")
    included = await client.get(f"{PREFIX}/profiles", params={"include_archived": True})

    assert default.json()["profiles"] == []
    assert len(included.json()["profiles"]) == 1


async def test_a_live_revision_round_trips_its_envelope(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)

    created = await client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions",
        json={
            "expected_revision": 1,
            "credential_slot": "alpaca_live_primary",
            "endpoint_mode": "live",
            "live_envelope": LIVE_ENVELOPE_PAYLOAD,
        },
    )

    assert created.status_code == 201
    assert created.json()["live_envelope"] == LIVE_ENVELOPE_PAYLOAD
    history = await client.get(f"{PREFIX}/profiles/{profile_id}/revisions")
    assert [revision["revision"] for revision in history.json()["revisions"]] == [1, 2]


async def test_a_stale_revision_edit_returns_a_conflict_with_its_reason(
    client: AsyncClient,
) -> None:
    profile_id = await _create_paper_profile(client)
    await client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions",
        json={"expected_revision": 1, **PAPER_BODY, "credential_slot": "alpaca_paper_secondary"},
    )

    stale = await client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions",
        json={"expected_revision": 1, **PAPER_BODY, "credential_slot": "alpaca_paper_tertiary"},
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "revision_conflict"
    assert stale.json()["detail"]["message"]
    assert stale.json()["detail"]["next_step"]


async def test_an_unknown_profile_is_a_404_with_its_reason(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/profiles/profile_missing")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "profile_not_found"


async def test_verify_and_pin_an_observed_account(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)

    observed = await client.post(f"{PREFIX}/profiles/{profile_id}/revisions/1/verify-account")
    pinned = await client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions/1/account-pin",
        json={"account_id": "PA000PAPER"},
    )

    assert observed.status_code == 200
    assert observed.json()["observed_accounts"][0]["account_id"] == "PA000PAPER"
    assert pinned.status_code == 200
    assert pinned.json()["account_pin"] == "PA000PAPER"


async def test_pinning_an_account_nobody_observed_is_refused(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)

    response = await client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions/1/account-pin",
        json={"account_id": "TYPED-BY-HAND"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "account_pin_mismatch"


class _RefusingVerifier:
    """A verifier that refuses in package C's vocabulary, as the real one does.

    ``AlpacaAccountVerifier`` (``app/broker_configuration/alpaca_seams.py``)
    raises ``BrokerProfileError`` subclasses — the same ``reason`` strings as
    ``BrokerConfigurationError`` but a different base class, and only the two
    account ceremonies reach that family. This double is the smallest thing
    that reproduces it without a broker.
    """

    async def observe_accounts(
        self, *, credential_slot: str, endpoint_mode: str, live_envelope: object = None
    ) -> tuple[ObservedAccount, ...]:
        from app.broker.alpaca.profile import CredentialSlotUnavailable

        del endpoint_mode, live_envelope
        raise CredentialSlotUnavailable(credential_slot)


@pytest.fixture
async def refusing_client(clerk_dir: Path, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    from app.main import app

    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=slot_directory_for_tests(),
        account_verifier=_RefusingVerifier(),
    )
    app.dependency_overrides[service_dependency] = lambda: built
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.pop(service_dependency, None)
    built.close()
    reset_broker_configuration_service_for_testing()


@pytest.mark.parametrize(
    ("suffix", "body"),
    [("verify-account", None), ("account-pin", {"account_id": "PA000PAPER"})],
)
async def test_a_verifier_refusal_answers_in_the_contract_shape(
    refusing_client: AsyncClient, suffix: str, body: dict | None
) -> None:
    """A credential refusal is a typed 409, never the catch-all 500.

    ``BrokerProfileError`` already carries the contract §6 ``{reason, message,
    next_step}`` payload and its own ``http_status``, but nothing translated
    it: it is not a ``BrokerConfigurationError``, so it reached the ``Exception``
    handler and the desk saw a generic fault on exactly the two routes that
    exist to say which credential is missing.
    """
    profile_id = await _create_paper_profile(refusing_client)

    response = await refusing_client.post(
        f"{PREFIX}/profiles/{profile_id}/revisions/1/{suffix}", json=body
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "credential_slot_unavailable"
    assert detail["message"]
    assert detail["next_step"]


async def test_a_refusal_family_member_without_its_contract_row_is_not_answered_by_the_handler(
    refusing_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler must not itself raise when a subclass omits its vocabulary.

    ``reason`` and ``http_status`` are ``ClassVar``s with no default, so the base
    class is constructible without them. Reading them unguarded would make the
    handler raise ``AttributeError`` and answer with a broken response instead
    of the refusal it exists to give — so an incomplete member falls through to
    the catch-all, which is a plain 500 and not a malformed 200.
    """
    from app.broker.alpaca.profile import BrokerProfileError

    class _Incomplete(BrokerProfileError):
        """A subclass that forgot its contract row."""

    async def _refuse(_self: object, **_: object) -> tuple[ObservedAccount, ...]:
        raise _Incomplete("no vocabulary", next_step="none")

    monkeypatch.setattr(_RefusingVerifier, "observe_accounts", _refuse)
    profile_id = await _create_paper_profile(refusing_client)

    # The original exception propagates — never an ``AttributeError`` raised by
    # the handler itself. The ASGI transport re-raises what the handlers did not
    # answer; in the app it reaches the catch-all, which is an honest 500.
    with pytest.raises(_Incomplete):
        await refusing_client.post(f"{PREFIX}/profiles/{profile_id}/revisions/1/verify-account")


async def test_account_nicknames_are_keyed_to_the_account(client: AsyncClient) -> None:
    put = await client.put(
        f"{PREFIX}/account-nicknames/PA000PAPER", json={"nickname": "Testing account"}
    )
    listed = await client.get(f"{PREFIX}/account-nicknames")

    assert put.status_code == 200
    assert listed.json()["nicknames"] == [
        {"account_id": "PA000PAPER", "nickname": "Testing account", "updated_at_ms": put.json()["updated_at_ms"]}
    ]


async def test_selection_reports_staged_and_effective_and_apply_returns_202(
    client: AsyncClient,
) -> None:
    profile_id = await _create_paper_profile(client)

    staged = await client.put(
        f"{PREFIX}/selection",
        json={"profile_id": profile_id, "revision": 1, "expected_selection_generation": 0},
    )
    applied = await client.post(
        f"{PREFIX}/selection/apply", json={"expected_selection_generation": 1}
    )
    read = await client.get(f"{PREFIX}/selection")

    assert staged.status_code == 200
    assert applied.status_code == 202
    body = read.json()
    assert body["staged_profile_id"] == profile_id
    assert body["apply_requested"] is True
    # Apply changed no runtime: nothing is effective until the worker says so.
    assert body["effective_profile_id"] is None
    assert body["effective_acknowledged_at_ms"] is None


async def test_a_stale_staging_generation_returns_a_conflict(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)
    await client.put(
        f"{PREFIX}/selection",
        json={"profile_id": profile_id, "revision": 1, "expected_selection_generation": 0},
    )

    stale = await client.put(
        f"{PREFIX}/selection",
        json={"profile_id": profile_id, "revision": 1, "expected_selection_generation": 0},
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "selection_generation_conflict"


async def test_no_route_writes_the_effective_fields(client: AsyncClient) -> None:
    """Only the worker writes them, so no request body may carry them."""
    profile_id = await _create_paper_profile(client)

    response = await client.put(
        f"{PREFIX}/selection",
        json={
            "profile_id": profile_id,
            "revision": 1,
            "expected_selection_generation": 0,
            "effective_profile_id": profile_id,
        },
    )

    assert response.status_code == 422
    assert (await client.get(f"{PREFIX}/selection")).json()["effective_profile_id"] is None


@pytest.mark.parametrize("field", ["owner_id", "actor", "user_id"])
async def test_a_body_claiming_a_server_resolved_identity_is_refused(
    client: AsyncClient, field: str
) -> None:
    response = await client.post(
        f"{PREFIX}/profiles",
        json={**PAPER_BODY, "display_name": "Paper — forged", field: "someone-else"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "owner_field_not_accepted"
    assert field in response.json()["detail"]["message"]
    assert (await client.get(f"{PREFIX}/profiles")).json()["profiles"] == []


async def test_the_events_route_pages_the_audit_log(client: AsyncClient) -> None:
    profile_id = await _create_paper_profile(client)
    await client.patch(f"{PREFIX}/profiles/{profile_id}", json={"display_name": "Renamed"})

    newest = await client.get(f"{PREFIX}/events", params={"limit": 1})
    older = await client.get(
        f"{PREFIX}/events", params={"before_event_id": newest.json()["events"][0]["event_id"]}
    )

    assert [event["action"] for event in newest.json()["events"]] == ["profile_renamed"]
    assert [event["action"] for event in older.json()["events"]] == ["profile_created"]


async def test_an_unreadable_database_is_a_503_not_a_crash(
    clerk_dir: Path, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0060 Decision 7: fail closed with a reason, never crash the boot."""
    import app.broker_configuration.runtime as broker_configuration_runtime
    from app.main import app

    app.dependency_overrides.pop(service_dependency, None)
    monkeypatch.setattr(
        broker_configuration_runtime, "resolve_clerk_dir", lambda: clerk_dir / "corrupt"
    )
    reset_broker_configuration_service_for_testing()
    corrupt = clerk_dir / "corrupt" / "broker_configuration" / "profiles.db"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"this is not a SQLite database")

    response = await client.get(f"{PREFIX}/profiles")

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "profiles_database_unavailable"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/owner", None),
        ("GET", "/profiles", None),
        ("GET", "/selection", None),
        ("GET", "/events", None),
        ("PATCH", "/owner", {"display_label": "x"}),
        ("POST", "/profiles", {**PAPER_BODY, "display_name": "x"}),
        ("POST", "/selection/apply", {"expected_selection_generation": 0}),
    ],
)
async def test_every_route_requires_the_control_secret(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict | None
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "a-configured-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    response = await client.request(method, f"{PREFIX}{path}", json=body)

    assert response.status_code == 403, f"{method} {path} answered {response.status_code}"

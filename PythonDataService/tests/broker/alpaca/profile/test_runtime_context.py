"""Resolving a revision into an immutable runtime binding.

The load-bearing property is ADR 0060 Decision 6: a revision's stored values
and the same values read from the environment must produce a **bit-identical**
``LiveEnvelopeValues.sha``, because every arming record already in an
operator's ledger is sealed over that exact encoding.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker.alpaca.profile.errors import (
    CredentialSlotUnavailable,
    CredentialSlotUnknown,
    RevisionIncomplete,
)
from app.broker.alpaca.profile.runtime_context import (
    LIVE_ENVELOPE_FIELDS,
    resolve_runtime_context,
)
from tests.broker.alpaca.profile.conftest import (
    COMPLETE_ENVELOPE,
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    LIVE_SLOT_KEY,
    make_environment,
)

_LIVE_SETTINGS_KWARGS = {
    "live_loss_fraction": 0.02,
    "live_loss_usd": 500.0,
    "live_shadow_sessions": 5,
    "live_arming_max_sessions": 20,
    "live_xh_entry_bps": 10.0,
    "live_xh_exit_bps": 12.5,
}


def test_a_paper_revision_resolves_without_an_envelope(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    context = resolve_runtime_context(
        endpoint_mode="paper",
        credential_slot="default",
        environment=only_default_slot_injected,
        profile_id="profile-1",
        revision=1,
    )

    assert context.mode == "paper"
    assert context.is_paper is True
    assert context.base_url == "https://paper-api.alpaca.markets"
    assert context.live_envelope is None
    assert context.credential_slot == "default"
    assert context.settings.api_key_id == DEFAULT_SLOT_KEY
    assert context.settings.api_secret_key == DEFAULT_SLOT_SECRET


def test_a_live_revision_resolves_its_envelope_and_live_endpoint(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    context = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="live",
        live_envelope=COMPLETE_ENVELOPE,
        environment=both_slots_injected,
    )

    assert context.is_live is True
    assert context.base_url == "https://api.alpaca.markets"
    assert context.live_envelope == LiveEnvelopeValues(
        loss_fraction=0.02,
        loss_usd=500.0,
        shadow_sessions=5,
        arming_max_sessions=20,
        xh_entry_bps=10.0,
        xh_exit_bps=12.5,
    )


def test_a_live_revision_without_an_envelope_is_incomplete_not_a_crash(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    with pytest.raises(RevisionIncomplete) as info:
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="live",
            environment=both_slots_injected,
        )

    assert info.value.reason == "revision_incomplete"
    assert info.value.http_status == 422
    assert "ALPACA_MODE=live requires" in info.value.detail


def test_stored_values_and_environment_values_seal_to_the_same_sha(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    from_environment = LiveEnvelopeValues.from_settings(
        AlpacaSettings(
            api_key_id="k", api_secret_key="s", mode="live", **_LIVE_SETTINGS_KWARGS
        )
    )

    context = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="default",
        live_envelope=COMPLETE_ENVELOPE,
        environment=only_default_slot_injected,
    )

    assert context.live_envelope is not None
    assert context.live_envelope.sha == from_environment.sha


def test_an_integer_stored_for_a_float_field_round_trips_as_a_float(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    # SQLite type affinity can hand back ``500`` where ``500.0`` was written,
    # and ``5000`` hashes differently from ``5000.0``. The load path converts.
    stored = {**COMPLETE_ENVELOPE, "loss_usd": 500}

    context = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="default",
        live_envelope=stored,
        environment=only_default_slot_injected,
    )

    assert context.live_envelope is not None
    assert type(context.live_envelope.loss_usd) is float
    assert context.live_envelope.sha == LiveEnvelopeValues.from_settings(
        AlpacaSettings(
            api_key_id="k", api_secret_key="s", mode="live", **_LIVE_SETTINGS_KWARGS
        )
    ).sha


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shadow_sessions", True),
        ("arming_max_sessions", 20.0),
        ("shadow_sessions", "5"),
        ("loss_usd", "500"),
        ("loss_fraction", True),
        ("xh_entry_bps", None),
    ],
    ids=["bool-count", "float-count", "str-count", "str-float", "bool-float", "none-float"],
)
def test_a_stored_value_of_the_wrong_python_type_is_refused(
    field: str,
    value: object,
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    # Pydantic's lax mode would coerce every one of these; the contract's
    # type-fidelity rule (§2.4) means the sha must never be taken over a
    # coerced stand-in for what was stored.
    stored = {**COMPLETE_ENVELOPE, field: value}

    with pytest.raises(RevisionIncomplete, match=field):
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="default",
            live_envelope=stored,
            environment=only_default_slot_injected,
        )


def test_a_missing_envelope_field_names_what_is_missing(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    stored = {key: value for key, value in COMPLETE_ENVELOPE.items() if key != "loss_usd"}

    with pytest.raises(RevisionIncomplete, match="loss_usd"):
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="default",
            live_envelope=stored,
            environment=only_default_slot_injected,
        )


def test_an_envelope_carrying_a_field_that_is_not_an_envelope_field_is_refused(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    stored = {**COMPLETE_ENVELOPE, "base_url": "https://example.invalid"}

    with pytest.raises(RevisionIncomplete, match="base_url"):
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="default",
            live_envelope=stored,
            environment=only_default_slot_injected,
        )


def test_an_out_of_domain_envelope_value_is_refused_without_echoing_input(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    stored = {**COMPLETE_ENVELOPE, "loss_fraction": 1.5}

    with pytest.raises(RevisionIncomplete) as info:
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="default",
            live_envelope=stored,
            environment=only_default_slot_injected,
        )

    assert "less than 1" in info.value.detail
    assert DEFAULT_SLOT_SECRET not in str(info.value)


def test_the_envelope_field_names_are_the_dataclass_field_names() -> None:
    assert set(LIVE_ENVELOPE_FIELDS) == set(LiveEnvelopeValues.__dataclass_fields__)


def test_two_revisions_bind_distinct_credentials_without_cross_contamination(
    both_slots_injected: AlpacaCredentialEnvironment,
) -> None:
    paper = resolve_runtime_context(
        endpoint_mode="paper",
        credential_slot="default",
        environment=both_slots_injected,
        profile_id="paper-profile",
    )
    live = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="live",
        live_envelope=COMPLETE_ENVELOPE,
        environment=both_slots_injected,
        profile_id="live-profile",
    )

    assert paper.settings.api_key_id == DEFAULT_SLOT_KEY
    assert live.settings.api_key_id == LIVE_SLOT_KEY
    assert paper.settings.api_secret_key != live.settings.api_secret_key
    assert paper.base_url != live.base_url
    assert paper.live_envelope is None
    assert live.live_envelope is not None


def test_a_stale_live_value_in_the_environment_cannot_reach_a_paper_revision(
    monkeypatch: pytest.MonkeyPatch,
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    # After cutover there is no fallback to a stale user setting in the
    # environment (ADR 0060 Decision 7). Every live field is passed explicitly.
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "999999")
    monkeypatch.setenv("ALPACA_LIVE_SHADOW_SESSIONS", "1")

    context = resolve_runtime_context(
        endpoint_mode="paper",
        credential_slot="default",
        environment=only_default_slot_injected,
    )

    assert context.settings.live_loss_usd is None
    assert context.settings.live_shadow_sessions is None
    assert context.live_envelope is None


def test_the_clerk_directory_stays_a_deployment_bootstrap_environment_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path / "clerk"))

    context = resolve_runtime_context(
        endpoint_mode="paper",
        credential_slot="default",
        environment=only_default_slot_injected,
    )

    assert context.settings.clerk_dir == tmp_path / "clerk"


def test_a_revision_cannot_supply_an_endpoint_url_or_a_clerk_directory() -> None:
    parameters = set(inspect.signature(resolve_runtime_context).parameters)

    assert "base_url" not in parameters
    assert "clerk_dir" not in parameters
    assert parameters == {
        "endpoint_mode",
        "credential_slot",
        "live_envelope",
        "account_pin",
        "profile_id",
        "revision",
        "environment",
    }


def test_an_endpoint_mode_that_is_neither_paper_nor_live_is_refused(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    with pytest.raises(RevisionIncomplete, match="endpoint mode"):
        resolve_runtime_context(
            endpoint_mode="shadow",  # type: ignore[arg-type]
            credential_slot="default",
            environment=only_default_slot_injected,
        )


def test_an_unknown_slot_is_refused_before_any_revision_value_is_read() -> None:
    with pytest.raises(CredentialSlotUnknown):
        resolve_runtime_context(
            endpoint_mode="paper",
            credential_slot="POLYGON_API_KEY",
            environment=make_environment(),
        )


def test_a_revision_naming_an_uninjected_slot_refuses_explicitly(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    with pytest.raises(CredentialSlotUnavailable):
        resolve_runtime_context(
            endpoint_mode="live",
            credential_slot="live",
            live_envelope=COMPLETE_ENVELOPE,
            environment=only_default_slot_injected,
        )


def test_the_resolved_context_is_immutable(
    only_default_slot_injected: AlpacaCredentialEnvironment,
) -> None:
    context = resolve_runtime_context(
        endpoint_mode="paper",
        credential_slot="default",
        environment=only_default_slot_injected,
    )

    with pytest.raises(AttributeError):
        context.account_pin = "PA123"  # type: ignore[misc]

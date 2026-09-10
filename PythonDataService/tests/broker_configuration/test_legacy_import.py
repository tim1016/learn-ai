"""The one-time import of an environment-configured installation.

Each test here corresponds to a line in the plan's package-F "done when":
preview writes nothing, a repeated import is idempotent, the imported envelope
hash matches the legacy input, and an empty or partial environment is handled
explicitly rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings
from app.broker_configuration.alpaca_seams import AlpacaCredentialSlotDirectory
from app.broker_configuration.legacy_environment import LegacyEnvironmentValues
from app.broker_configuration.legacy_import import (
    ConfigurationImportRefused,
    ExistingConfiguration,
    apply_import,
    plan_import,
)
from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from tests.broker.alpaca.profile.conftest import (
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    make_environment,
)
from tests.broker_configuration.conftest import (
    OPERATOR_IDENTITY,
    FakeAccountVerifier,
    FrozenClock,
)
from tests.broker_configuration.test_legacy_environment import LEGACY_LIVE_ENVIRONMENT

NOW_MS = 1_757_000_000_000
PAPER_ACCOUNT = "PA000PAPER"


@pytest.fixture
def service(clerk_dir: Path, clock: FrozenClock) -> Iterator[BrokerConfigurationService]:
    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=AlpacaCredentialSlotDirectory(
            environment=make_environment(
                api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET
            )
        ),
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id=PAPER_ACCOUNT, account_mode="paper", account_status="ACTIVE")
        ),
    )
    yield built
    built.close()


def _values(**overrides: object) -> LegacyEnvironmentValues:
    return LegacyEnvironmentValues(_env_file=None, **overrides)


def _live_values() -> LegacyEnvironmentValues:
    return _values(
        mode="live",
        live_loss_fraction=0.05,
        live_loss_usd=5000.0,
        live_shadow_sessions=3,
        live_arming_max_sessions=20,
        live_xh_entry_bps=11.0,
        live_xh_exit_bps=17.5,
    )


def _plan(values: LegacyEnvironmentValues, existing: ExistingConfiguration, **kwargs: object):
    return plan_import(
        existing=existing,
        values=values,
        operator_identity=OPERATOR_IDENTITY,
        now_ms=NOW_MS,
        **kwargs,
    )


def _plan_and_apply(service: BrokerConfigurationService, values: LegacyEnvironmentValues):
    plan = _plan(values, ExistingConfiguration.read(service))
    return apply_import(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        service=service,
        values=values,
        now_ms=NOW_MS,
    )


# ---- preview writes nothing ------------------------------------------------


def test_planning_an_uninitialised_installation_reads_the_environment_only() -> None:
    """"Preview changes no runtime or profiles-database state" — literally none.

    ``ProfilesStore.open`` creates and migrates, so an ``ExistingConfiguration``
    that opened the database to answer would bring one into being just by being
    asked. ``absent()`` is how a plan describes an installation with no database
    without touching the filesystem; the CLI's half of this promise — checking
    existence before opening — is pinned in
    ``tests/scripts/test_manage_broker_configuration.py``.
    """
    plan = _plan(_live_values(), ExistingConfiguration.absent())

    assert plan.writes_anything
    assert plan.owner_will_be_created
    assert plan.expected_selection_generation == 0


def test_planning_creates_no_profile(service: BrokerConfigurationService) -> None:
    _plan(_live_values(), ExistingConfiguration.read(service))

    assert service.list_profiles(include_archived=True) == []
    assert service.existing_owner() is None


# ---- envelope fidelity -----------------------------------------------------


def test_the_planned_envelope_hashes_to_the_legacy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt ADR 0060 Decision 6 asks for, visible in the plan artifact."""
    for name, value in LEGACY_LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    expected = LiveEnvelopeValues.from_settings(AlpacaSettings(_env_file=None)).sha

    plan = _plan(_live_values(), ExistingConfiguration.absent())

    assert plan.envelope_sha == expected


def test_the_stored_revision_reproduces_the_envelope_sha(
    service: BrokerConfigurationService,
) -> None:
    """Store → load → sha, on the path that carries a live limit out of ``.env``."""
    values = _live_values()
    receipt = _plan_and_apply(service, values)

    stored = service.read_revision(receipt.profile_id, receipt.revision)

    assert stored.live_envelope is not None
    assert stored.live_envelope.sha == receipt.envelope_sha
    assert type(stored.live_envelope.loss_usd) is float
    assert type(stored.live_envelope.shadow_sessions) is int


# ---- empty and partial environments ----------------------------------------


def test_a_live_mode_with_an_incomplete_envelope_is_refused() -> None:
    """"Never guess a live limit." The refusal names what is missing."""
    partial = _values(mode="live", live_loss_fraction=0.05, live_loss_usd=5000.0)

    with pytest.raises(ConfigurationImportRefused) as refused:
        _plan(partial, ExistingConfiguration.absent())

    assert "ALPACA_LIVE_SHADOW_SESSIONS" in str(refused.value)


def test_an_empty_environment_is_refused_explicitly() -> None:
    with pytest.raises(ConfigurationImportRefused, match="nothing to import"):
        _plan(_values(), ExistingConfiguration.absent())


def test_a_paper_environment_with_partial_values_imports_no_envelope() -> None:
    """Partial values are dropped and *named*, never completed with a default."""
    partial = _values(mode="paper", live_loss_usd=5000.0)

    plan = _plan(partial, ExistingConfiguration.absent())

    assert plan.endpoint_mode == "paper"
    assert plan.live_envelope is None
    assert any("ALPACA_LIVE_LOSS_FRACTION" in note for note in plan.notes)


def test_a_paper_environment_with_every_value_preserves_them() -> None:
    """They bound nothing while the mode is paper, but losing them is data loss."""
    complete_paper = _values(
        mode="paper",
        live_loss_fraction=0.05,
        live_loss_usd=5000.0,
        live_shadow_sessions=3,
        live_arming_max_sessions=20,
        live_xh_entry_bps=11.0,
        live_xh_exit_bps=17.5,
    )

    plan = _plan(complete_paper, ExistingConfiguration.absent())

    assert plan.endpoint_mode == "paper"
    assert plan.live_envelope is not None


def test_an_absent_mode_defaults_to_paper_like_the_settings_do() -> None:
    plan = _plan(_values(live_loss_usd=5000.0), ExistingConfiguration.absent())

    assert plan.endpoint_mode == "paper"


# ---- what apply writes -----------------------------------------------------


def test_apply_creates_a_staged_unpinned_revision(service: BrokerConfigurationService) -> None:
    receipt = _plan_and_apply(service, _live_values())

    stored = service.read_revision(receipt.profile_id, receipt.revision)
    selection = service.selection()

    assert receipt.created is True
    assert receipt.staged is True
    # Never pinned: observing and approving an account is the configuration
    # page's seam, and importing one would be a second way to choose an account.
    assert stored.account_pin is None
    assert selection.staged_profile_id == receipt.profile_id
    # Staging is not applying. Nothing becomes effective without the operator.
    assert selection.apply_requested is False
    assert selection.effective_profile_id is None


def test_the_owner_is_seeded_but_never_renamed(service: BrokerConfigurationService) -> None:
    """"Import operator identity without rewriting history"."""
    service.rename_owner(display_label="Chosen by the operator")

    receipt = _plan_and_apply(service, _live_values())

    assert receipt.owner_display_label == "Chosen by the operator"


def test_the_owner_is_created_from_the_operator_identity(
    service: BrokerConfigurationService,
) -> None:
    receipt = _plan_and_apply(service, _live_values())

    assert receipt.owner_display_label == OPERATOR_IDENTITY


# ---- idempotency -----------------------------------------------------------


def test_a_repeated_import_creates_nothing(service: BrokerConfigurationService) -> None:
    values = _live_values()
    first = _plan_and_apply(service, values)

    second = _plan_and_apply(service, values)

    assert second.created is False
    assert (second.profile_id, second.revision) == (first.profile_id, first.revision)
    assert len(service.list_profiles(include_archived=True)) == 1


def test_a_repeated_import_after_the_variables_were_deleted_still_no_ops(
    service: BrokerConfigurationService,
) -> None:
    """The documented cutover order is import, delete the lines, restart.

    A re-run afterwards sees an environment that no longer matches the plan.
    Reading that as drift would make idempotency false exactly when an operator
    re-runs to confirm the first import worked, so the already-imported check
    deliberately comes first.
    """
    _plan_and_apply(service, _live_values())

    plan = _plan(_live_values(), ExistingConfiguration.read(service))
    receipt = apply_import(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        service=service,
        values=_values(),  # every retired line deleted
        now_ms=NOW_MS,
    )

    assert receipt.created is False
    assert len(service.list_profiles(include_archived=True)) == 1


def test_content_already_saved_by_hand_is_adopted_not_duplicated(
    service: BrokerConfigurationService,
) -> None:
    """Matched on content, not on display name."""
    existing = service.create_profile(
        display_name="My own name for it",
        credential_slot="default",
        endpoint_mode="live",
        live_envelope=_plan(_live_values(), ExistingConfiguration.absent()).live_envelope,
    )

    receipt = _plan_and_apply(service, _live_values())

    assert receipt.created is False
    assert receipt.profile_id == existing.profile.profile_id


# ---- the confirmation ceremony ---------------------------------------------


def test_a_wrong_token_is_refused(service: BrokerConfigurationService) -> None:
    plan = _plan(_live_values(), ExistingConfiguration.read(service))

    with pytest.raises(ConfigurationImportRefused, match="confirmation token"):
        apply_import(
            plan=plan,
            confirmation_token="0" * 64,
            service=service,
            values=_live_values(),
            now_ms=NOW_MS,
        )

    assert service.list_profiles(include_archived=True) == []


def test_an_edited_plan_no_longer_verifies(service: BrokerConfigurationService) -> None:
    """The token is the plan's own content hash, so a changed limit invalidates it."""
    from dataclasses import replace

    plan = _plan(_live_values(), ExistingConfiguration.read(service))
    tampered = replace(plan, display_name="Something else")

    with pytest.raises(ConfigurationImportRefused, match="content hash"):
        apply_import(
            plan=tampered,
            confirmation_token=plan.confirmation_token,
            service=service,
            values=_live_values(),
            now_ms=NOW_MS,
        )


def test_an_expired_plan_is_refused(service: BrokerConfigurationService) -> None:
    plan = _plan(_live_values(), ExistingConfiguration.read(service))

    with pytest.raises(ConfigurationImportRefused, match="expired"):
        apply_import(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            service=service,
            values=_live_values(),
            now_ms=plan.expires_at_ms + 1,
        )


def test_an_environment_edited_after_planning_is_refused(
    service: BrokerConfigurationService,
) -> None:
    """The operator confirmed a specific set of numbers; import those or none."""
    plan = _plan(_live_values(), ExistingConfiguration.read(service))
    raised = _values(
        mode="live",
        live_loss_fraction=0.05,
        live_loss_usd=50_000.0,  # ten times the confirmed limit
        live_shadow_sessions=3,
        live_arming_max_sessions=20,
        live_xh_entry_bps=11.0,
        live_xh_exit_bps=17.5,
    )

    with pytest.raises(ConfigurationImportRefused, match="live envelope values changed"):
        apply_import(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            service=service,
            values=raised,
            now_ms=NOW_MS,
        )

    assert service.list_profiles(include_archived=True) == []


def test_an_unknown_credential_slot_never_becomes_a_lookup() -> None:
    with pytest.raises(ConfigurationImportRefused, match="allowlist"):
        _plan(
            _live_values(),
            ExistingConfiguration.absent(),
            credential_slot="../../etc/passwd",
        )


def test_a_name_already_taken_by_different_content_is_refused(
    service: BrokerConfigurationService,
) -> None:
    service.create_profile(
        display_name="Imported from environment",
        credential_slot="default",
        endpoint_mode="paper",
        live_envelope=None,
    )

    with pytest.raises(ConfigurationImportRefused, match="already exists"):
        _plan(_live_values(), ExistingConfiguration.read(service))


# ---- the profiles database is authoritative afterwards ---------------------


def test_a_conflicting_environment_never_edits_the_saved_revision(
    service: BrokerConfigurationService,
) -> None:
    """After cutover the saved revision is the configuration; ``.env`` is not.

    An operator who raises a limit in ``.env`` and re-imports must not thereby
    edit the revision an arming record is sealed against. A second import with
    different content is a *new* profile the operator has to stage and apply
    deliberately — which is the whole point of Apply — and the original revision
    keeps its values byte for byte.
    """
    first = _plan_and_apply(service, _live_values())
    original = service.read_revision(first.profile_id, first.revision)
    raised = _values(
        mode="live",
        live_loss_fraction=0.05,
        live_loss_usd=50_000.0,
        live_shadow_sessions=3,
        live_arming_max_sessions=20,
        live_xh_entry_bps=11.0,
        live_xh_exit_bps=17.5,
    )

    plan = _plan(
        raised, ExistingConfiguration.read(service), display_name="Imported again"
    )
    second = apply_import(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        service=service,
        values=raised,
        now_ms=NOW_MS,
    )

    assert second.created is True
    assert second.profile_id != first.profile_id
    unchanged = service.read_revision(first.profile_id, first.revision)
    assert unchanged == original
    assert unchanged.live_envelope is not None
    assert unchanged.live_envelope.loss_usd == 5000.0
    assert unchanged.live_envelope.sha == original.live_envelope.sha  # type: ignore[union-attr]

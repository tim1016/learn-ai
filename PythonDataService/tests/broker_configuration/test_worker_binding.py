"""The startup ceremony: what a worker binds, and what it refuses to bind.

This is package D's safety surface. Every test here corresponds to a line in
the plan's acceptance matrix or its "done when" list, and the ones that matter
most are the refusals — a refused Apply and a crash with a staged selection
both have to leave the worker running its *last-effective* revision, because a
worker with no broker and an open position cannot EXIT.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.active_binding import (
    APPLY_PREFLIGHT_REFUSED,
    BROKER_UNCONFIGURED,
    PROFILES_DATABASE_UNAVAILABLE,
)
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker_configuration.alpaca_seams import AlpacaCredentialSlotDirectory
from app.broker_configuration.errors import ProfilesDatabaseUnavailable
from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from app.broker_configuration.worker_binding import (
    BoundWorker,
    PriorObligations,
    UnboundWorker,
    UnprovableObligations,
    acknowledge_worker_binding,
    resolve_worker_binding,
)
from tests.broker.alpaca.profile.conftest import (
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    LIVE_SLOT_KEY,
    LIVE_SLOT_SECRET,
    make_environment,
)
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    OPERATOR_IDENTITY,
    FakeAccountVerifier,
    FrozenClock,
)

PAPER_ACCOUNT = "PA000PAPER"
OTHER_ACCOUNT = "PA000OTHER"
LIVE_ACCOUNT = "9LIVE0001"

# The real, code-owned allowlist (package C). These tests resolve credentials
# for real, so a fictional slot name would be refused before anything
# interesting happened.
PAPER_SLOT = "default"
LIVE_SLOT = "live"


@pytest.fixture
def environment() -> AlpacaCredentialEnvironment:
    return make_environment(
        api_key_id=DEFAULT_SLOT_KEY,
        api_secret_key=DEFAULT_SLOT_SECRET,
        credential_live_key_id=LIVE_SLOT_KEY,
        credential_live_secret_key=LIVE_SLOT_SECRET,
    )


@pytest.fixture
def service(
    clerk_dir: Path, clock: FrozenClock, environment: AlpacaCredentialEnvironment
) -> Iterator[BrokerConfigurationService]:
    """A service wired to the *real* slot directory, plus a fake verifier.

    The directory is real because these tests bind for real; the verifier is
    fake because observing an account is the one step that would reach a
    broker. Every account the tests pin is observable through it.
    """
    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=AlpacaCredentialSlotDirectory(environment=environment),
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id=PAPER_ACCOUNT, account_mode="paper", account_status="ACTIVE"),
            ObservedAccount(account_id=OTHER_ACCOUNT, account_mode="paper", account_status="ACTIVE"),
            ObservedAccount(account_id=LIVE_ACCOUNT, account_mode="live", account_status="ACTIVE"),
        ),
    )
    yield built
    built.close()


class _Clear:
    """Every prior account is provably clear."""

    async def observe(self, account_id: str) -> PriorObligations:
        return PriorObligations(account_id=account_id)


class _Encumbered:
    """The prior account still holds a position."""

    async def observe(self, account_id: str) -> PriorObligations:
        return PriorObligations(account_id=account_id, blocking_facts=("1 open position",))


def _service(service: BrokerConfigurationService):
    return lambda: service


async def _profile_bound_to(
    service: BrokerConfigurationService,
    *,
    display_name: str,
    account_id: str,
    slot: str = PAPER_SLOT,
) -> tuple[str, int]:
    """A saved paper profile whose revision 1 is pinned to ``account_id``."""
    created = service.create_profile(
        display_name=display_name,
        credential_slot=slot,
        endpoint_mode="paper",
        live_envelope=None,
    )
    profile_id = created.profile.profile_id
    await service.pin_account(profile_id, 1, account_id=account_id)
    return profile_id, 1


def _make_effective(
    service: BrokerConfigurationService, profile_id: str, revision: int, account_id: str
) -> None:
    """Drive the selection to "this revision is effective", the way a worker does."""
    generation = service.selection().selection_generation
    service.stage_selection(
        profile_id=profile_id, revision=revision, expected_selection_generation=generation
    )
    service.acknowledge_effective(
        profile_id=profile_id,
        revision=revision,
        account_id=account_id,
        expected_selection_generation=service.selection().selection_generation,
    )


def _stage_and_apply(
    service: BrokerConfigurationService, profile_id: str, revision: int
) -> None:
    service.stage_selection(
        profile_id=profile_id,
        revision=revision,
        expected_selection_generation=service.selection().selection_generation,
    )
    service.request_apply(
        expected_selection_generation=service.selection().selection_generation
    )


# ---- nothing configured ----------------------------------------------------


async def test_a_configured_installation_with_nothing_applied_closes_the_gate(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """Profiles exist but none is effective: no broker, and no environment fallback."""
    service.create_profile(
        display_name="Paper — testing",
        credential_slot=PAPER_SLOT,
        endpoint_mode="paper",
        live_envelope=None,
    )

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, UnboundWorker)
    assert resolved.unbound.reason == BROKER_UNCONFIGURED
    assert resolved.unbound.message
    assert resolved.unbound.next_step


async def test_an_unreadable_profiles_database_closes_the_gate_without_crashing(
    environment: AlpacaCredentialEnvironment,
) -> None:
    """#2014's failure mode: fail closed with a reason, never a crash loop."""

    def _refuse() -> BrokerConfigurationService:
        raise ProfilesDatabaseUnavailable("the volume is not mounted")

    resolved = await resolve_worker_binding(
        service_factory=_refuse, environment=environment
    )

    assert isinstance(resolved, UnboundWorker)
    assert resolved.unbound.reason == PROFILES_DATABASE_UNAVAILABLE


# ---- ordinary recovery -----------------------------------------------------


async def test_a_restart_binds_the_effective_revision(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    profile_id, revision = await _profile_bound_to(
        service, display_name="Paper — testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, revision, PAPER_ACCOUNT)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.from_profile
    assert resolved.context.profile_id == profile_id
    assert resolved.context.revision == revision
    assert resolved.context.account_pin == PAPER_ACCOUNT
    assert resolved.context.settings.mode == "paper"


async def test_same_account_recovery_with_exposure_is_never_preflighted(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """Recovering your own account with a position open must work.

    The probe is one that refuses everything; a recovery that consulted it
    would refuse, which is exactly the regression this pins.
    """
    profile_id, revision = await _profile_bound_to(
        service, display_name="Paper — testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, revision, PAPER_ACCOUNT)

    resolved = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Encumbered(),
        environment=environment,
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.profile_id == profile_id


async def test_a_crash_with_a_staged_selection_boots_the_last_effective_revision(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper — effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper — staged", account_id=OTHER_ACCOUNT
    )
    service.stage_selection(
        profile_id=staged_id,
        revision=1,
        expected_selection_generation=service.selection().selection_generation,
    )

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.profile_id == effective_id
    assert service.selection().staged_profile_id == staged_id


# ---- applying --------------------------------------------------------------


async def test_an_applied_revision_becomes_the_binding_when_the_prior_account_is_clear(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper — effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper — staged", account_id=OTHER_ACCOUNT
    )
    _stage_and_apply(service, staged_id, 1)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), obligations=_Clear(), environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.profile_id == staged_id
    assert resolved.context.account_pin == OTHER_ACCOUNT


async def test_switching_accounts_with_exposure_refuses_and_boots_last_effective(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """The row this whole package exists for.

    A position at the previous account, an Apply pointing somewhere else: the
    Apply is refused, the refusal is recorded with its reason, and the worker
    comes up on the revision that can still EXIT that position.
    """
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper — effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper — staged", account_id=OTHER_ACCOUNT
    )
    _stage_and_apply(service, staged_id, 1)

    resolved = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Encumbered(),
        environment=environment,
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.profile_id == effective_id
    assert resolved.context.account_pin == PAPER_ACCOUNT

    selection = service.selection()
    assert selection.last_apply_outcome == "refused"
    assert "1 open position" in (selection.last_apply_refusal_reason or "")
    assert not selection.apply_requested


async def test_a_refused_apply_is_consumed_so_the_next_restart_cannot_re_arm_it(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper — effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper — staged", account_id=OTHER_ACCOUNT
    )
    _stage_and_apply(service, staged_id, 1)

    await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Encumbered(),
        environment=environment,
    )
    second = await resolve_worker_binding(
        service_factory=_service(service), obligations=_Clear(), environment=environment
    )

    assert isinstance(second, BoundWorker)
    assert second.context.profile_id == effective_id


async def test_an_unprovable_prior_account_refuses_rather_than_assuming_it_is_clear(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """Absence of evidence is not evidence of absence (plan §5)."""
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper — effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper — staged", account_id=OTHER_ACCOUNT
    )
    _stage_and_apply(service, staged_id, 1)

    resolved = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=UnprovableObligations(),
        environment=environment,
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.profile_id == effective_id
    assert "could not be inspected" in (service.selection().last_apply_refusal_reason or "")


async def test_applying_a_new_revision_of_the_same_account_needs_no_flatness(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """Two revisions, one account: nothing is stranded, so nothing is refused."""
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper — testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, 1, PAPER_ACCOUNT)
    service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot=LIVE_SLOT,
        endpoint_mode="paper",
        live_envelope=None,
    )
    await service.pin_account(profile_id, 2, account_id=PAPER_ACCOUNT)
    _stage_and_apply(service, profile_id, 2)

    resolved = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Encumbered(),
        environment=environment,
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.revision == 2
    assert service.selection().last_apply_outcome != "refused"


async def test_the_first_apply_being_refused_leaves_nothing_stranded(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """No previous binding means no position, order or custody to abandon."""
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper — staged", account_id=PAPER_ACCOUNT, slot=LIVE_SLOT
    )
    _stage_and_apply(service, staged_id, 1)

    # The worker resolves against an environment with no live pair injected, so
    # the staged revision names a real slot that cannot be resolved.
    resolved = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Clear(),
        environment=make_environment(
            api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET
        ),
    )

    assert isinstance(resolved, UnboundWorker)
    assert resolved.unbound.reason == APPLY_PREFLIGHT_REFUSED


# ---- the acknowledgement ---------------------------------------------------


async def test_the_effective_binding_is_recorded_after_an_apply(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper — testing", account_id=PAPER_ACCOUNT
    )
    _stage_and_apply(service, profile_id, 1)
    resolved = await resolve_worker_binding(
        service_factory=_service(service), obligations=_Clear(), environment=environment
    )
    assert isinstance(resolved, BoundWorker)

    acknowledge_worker_binding(
        bound=resolved, account_id=PAPER_ACCOUNT, service_factory=_service(service)
    )

    selection = service.selection()
    assert selection.effective_profile_id == profile_id
    assert selection.effective_revision == 1
    assert selection.effective_account_id == PAPER_ACCOUNT
    assert selection.last_apply_outcome == "applied"


async def test_an_ordinary_restart_does_not_advance_the_selection_generation(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """A reboot must not invalidate the generation a browser is holding."""
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper — testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, 1, PAPER_ACCOUNT)
    before = service.selection().selection_generation

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )
    assert isinstance(resolved, BoundWorker)
    acknowledge_worker_binding(
        bound=resolved, account_id=PAPER_ACCOUNT, service_factory=_service(service)
    )

    assert service.selection().selection_generation == before


async def test_a_stale_worker_cannot_publish_itself_as_the_effective_runtime(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """The generation fence, from the worker's side.

    The worker resolves, the operator stages something else while it is
    building, and the worker's acknowledgement is refused rather than
    overwriting the newer selection.
    """
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper — testing", account_id=PAPER_ACCOUNT
    )
    _stage_and_apply(service, profile_id, 1)
    resolved = await resolve_worker_binding(
        service_factory=_service(service), obligations=_Clear(), environment=environment
    )
    assert isinstance(resolved, BoundWorker)

    other_id, _ = await _profile_bound_to(
        service, display_name="Paper — other", account_id=OTHER_ACCOUNT
    )
    service.stage_selection(
        profile_id=other_id,
        revision=1,
        expected_selection_generation=service.selection().selection_generation,
    )

    acknowledge_worker_binding(
        bound=resolved, account_id=PAPER_ACCOUNT, service_factory=_service(service)
    )

    assert service.selection().effective_profile_id is None


# ---- the pre-cutover bootstrap ---------------------------------------------


async def test_an_installation_with_no_profiles_boots_from_the_environment(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Package F's import retires this path by writing the first profile."""
    from app.broker.alpaca import config as config_module

    monkeypatch.setattr(
        config_module,
        "get_alpaca_settings",
        lambda: config_module.AlpacaSettings(
            api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET, mode="paper"
        ),
    )

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert not resolved.from_profile
    assert resolved.context.profile_id is None


async def test_the_environment_bootstrap_acknowledges_no_effective_profile(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It binds no revision, so recording one would invent an operator's choice."""
    from app.broker.alpaca import config as config_module

    monkeypatch.setattr(
        config_module,
        "get_alpaca_settings",
        lambda: config_module.AlpacaSettings(
            api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET, mode="paper"
        ),
    )
    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )
    assert isinstance(resolved, BoundWorker)

    acknowledge_worker_binding(
        bound=resolved, account_id=PAPER_ACCOUNT, service_factory=_service(service)
    )

    assert service.selection().effective_profile_id is None


# ---- live revisions --------------------------------------------------------


async def test_a_live_revision_binds_its_sealed_envelope_values(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment, clerk_dir: Path
) -> None:
    """Store → load → the same six values, in the same Python types."""
    from app.broker_configuration.envelope import ValidatedLiveEnvelope

    created = service.create_profile(
        display_name="Live — real money",
        credential_slot=LIVE_SLOT,
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )
    profile_id = created.profile.profile_id
    await service.pin_account(profile_id, 1, account_id=LIVE_ACCOUNT)
    _make_effective(service, profile_id, 1, LIVE_ACCOUNT)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.settings.mode == "live"
    envelope = resolved.context.live_envelope
    assert envelope is not None
    assert envelope.loss_fraction == LIVE_ENVELOPE_PAYLOAD["loss_fraction"]
    assert type(envelope.shadow_sessions) is int
    assert type(envelope.xh_exit_bps) is float

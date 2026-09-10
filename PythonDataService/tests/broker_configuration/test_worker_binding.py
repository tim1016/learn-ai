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
    ACCOUNT_PIN_MISMATCH,
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


# ---- the account pin, re-observed at startup -------------------------------


class _Reaches:
    """A broker whose credentials reach exactly one account."""

    def __init__(self, account_id: str, *, mode: str = "paper") -> None:
        self._account_id = account_id
        self._mode = mode

    async def get_account(self):
        from app.broker.contract.models import BrokerAccountSnapshot
        from app.utils.timestamps import now_ms_utc

        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=self._account_id,
            account_mode=self._mode,
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
            observed_at_ms=now_ms_utc(),
        )


def _reaching(account_id: str):
    """Patch the startup re-observation to reach ``account_id``."""
    import app.broker_configuration.worker_binding as wb
    from app.broker.alpaca.profile import verify_account as real_verify

    async def _verify(context, *, discovery=None):
        return await real_verify(context, discovery=_Reaches(account_id))

    return wb, _verify


async def test_credentials_reaching_the_pinned_account_bind_normally(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper - testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, 1, PAPER_ACCOUNT)
    wb, verify = _reaching(PAPER_ACCOUNT)
    monkeypatch.setattr(wb, "verify_account", verify)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.account_pin == PAPER_ACCOUNT


async def test_credentials_reaching_another_account_refuse_before_custody(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential slot repointed at a different Alpaca account.

    Everything else still looks right - the profile is applied, the mode
    agrees, the envelope is intact - so nothing else in the boot would catch
    it, and the worker would take custody of an account nobody approved. The
    check runs before the Clerk composes, so no lease is ever taken on it.
    """
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper - testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, 1, PAPER_ACCOUNT)
    wb, verify = _reaching(OTHER_ACCOUNT)
    monkeypatch.setattr(wb, "verify_account", verify)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, UnboundWorker)
    assert resolved.unbound.reason == ACCOUNT_PIN_MISMATCH


async def test_an_unreachable_broker_at_startup_is_not_a_pin_mismatch(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient blip must not unbind the worker for the whole process.

    Today an unreachable broker still boots and authority selection reports
    BROKER_ACCOUNT_UNAVAILABLE, which can recover. Treating "cannot observe" as
    "wrong account" would be strictly worse.
    """
    import app.broker_configuration.worker_binding as wb
    from app.broker.alpaca.profile import AccountVerificationFailed

    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper - testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, 1, PAPER_ACCOUNT)

    async def _unreachable(context, *, discovery=None):
        raise AccountVerificationFailed(
            "the broker could not be reached", next_step="Retry once it is reachable."
        )

    monkeypatch.setattr(wb, "verify_account", _unreachable)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)


async def test_an_unpinned_revision_needs_no_reobservation(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker_configuration.worker_binding as wb

    created = service.create_profile(
        display_name="Paper - unpinned",
        credential_slot=PAPER_SLOT,
        endpoint_mode="paper",
        live_envelope=None,
    )
    _make_effective(service, created.profile.profile_id, 1, PAPER_ACCOUNT)

    async def _must_not_run(context, *, discovery=None):  # pragma: no cover
        raise AssertionError("an unpinned revision has no pin to re-observe")

    monkeypatch.setattr(wb, "verify_account", _must_not_run)

    resolved = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )

    assert isinstance(resolved, BoundWorker)


# ---- regressions from the independent review -------------------------------


async def test_a_clerk_less_boot_does_not_erase_the_remembered_account(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """The switch preflight reads effective_account_id to decide what to prove.

    A boot where authority selection fails - activation missing, lease held
    elsewhere, database refused - takes no execution lease, so it must not
    write the effective fields at all. Writing them NULLed the remembered
    account, which silently disarmed the preflight: the next Apply saw
    NO_PREVIOUS_BINDING and switched away from an account holding a position
    without ever consulting the obligations probe.
    """
    profile_id, _ = await _profile_bound_to(
        service, display_name="Paper - testing", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, profile_id, 1, PAPER_ACCOUNT)

    booted = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )
    assert isinstance(booted, BoundWorker)
    acknowledge_worker_binding(
        bound=booted, account_id=None, service_factory=_service(service)
    )

    assert service.selection().effective_account_id == PAPER_ACCOUNT


async def test_the_preflight_still_runs_after_a_clerk_less_boot(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """The consequence the previous test protects, driven end to end."""
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper - effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)

    clerk_less = await resolve_worker_binding(
        service_factory=_service(service), environment=environment
    )
    assert isinstance(clerk_less, BoundWorker)
    acknowledge_worker_binding(
        bound=clerk_less, account_id=None, service_factory=_service(service)
    )

    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper - staged", account_id=OTHER_ACCOUNT
    )
    _stage_and_apply(service, staged_id, 1)
    switched = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Encumbered(),
        environment=environment,
    )

    assert isinstance(switched, BoundWorker)
    assert switched.context.profile_id == effective_id
    assert service.selection().last_apply_outcome == "refused"


async def test_a_refused_apply_survives_an_unreadable_database_afterwards(
    service: BrokerConfigurationService, environment: AlpacaCredentialEnvironment
) -> None:
    """The refusal path must not carry the module's only unguarded read.

    A database blip while booting the last-effective revision used to escape
    into the lifespan and abort startup - a crash loop in exactly the state
    this branch exists for, where an Apply was just refused because the prior
    account still holds a position and nobody can EXIT if the worker will not
    boot.
    """
    effective_id, _ = await _profile_bound_to(
        service, display_name="Paper - effective", account_id=PAPER_ACCOUNT
    )
    _make_effective(service, effective_id, 1, PAPER_ACCOUNT)
    staged_id, _ = await _profile_bound_to(
        service, display_name="Paper - staged", account_id=OTHER_ACCOUNT
    )
    _stage_and_apply(service, staged_id, 1)

    calls: list[str] = []
    real_selection = service.selection

    def _selection_fails_after_the_refusal() -> object:
        calls.append("selection")
        if len(calls) > 1:
            raise ProfilesDatabaseUnavailable("the volume went away")
        return real_selection()

    service.selection = _selection_fails_after_the_refusal  # type: ignore[method-assign]

    resolved = await resolve_worker_binding(
        service_factory=_service(service),
        obligations=_Encumbered(),
        environment=environment,
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.profile_id == effective_id

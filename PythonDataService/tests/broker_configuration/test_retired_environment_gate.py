"""Refusing to bind while a retired setting is still in the environment.

The owner's resolution of ADR 0060 open question 1 (2026-09-10): a variable the
profiles database replaced, left behind after cutover, is **refused** rather
than ignored quietly — which is what ``extra="ignore"`` does today.

Three properties, and the second is the one that would break every deployment if
it were wrong:

1. A cut-over installation refuses, names the variables, and **still boots** —
   the gate closes, the process does not crash-loop (#2014).
2. A **pre-cutover** installation does not refuse. Before the import runs, those
   same variables are the only description of the worker there is.
3. The operator CLIs refuse identically, so an arming ceremony cannot run
   against a configuration the worker itself would not bind.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.active_binding import (
    RETIRED_ENVIRONMENT_SETTINGS,
    BrokerUnbound,
)
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker_configuration.alpaca_seams import AlpacaCredentialSlotDirectory
from app.broker_configuration.cli_binding import effective_broker
from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from app.broker_configuration.worker_binding import (
    BoundWorker,
    UnboundWorker,
    resolve_worker_binding,
)
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

PAPER_ACCOUNT = "PA000PAPER"
PAPER_SLOT = "default"


@pytest.fixture
def environment() -> AlpacaCredentialEnvironment:
    return make_environment(api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET)


@pytest.fixture
def service(
    clerk_dir: Path, clock: FrozenClock, environment: AlpacaCredentialEnvironment
) -> Iterator[BrokerConfigurationService]:
    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=AlpacaCredentialSlotDirectory(environment=environment),
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id=PAPER_ACCOUNT, account_mode="paper", account_status="ACTIVE")
        ),
    )
    yield built
    built.close()


@pytest.fixture
def reads_the_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the autouse isolation, for the tests that are *about* the detector.

    ``tests/conftest.py`` pins every test's view of the retired settings to
    "absent" so a developer's real ``.env`` cannot decide an unrelated binding
    test. These tests need the real reader back; a later patch wins.
    """


async def _effective_paper_profile(service: BrokerConfigurationService) -> None:
    """One saved, pinned, effective revision — an installation that has cut over."""
    created = service.create_profile(
        display_name="Imported from environment",
        credential_slot=PAPER_SLOT,
        endpoint_mode="paper",
        live_envelope=None,
    )
    profile_id = created.profile.profile_id
    await service.pin_account(profile_id, 1, account_id=PAPER_ACCOUNT)
    service.stage_selection(
        profile_id=profile_id,
        revision=1,
        expected_selection_generation=service.selection().selection_generation,
    )
    service.acknowledge_effective(
        profile_id=profile_id,
        revision=1,
        account_id=PAPER_ACCOUNT,
        expected_selection_generation=service.selection().selection_generation,
    )


@pytest.mark.usefixtures("reads_the_real_environment")
async def test_a_cut_over_worker_refuses_while_a_retired_variable_is_set(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _effective_paper_profile(service)
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment
    )

    assert isinstance(resolved, UnboundWorker)
    assert resolved.unbound.reason == RETIRED_ENVIRONMENT_SETTINGS
    assert "ALPACA_LIVE_LOSS_USD" in resolved.unbound.next_step


@pytest.mark.usefixtures("reads_the_real_environment")
async def test_the_refusal_is_a_closed_gate_not_a_crash(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2014's shape: a configuration problem never prevents the service booting.

    ``resolve_worker_binding`` returning an ``UnboundWorker`` is exactly what
    ``app/main.py`` already handles — it logs the reason code and continues — so
    the refusal reaches the operator as a 503 with words, not a restart loop.
    """
    await _effective_paper_profile(service)
    monkeypatch.setenv("ALPACA_MODE", "paper")

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment
    )

    assert isinstance(resolved, UnboundWorker)


@pytest.mark.usefixtures("reads_the_real_environment")
async def test_the_credential_pair_does_not_trip_the_gate(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that would brick every deployment if the list were wrong.

    ``ALPACA_API_KEY_ID`` / ``ALPACA_API_SECRET_KEY`` *are* the ``default``
    credential slot. They stay in the environment forever, and a cut-over worker
    binds normally with them present.
    """
    await _effective_paper_profile(service)
    monkeypatch.setenv("ALPACA_API_KEY_ID", DEFAULT_SLOT_KEY)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", DEFAULT_SLOT_SECRET)
    monkeypatch.setenv("ALPACA_CLERK_DIR", "/app/artifacts/alpaca_clerk")

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.from_profile


@pytest.mark.usefixtures("reads_the_real_environment")
async def test_a_pre_cutover_installation_still_boots_from_the_environment(
    clerk_dir: Path,
    clock: FrozenClock,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No profiles: the retired variables are still the source, so no refusal.

    Refusing here would break every deployment that has not run the import yet,
    which is all of them at the moment this lands.
    """
    empty = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=AlpacaCredentialSlotDirectory(environment=environment),
        account_verifier=FakeAccountVerifier(),
    )
    monkeypatch.setenv("ALPACA_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY_ID", DEFAULT_SLOT_KEY)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", DEFAULT_SLOT_SECRET)
    try:
        resolved = await resolve_worker_binding(
            service_factory=lambda: empty, environment=environment
        )
    finally:
        empty.close()

    assert isinstance(resolved, BoundWorker)
    assert resolved.candidate is None  # the environment bootstrap, not a profile


async def test_a_refused_apply_is_consumed_so_a_later_tidy_up_cannot_re_arm_it(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0060 D4.3: a refused Apply is consumed, not left pending.

    Otherwise an operator presses Apply on a live revision, restarts, meets this
    refusal and walks away -- and weeks later someone deletes the stale line for
    an unrelated reason, restarts, and the pending Apply silently takes effect.
    The change has to be re-authorised after the environment is fixed.
    """
    await _effective_paper_profile(service)
    created = service.create_profile(
        display_name="A change the operator applied",
        credential_slot=PAPER_SLOT,
        endpoint_mode="paper",
        live_envelope=None,
    )
    service.stage_selection(
        profile_id=created.profile.profile_id,
        revision=1,
        expected_selection_generation=service.selection().selection_generation,
    )
    service.request_apply(
        expected_selection_generation=service.selection().selection_generation
    )
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment
    )

    assert isinstance(resolved, UnboundWorker)
    assert resolved.unbound.reason == RETIRED_ENVIRONMENT_SETTINGS
    selection = service.selection()
    assert selection.apply_requested is False
    assert selection.last_apply_outcome == "refused"
    assert selection.last_apply_refusal_reason is not None
    # The previously-effective revision is untouched: the refusal blocked a
    # *change*, it did not rewrite what was already bound.
    assert selection.effective_profile_id != created.profile.profile_id


async def test_the_operator_clis_refuse_the_same_way(
    service: BrokerConfigurationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arming ceremony must not run against what the worker would not bind."""
    await _effective_paper_profile(service)
    monkeypatch.setenv("ALPACA_LIVE_SHADOW_SESSIONS", "3")

    with pytest.raises(BrokerUnbound) as refused:
        effective_broker(service_factory=lambda: service)

    assert refused.value.reason == RETIRED_ENVIRONMENT_SETTINGS

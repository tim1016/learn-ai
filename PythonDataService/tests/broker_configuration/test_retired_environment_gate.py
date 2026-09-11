"""Answering a retired setting that is still in the environment after cutover.

The owner's resolution of ADR 0060 open question 1 (2026-09-10): a variable the
profiles database replaced, left behind after cutover, is **answered** rather
than ignored quietly — which is what ``extra="ignore"`` does today. Narrowed the
same day, after review found the first cut collided with owner decision 4: *what*
the answer is depends on why the worker is binding.

Four properties, and the middle two are the ones that would break a live
deployment if they were wrong:

1. A deliberate **Apply** refuses, names the variables, and **still boots** —
   the gate closes, the process does not crash-loop (#2014).
2. An ordinary **restart** of an already-effective configuration *binds*, and
   says so loudly in the log. Refusing it would leave the worker with no broker
   while a position could be open, and nothing able to EXIT it (ADR 0060 D4.3).
3. A **pre-cutover** installation does not refuse. Before the import runs, those
   same variables are the only description of the worker there is.
4. The operator CLIs refuse, because running a ceremony is a deliberate act in
   the same family as Apply.
"""

from __future__ import annotations

import logging
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
    PriorObligations,
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


async def _pending_apply_on_a_second_profile(service: BrokerConfigurationService) -> str:
    """A staged revision with Apply pressed against it — a deliberate change.

    Pinned to the same account as the effective revision, the way an operator
    reaches Apply in practice: the configuration page verifies and approves the
    account first. Without the pin the Apply would be refused by the *switch*
    preflight instead, which is a different test's subject.
    """
    created = service.create_profile(
        display_name="A change the operator applied",
        credential_slot=PAPER_SLOT,
        endpoint_mode="paper",
        live_envelope=None,
    )
    await service.pin_account(created.profile.profile_id, 1, account_id=PAPER_ACCOUNT)
    service.stage_selection(
        profile_id=created.profile.profile_id,
        revision=1,
        expected_selection_generation=service.selection().selection_generation,
    )
    service.request_apply(
        expected_selection_generation=service.selection().selection_generation
    )
    return created.profile.profile_id


def _logged_actions(caplog: pytest.LogCaptureFixture, action: str) -> list[logging.LogRecord]:
    """Every record this boot emitted under one structured ``action``."""
    return [record for record in caplog.records if getattr(record, "action", None) == action]


async def test_a_plain_restart_binds_and_complains_while_a_retired_variable_is_set(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The narrowing, and the reason for it (owner decision 4 / ADR 0060 D4.3).

    Nothing is pending here: this is an already-effective installation coming
    back from a crash, a reboot or an OOM kill. Refusing it — which this gate did
    when it first landed — would leave the worker holding no broker on *every*
    subsequent restart, and a worker with no broker cannot EXIT a position it
    already has. One stale line must not be able to do that.

    So the boot binds, and the stale fact is carried by the log instead. The two
    assertions on the bound context are the other half of the bargain: binding
    anyway is only safe because nothing in the binding comes from these
    variables. ``ALPACA_MODE=live`` is set here and the worker still binds the
    profile's ``paper``.
    """
    await _effective_paper_profile(service)
    monkeypatch.setenv("ALPACA_MODE", "live")
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    with caplog.at_level(logging.ERROR, logger="app.broker_configuration.worker_binding"):
        resolved = await resolve_worker_binding(
            service_factory=lambda: service, environment=environment
        )

    assert isinstance(resolved, BoundWorker)
    assert resolved.from_profile
    assert resolved.context.settings.mode == "paper"
    assert resolved.context.settings.live_loss_usd is None

    complaints = _logged_actions(caplog, "worker_binding_retired_environment_recovery_warning")
    assert len(complaints) == 1
    assert complaints[0].levelno == logging.ERROR
    assert complaints[0].__dict__["retired_variables"] == [
        "ALPACA_MODE",
        "ALPACA_LIVE_LOSS_USD",
    ]
    assert complaints[0].__dict__["reason_code"] == RETIRED_ENVIRONMENT_SETTINGS
    # The refusal's own action is absent: nothing was refused on this boot, and
    # an alert rule keyed on it must not fire for an ordinary restart.
    assert _logged_actions(caplog, "worker_binding_retired_environment") == []


async def test_a_pending_apply_still_refuses_while_a_retired_variable_is_set(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other side of the narrowing: a deliberate change still refuses.

    The environment is *identical* to the restart above, deliberately — the only
    difference is the pending Apply, which is the whole of the distinction the
    owner drew. Applying a change while a retired line still claims to describe
    the configuration is the case that stays closed, and an operator who just
    pressed Apply is standing there to delete the line.

    What that refusal does to the one-shot Apply has its own test below; the one
    assertion here is enough to say the two are wired together.
    """
    await _effective_paper_profile(service)
    await _pending_apply_on_a_second_profile(service)
    monkeypatch.setenv("ALPACA_MODE", "live")
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    with caplog.at_level(logging.ERROR, logger="app.broker_configuration.worker_binding"):
        resolved = await resolve_worker_binding(
            service_factory=lambda: service, environment=environment
        )

    assert isinstance(resolved, BoundWorker)
    assert resolved.context.settings.mode == "paper"
    assert resolved.candidate is not None
    assert resolved.candidate.profile_id == service.selection().effective_profile_id
    assert service.selection().last_apply_outcome == "refused"

    refusals = _logged_actions(caplog, "worker_binding_retired_environment")
    assert len(refusals) == 1
    assert refusals[0].__dict__["retired_variables"] == ["ALPACA_MODE", "ALPACA_LIVE_LOSS_USD"]
    assert _logged_actions(caplog, "worker_binding_retired_environment_recovery_warning") == []


async def test_the_refusal_is_a_closed_gate_not_a_crash(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2014's shape: a configuration problem never prevents the service booting.

    ``resolve_worker_binding`` returning an ``UnboundWorker`` is exactly what
    ``app/main.py`` already handles — it logs the reason code and continues — so
    the refusal reaches the operator as a 503 with words, not a restart loop. The
    Apply is what makes this boot refusable at all; see the restart test above
    for what the same environment does without one.
    """
    await _pending_apply_on_a_second_profile(service)
    monkeypatch.setenv("ALPACA_MODE", "paper")

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment
    )

    assert isinstance(resolved, UnboundWorker)


async def test_the_credential_pair_does_not_trip_the_gate(
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that would brick every deployment if the list were wrong.

    ``ALPACA_API_KEY_ID`` / ``ALPACA_API_SECRET_KEY`` *are* the ``default``
    credential slot. They stay in the environment forever, and a cut-over worker
    binds normally with them present.

    Deliberately an **Apply** boot. A recovery binds through a retired variable
    now, so running this as a restart would assert nothing: it would pass just as
    happily if someone wrongly added the credential pair to ``RETIRED_SETTINGS``.
    Apply is the boot shape where a wrongly-retired name still refuses, so it is
    the only one where this test has teeth.
    """
    await _effective_paper_profile(service)
    staged_profile_id = await _pending_apply_on_a_second_profile(service)
    monkeypatch.setenv("ALPACA_API_KEY_ID", DEFAULT_SLOT_KEY)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", DEFAULT_SLOT_SECRET)
    monkeypatch.setenv("ALPACA_CLERK_DIR", "/app/artifacts/alpaca_clerk")

    class ClearAccount:
        async def observe(self, account_id: str) -> PriorObligations:
            return PriorObligations(account_id=account_id)

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment, obligations=ClearAccount()
    )

    assert isinstance(resolved, BoundWorker)
    assert resolved.candidate is not None
    assert resolved.candidate.profile_id == staged_profile_id


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
    staged_profile_id = await _pending_apply_on_a_second_profile(service)
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    resolved = await resolve_worker_binding(
        service_factory=lambda: service, environment=environment
    )

    assert isinstance(resolved, BoundWorker)
    selection = service.selection()
    assert selection.apply_requested is False
    assert selection.last_apply_outcome == "refused"
    assert selection.last_apply_refusal_reason is not None
    # The previously-effective revision is untouched: the refusal blocked a
    # *change*, it did not rewrite what was already bound.
    assert selection.effective_profile_id != staged_profile_id


async def test_the_operator_clis_refuse_the_same_way(
    service: BrokerConfigurationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceremony is a deliberate act, so it refuses where a restart would not.

    The worker's *recovery* path now binds through a stale line and complains in
    the log; an operator command does not. Running one is a deliberate act in the
    same family as Apply — somebody is at the keyboard, and an arming ceremony is
    the last place two resolvers should disagree about which document is in
    force. Refusing costs that operator one line-edit; binding would seal an
    arming record against a configuration whose description is ambiguous.
    """
    await _effective_paper_profile(service)
    monkeypatch.setenv("ALPACA_LIVE_SHADOW_SESSIONS", "3")

    with pytest.raises(BrokerUnbound) as refused:
        effective_broker(service_factory=lambda: service)

    assert refused.value.reason == RETIRED_ENVIRONMENT_SETTINGS

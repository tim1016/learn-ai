"""The whole cutover, start to finish, on an isolated tmp-path installation.

The plan's package-F "done when" asks for a rehearsal demonstrating cutover and
rollback. The *production* rehearsal is a scheduled operator action against a
copy of a real Clerk volume and is explicitly not an implementation agent's to
perform; this is its automated equivalent, and it is the only test that walks
every step in order:

    env-configured boot → import → Apply → restart with the lines still
    present (refused) → lines deleted → bound from the profile → rollback

Three properties fall out of walking it in order rather than testing each step
alone. The refusal at step 4 is *load-bearing to the documented sequence*, not
an edge case — an operator who applies before editing ``.env`` meets it every
time. The envelope the worker finally binds is asserted equal, by ``sha``, to
the one the environment described at step 1: the cutover moved the numbers
without changing the document every arming record is sealed over. And step 6
puts the retired lines *back* with no Apply pending, which is the shape of every
unattended restart afterwards — it binds and complains rather than refusing,
because a refusal there would strand a live account (ADR 0060 D4.3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.active_binding import RETIRED_ENVIRONMENT_SETTINGS
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker_configuration.alpaca_seams import AlpacaCredentialSlotDirectory
from app.broker_configuration.legacy_environment import (
    LegacyEnvironmentValues,
)
from app.broker_configuration.legacy_import import (
    ExistingConfiguration,
    apply_import,
    plan_import,
)
from app.broker_configuration.records import ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from app.broker_configuration.worker_binding import (
    BoundWorker,
    UnboundWorker,
    acknowledge_worker_binding,
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
from tests.broker_configuration.test_legacy_environment import LEGACY_LIVE_ENVIRONMENT

NOW_MS = 1_757_000_000_000
LIVE_ACCOUNT = "9LIVE0001"


@pytest.fixture
def credential_environment() -> AlpacaCredentialEnvironment:
    return make_environment(api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET)


@pytest.fixture
def service(
    clerk_dir: Path, clock: FrozenClock, credential_environment: AlpacaCredentialEnvironment
) -> Iterator[BrokerConfigurationService]:
    built = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        clock=clock,
        credential_slots=AlpacaCredentialSlotDirectory(environment=credential_environment),
        account_verifier=FakeAccountVerifier(
            ObservedAccount(account_id=LIVE_ACCOUNT, account_mode="live", account_status="ACTIVE")
        ),
    )
    yield built
    built.close()


@pytest.fixture
def rehearsal_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An ADR 0059 live deployment: seven retired lines plus a credential pair.

    Set on the *process* environment, which is the half of the reader the autouse
    isolation in ``tests/conftest.py`` leaves alone — it drops only the ``.env``
    half, so this test sees exactly what it sets and never whatever ``.env``
    happens to sit beside the test run.
    """
    for name, value in LEGACY_LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALPACA_API_KEY_ID", DEFAULT_SLOT_KEY)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", DEFAULT_SLOT_SECRET)
    reset_alpaca_settings_for_testing()
    yield
    reset_alpaca_settings_for_testing()


def _delete_retired_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 5 of the documented order: the operator edits ``.env``."""
    for name in LEGACY_LIVE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("rehearsal_environment")
async def test_the_whole_cutover_and_its_rollback(
    service: BrokerConfigurationService,
    credential_environment: AlpacaCredentialEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy_sha = LiveEnvelopeValues.from_settings(AlpacaSettings(_env_file=None)).sha

    # 1. Before the import: no profiles, so the environment is still the only
    #    description of this worker and it boots from it unchanged.
    before = await resolve_worker_binding(
        service_factory=lambda: service, environment=credential_environment
    )
    assert isinstance(before, BoundWorker)
    assert before.candidate is None
    assert before.context.live_envelope is not None
    assert before.context.live_envelope.sha == legacy_sha

    # 2. Import. Reads the environment, writes one profile revision, stages it.
    values = LegacyEnvironmentValues(_env_file=None)
    plan = plan_import(
        existing=ExistingConfiguration.read(service),
        values=values,
        operator_identity=OPERATOR_IDENTITY,
        now_ms=NOW_MS,
    )
    assert plan.envelope_sha == legacy_sha
    receipt = apply_import(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        service=service,
        values=values,
        now_ms=NOW_MS,
    )
    assert receipt.created and receipt.staged

    # 3. The operator verifies the account on the configuration page and presses
    #    Apply. The import did neither: it never observes an account, and Apply
    #    is the operator's act.
    await service.pin_account(receipt.profile_id, receipt.revision, account_id=LIVE_ACCOUNT)
    service.request_apply(
        expected_selection_generation=service.selection().selection_generation
    )

    # 4. Restart with the retired lines still in place. This is where an
    #    operator who skipped the edit lands, every time -- refused, and told
    #    exactly which lines to delete. The service still boots.
    refused = await resolve_worker_binding(
        service_factory=lambda: service, environment=credential_environment
    )
    assert isinstance(refused, UnboundWorker)
    assert refused.unbound.reason == RETIRED_ENVIRONMENT_SETTINGS
    for name in LEGACY_LIVE_ENVIRONMENT:
        assert name in refused.unbound.next_step
    #    The one-shot Apply is *consumed* by that refusal (ADR 0060 D4.3), so a
    #    later restart -- after someone tidies `.env` for an unrelated reason --
    #    cannot silently apply the change this boot refused. The operator has to
    #    say yes again, which is the point.
    assert service.selection().apply_requested is False
    assert service.selection().last_apply_outcome == "refused"

    # 5. Delete the lines, press Apply again, and restart. Now the worker binds
    #    the profile, and the envelope it binds is the one the environment
    #    described in step 1 -- same values, same sha, so every arming record
    #    sealed before the cutover still verifies against it.
    _delete_retired_lines(monkeypatch)
    reset_alpaca_settings_for_testing()
    service.request_apply(
        expected_selection_generation=service.selection().selection_generation
    )
    after = await resolve_worker_binding(
        service_factory=lambda: service, environment=credential_environment
    )
    assert isinstance(after, BoundWorker)
    assert after.from_profile
    assert after.context.live_envelope is not None
    assert after.context.live_envelope.sha == legacy_sha
    assert after.context.settings.mode == "live"
    #    The lifespan acknowledges once the Clerk holds the account's lease, which
    #    is what consumes the one-shot Apply. Without that step every later
    #    "restart" in this rehearsal would still look like a pending Apply, and
    #    step 6 would be testing step 4 over again.
    acknowledge_worker_binding(
        bound=after, account_id=LIVE_ACCOUNT, service_factory=lambda: service
    )
    assert service.selection().apply_requested is False

    # 6. Rollback-by-half. Restoring the lines without also reverting the code
    #    does not silently revert the configuration: the profile is still what
    #    this code reads, and the restart binds it and says so. Nothing is
    #    pending, so this is a recovery -- and a recovery that refused would
    #    leave a live account with no broker and nothing able to EXIT it
    #    (ADR 0060 D4.3). The lines are reported, loudly, and ignored. A real
    #    rollback reverts the code too, and the profile records are deliberately
    #    kept so it can.
    #
    #    The restored values are deliberately *drifted* from the profile's. This
    #    is the assertion that proves the recovery ignores them: restoring the
    #    identical numbers would satisfy the `sha` check whether the envelope came
    #    from the profile or from the environment, so it would prove nothing. On a
    #    **live** revision these six are the real risk limits, and a leak here
    #    would raise a loss cap on real money.
    drifted = {
        **LEGACY_LIVE_ENVIRONMENT,
        "ALPACA_LIVE_LOSS_USD": "999999",
        "ALPACA_LIVE_ARMING_MAX_SESSIONS": "9999",
    }
    for name, value in drifted.items():
        monkeypatch.setenv(name, value)
    with caplog.at_level(logging.ERROR, logger="app.broker_configuration.worker_binding"):
        rolled_back = await resolve_worker_binding(
            service_factory=lambda: service, environment=credential_environment
        )
    assert isinstance(rolled_back, BoundWorker)
    assert rolled_back.context.live_envelope is not None
    assert rolled_back.context.live_envelope.sha == legacy_sha
    assert rolled_back.context.settings.live_loss_usd == float(
        LEGACY_LIVE_ENVIRONMENT["ALPACA_LIVE_LOSS_USD"]
    )
    assert rolled_back.context.settings.live_arming_max_sessions == int(
        LEGACY_LIVE_ENVIRONMENT["ALPACA_LIVE_ARMING_MAX_SESSIONS"]
    )
    complained = [
        record
        for record in caplog.records
        if getattr(record, "action", None)
        == "worker_binding_retired_environment_recovery_warning"
    ]
    assert len(complained) == 1
    assert complained[0].__dict__["retired_variables"] == list(LEGACY_LIVE_ENVIRONMENT)
    assert service.read_revision(receipt.profile_id, receipt.revision).live_envelope is not None

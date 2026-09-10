"""The whole cutover, start to finish, on an isolated tmp-path installation.

The plan's package-F "done when" asks for a rehearsal demonstrating cutover and
rollback. The *production* rehearsal is a scheduled operator action against a
copy of a real Clerk volume and is explicitly not an implementation agent's to
perform; this is its automated equivalent, and it is the only test that walks
every step in order:

    env-configured boot → import → Apply → restart with the lines still
    present (refused) → lines deleted → bound from the profile → rollback

Two properties fall out of walking it in order rather than testing each step
alone. The refusal at step 4 is *load-bearing to the documented sequence*, not
an edge case — an operator who applies before editing ``.env`` meets it every
time. And the envelope the worker finally binds is asserted equal, by ``sha``,
to the one the environment described at step 1: the cutover moved the numbers
without changing the document every arming record is sealed over.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.active_binding import RETIRED_ENVIRONMENT_SETTINGS
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker_configuration import legacy_environment
from app.broker_configuration.alpaca_seams import AlpacaCredentialSlotDirectory
from app.broker_configuration.legacy_environment import (
    LegacyEnvironmentPresence,
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

    The autouse isolation in ``tests/conftest.py`` is undone here — this test is
    *about* the detector, so it needs the real reader, pinned to the process
    environment (``_env_file=None``) rather than to whatever ``.env`` happens to
    sit beside the test run.
    """
    for name, value in LEGACY_LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALPACA_API_KEY_ID", DEFAULT_SLOT_KEY)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", DEFAULT_SLOT_SECRET)
    monkeypatch.setattr(
        legacy_environment,
        "current_retired_settings",
        lambda: LegacyEnvironmentPresence(_env_file=None),
    )
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

    # 5. Delete the lines and restart. Now the worker binds the profile, and the
    #    envelope it binds is the one the environment described in step 1 --
    #    same values, same sha, so every arming record sealed before the cutover
    #    still verifies against it.
    _delete_retired_lines(monkeypatch)
    reset_alpaca_settings_for_testing()
    after = await resolve_worker_binding(
        service_factory=lambda: service, environment=credential_environment
    )
    assert isinstance(after, BoundWorker)
    assert after.from_profile
    assert after.context.live_envelope is not None
    assert after.context.live_envelope.sha == legacy_sha
    assert after.context.settings.mode == "live"

    # 6. Rollback. Restoring the lines without also reverting the code does not
    #    silently revert the configuration -- it refuses, which is the honest
    #    outcome: the profile is still what this code reads. A real rollback
    #    reverts the code too, and the profile records are deliberately kept.
    for name, value in LEGACY_LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    rolled_back = await resolve_worker_binding(
        service_factory=lambda: service, environment=credential_environment
    )
    assert isinstance(rolled_back, UnboundWorker)
    assert rolled_back.unbound.reason == RETIRED_ENVIRONMENT_SETTINGS
    assert service.read_revision(receipt.profile_id, receipt.revision).live_envelope is not None

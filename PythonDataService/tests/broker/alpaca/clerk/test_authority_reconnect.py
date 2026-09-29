"""A few seconds without Alpaca at startup no longer leaves a clerk down until a restart (#2582).

2026-09-29 04:07 UTC: the Live clerk's startup recovery could not read its
positions ("Could not reach Alpaca while fetching positions."), the selection
failed, nothing retried it, and every account read answered 503 until the
container was restarted by hand. Alpaca had answered again within seconds.

Every test runs the real selector against a broker double whose reads the
test steers; only the backoff's sleep is replaced, and it is recorded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    get_alpaca_clerk,
    get_clerk_runtime,
    install_primary_clerk_runtime,
    register_clerk_runtime,
    reset_alpaca_clerk_for_testing,
    select_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.active_runtime import reconnecting_refusal
from app.broker.alpaca.clerk.authority_reconnect import reconnect_authority, run_authority_reconnect
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerAuthError, BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot, BrokerPosition
from app.routers.broker_v2_panel import read_account_money_scoped
from tests.broker.alpaca.clerk.activation_fixtures import _ActivationStore
from tests.broker.alpaca.clerk.live_authority_fixtures import (
    instance_seals_over,
    live_activation,
    pinned_repository,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.test_active_authority import _activation, _Broker
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import NOW_MS

UNREACHABLE = "Could not reach Alpaca while fetching positions."


@pytest.fixture(autouse=True)
def _no_primary() -> Iterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


class _LiveAlpacaThatBlinks(_LiveBroker):
    """The live account, with Alpaca unreachable for its first ``outage`` positions reads."""

    def __init__(self, *, outage: int, error: Exception | None = None) -> None:
        super().__init__(now_ms=NOW_MS)
        self.outage = outage
        self.error = error or BrokerUnavailable(UNREACHABLE, broker="alpaca", detail="Connection aborted.")

    async def list_positions(self) -> list[BrokerPosition]:
        if self.outage:
            self.outage -= 1
            raise self.error
        return await super().list_positions()


class _PaperAlpacaThatBlinks(_Broker):
    """The paper account, with Alpaca unreachable for its first ``outage`` account reads."""

    def __init__(self, *, outage: int) -> None:
        self.outage = outage

    async def get_account(self) -> BrokerAccountSnapshot:
        if self.outage:
            self.outage -= 1
            raise BrokerUnavailable("Alpaca timed out while fetching account.", broker="alpaca")
        return await super().get_account()


def _live_selection(tmp_path: Path, broker: _LiveBroker) -> Callable[[], Awaitable[ActiveClerkRuntime]]:
    """The composition root's selection for one activated live account, repeatable."""
    repository = ClerkSqliteRepository.initialize(
        account_id=LIVE_ACCT, artifacts_root=tmp_path, clock=lambda: NOW_MS
    )
    meta = repository.control_meta_snapshot()
    repository.close()
    activation = live_activation(
        authority_generation=meta.authority_generation,
        db_identity_token=meta.db_identity_token,
        artifacts_root=tmp_path,
    )

    async def _select() -> ActiveClerkRuntime:
        return await select_active_clerk_runtime(
            read=broker,
            trade=broker,
            artifacts_root=tmp_path,
            activation_store=_ActivationStore(activation),
            repository_opener=pinned_repository(NOW_MS),
            live_envelope_values=TEST_ENVELOPE_VALUES,
            instance_seals=instance_seals_over(tmp_path / "runner"),
        )

    return _select


class _Backoff:
    """The reconnect's sleep, recorded instead of slept."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, delay_s: float) -> None:
        self.waits.append(delay_s)


async def test_a_live_boot_that_briefly_cannot_reach_alpaca_installs_its_authority(
    tmp_path: Path,
) -> None:
    """The incident: the first positions read fails, the second succeeds, authority installs."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))

    at_boot = await select()

    assert at_boot.clerk is None
    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.reason_code == "BROKER_UNREACHABLE_RECONNECTING"
    # The copy is the truth: why, that it is reconnecting, and that nobody needs to act.
    assert UNREACHABLE in at_boot.startup_failure.recovery
    assert "will take over this account on its own" in at_boot.startup_failure.recovery
    assert "no restart is needed" in at_boot.startup_failure.recovery

    backoff = _Backoff()
    installed = await reconnect_authority(at_boot, select=select, sleep=backoff)
    try:
        assert installed.clerk is not None
        assert installed.selected_account_authority_kind == "real_live"
        assert backoff.waits == [2.0]
    finally:
        await installed.close()


async def test_a_longer_outage_backs_off_and_caps_its_wait(tmp_path: Path) -> None:
    """Every attempt that still cannot reach Alpaca doubles the wait, up to its cap."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=6))
    backoff = _Backoff()

    installed = await reconnect_authority(
        await select(), select=select, sleep=backoff, first_delay_s=2.0, max_delay_s=10.0
    )
    try:
        assert installed.clerk is not None
        assert backoff.waits == [2.0, 4.0, 8.0, 10.0, 10.0, 10.0]
    finally:
        await installed.close()


async def test_a_paper_boot_whose_account_read_times_out_reconnects(tmp_path: Path) -> None:
    """The same rule when the very first read, the account's identity, cannot reach Alpaca."""
    repository = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    broker = _PaperAlpacaThatBlinks(outage=1)

    async def _select() -> ActiveClerkRuntime:
        return await select_active_clerk_runtime(
            read=broker,
            trade=broker,
            artifacts_root=tmp_path,
            activation_store=_ActivationStore(_activation()),
            repository_opener=lambda _account_id, _root: repository,
        )

    at_boot = await _select()
    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.reason_code == "BROKER_UNREACHABLE_RECONNECTING"
    assert "Alpaca timed out while fetching account." in at_boot.startup_failure.recovery

    installed = await reconnect_authority(at_boot, select=_select, sleep=_Backoff())
    try:
        assert installed.selected_account_authority_kind == "real_paper"
    finally:
        await installed.close()


async def test_a_refused_credential_is_final_and_never_retried(tmp_path: Path) -> None:
    """Alpaca answered and said no: that is not a blip, and no copy promises a retry."""
    select = _live_selection(
        tmp_path,
        _LiveAlpacaThatBlinks(
            outage=1, error=BrokerAuthError("Alpaca rejected our credentials: forbidden", broker="alpaca")
        ),
    )
    at_boot = await select()
    attempts: list[None] = []

    async def _must_not_reselect() -> ActiveClerkRuntime:
        attempts.append(None)
        return await select()

    final = await reconnect_authority(at_boot, select=_must_not_reselect, sleep=_Backoff())

    assert final is at_boot
    assert attempts == []
    assert final.startup_failure is not None
    assert final.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
    assert "Alpaca rejected our credentials" in final.startup_failure.recovery
    assert "It will not retry on its own; restart the Clerk once that is fixed." in final.startup_failure.recovery
    assert "reconnecting" not in final.startup_failure.recovery


async def test_an_unapproved_account_is_final_and_never_retried(tmp_path: Path) -> None:
    """ACCOUNT_PIN_MISMATCH stays terminal: reaching Alpaca is not what is wrong."""
    broker = _Broker()

    at_boot = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        expected_account_id="PA-APPROVED",
    )

    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.reason_code == "ACCOUNT_PIN_MISMATCH"
    assert at_boot.reconnecting is False


async def test_every_reconnect_attempt_is_logged_and_numbered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=2))
    caplog.set_level(logging.WARNING, logger="app.broker.alpaca.clerk.authority_reconnect")

    installed = await reconnect_authority(await select(), select=select, sleep=_Backoff())
    try:
        scheduled = [
            record for record in caplog.records if getattr(record, "action", None) == "clerk_authority_reconnect_scheduled"
        ]
        assert [record.attempt for record in scheduled] == [1, 2]
        assert all(UNREACHABLE in record.getMessage() for record in scheduled)
        (done,) = [
            record for record in caplog.records if getattr(record, "action", None) == "clerk_authority_reconnected"
        ]
        assert done.attempts == 2
    finally:
        await installed.close()


async def test_the_log_that_broker_truth_was_unreadable_says_why(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The lane's log names the cause, in the message and as a structured field."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    caplog.set_level(logging.WARNING, logger="app.broker.alpaca.clerk.sqlite.reconcile")

    await select()

    (stale,) = [record for record in caplog.records if getattr(record, "action", None) == "reconcile_account_stale"]
    assert UNREACHABLE in stale.getMessage()
    assert stale.error == UNREACHABLE
    assert stale.error_detail == "Connection aborted."


async def test_the_lanes_money_read_says_it_is_reconnecting(tmp_path: Path) -> None:
    """Between attempts the account's reads refuse with the reconnecting copy, not "activate it"."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    set_active_clerk_runtime(await select())

    with pytest.raises(HTTPException) as refused:
        await read_account_money_scoped("alpaca", LIVE_ACCT)

    assert refused.value.status_code == 503
    assert isinstance(refused.value.detail, dict)
    assert UNREACHABLE in refused.value.detail["why"]
    assert "will take over this account on its own once Alpaca answers" in refused.value.detail["why"]


async def test_the_reconnected_authority_leaves_every_dry_run_authority_serving(tmp_path: Path) -> None:
    """Installing the reconnect's authority drops nothing boot recovery registered meanwhile.

    A Dry Run's own authority registered while the lane reconnected keeps its
    execution lease and its running bot; a reset would drop it unclosed.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    unreachable = await select()
    install_primary_clerk_runtime(unreachable)
    dry_run = ActiveClerkRuntime(authority_kind="synthetic", clerk=object(), account_id="sim:dry-1")  # type: ignore[arg-type]
    register_clerk_runtime(dry_run)

    installed = await reconnect_authority(unreachable, select=select, sleep=_Backoff())
    install_primary_clerk_runtime(installed)
    try:
        assert get_alpaca_clerk() is installed.clerk
        assert get_clerk_runtime(LIVE_ACCT) is installed
        assert get_clerk_runtime("sim:dry-1") is dry_run
    finally:
        await installed.close()


class _BootSteps:
    """The composition root's acknowledge, install and boot steps, recorded in order."""

    def __init__(self, *, boot_error: Exception | None = None) -> None:
        self.steps: list[tuple[str, ActiveClerkRuntime]] = []
        self.boot_error = boot_error

    async def acknowledge(self, runtime: ActiveClerkRuntime) -> ActiveClerkRuntime:
        self.steps.append(("acknowledge", runtime))
        return runtime

    def install(self, runtime: ActiveClerkRuntime) -> None:
        self.steps.append(("install", runtime))

    async def boot(self, runtime: ActiveClerkRuntime) -> None:
        self.steps.append(("boot", runtime))
        if self.boot_error is not None:
            raise self.boot_error


async def _run(unreachable: ActiveClerkRuntime, select, steps: _BootSteps) -> None:
    await run_authority_reconnect(
        unreachable,
        select=select,
        acknowledge=steps.acknowledge,
        install=steps.install,
        boot=steps.boot,
        sleep=_Backoff(),
    )


async def test_the_reconnected_authority_is_acknowledged_installed_and_booted(tmp_path: Path) -> None:
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    steps = _BootSteps()

    await _run(await select(), select, steps)

    installed = steps.steps[0][1]
    try:
        assert installed.clerk is not None
        assert [(name, runtime) for name, runtime in steps.steps] == [
            ("acknowledge", installed), ("install", installed), ("boot", installed),
        ]
    finally:
        await installed.close()


async def test_a_reconnect_that_ends_final_installs_the_final_copy_and_boots_nothing(tmp_path: Path) -> None:
    select = _live_selection(
        tmp_path,
        _LiveAlpacaThatBlinks(outage=2, error=BrokerAuthError("Alpaca rejected our credentials", broker="alpaca")),
    )
    unreachable = reconnecting_refusal(BrokerUnavailable(UNREACHABLE, broker="alpaca"), account_id=LIVE_ACCT)
    steps = _BootSteps()

    await _run(unreachable, select, steps)

    assert [name for name, _runtime in steps.steps] == ["acknowledge", "install"]
    final = steps.steps[-1][1].startup_failure
    assert final is not None and "will not retry on its own" in final.recovery


async def test_a_reconnect_that_breaks_stops_promising_a_reconnect(caplog: pytest.LogCaptureFixture) -> None:
    """Never a silent death: logged, and the lane's copy turns final."""
    unreachable = reconnecting_refusal(BrokerUnavailable(UNREACHABLE, broker="alpaca"), account_id=LIVE_ACCT)

    async def _select_breaks() -> ActiveClerkRuntime:
        raise OSError("the activation ledger could not be read")

    steps = _BootSteps()
    caplog.set_level(logging.ERROR, logger="app.broker.alpaca.clerk.authority_reconnect")

    await _run(unreachable, _select_breaks, steps)

    (installed,) = [runtime for name, runtime in steps.steps if name == "install"]
    assert installed.reconnecting is False
    assert installed.startup_failure is not None
    assert installed.startup_failure.reason_code == "CLERK_RECONNECT_FAILED"
    assert "the activation ledger could not be read" in installed.startup_failure.recovery
    assert "will not retry on its own" in installed.startup_failure.recovery
    assert any(getattr(record, "action", None) == "clerk_authority_reconnect_failed" for record in caplog.records)


async def test_a_reconnected_authority_whose_boot_fails_stays_installed_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    steps = _BootSteps(boot_error=RuntimeError("boot recovery could not project a bot"))
    caplog.set_level(logging.ERROR, logger="app.broker.alpaca.clerk.authority_reconnect")

    await _run(await select(), select, steps)

    installed = steps.steps[0][1]
    try:
        assert [name for name, _runtime in steps.steps] == ["acknowledge", "install", "boot"]
        (logged,) = [r for r in caplog.records if getattr(r, "action", None) == "clerk_authority_reconnect_failed"]
        assert "boot recovery could not project a bot" in logged.getMessage()
    finally:
        await installed.close()


async def test_a_selection_cut_short_by_shutdown_releases_its_execution_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels a reconnect mid-attempt; the lease it took is released, not left to expire."""
    recovering = asyncio.Event()

    async def _recover_until_cancelled(_self: object) -> None:
        recovering.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "app.broker.alpaca.clerk.active_runtime.SqliteAlpacaClerkFacade.recover", _recover_until_cancelled
    )
    attempt = asyncio.create_task(_live_selection(tmp_path, _LiveBroker(now_ms=NOW_MS))())
    await recovering.wait()

    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt

    next_process = ClerkSqliteRepository.open(
        account_id=LIVE_ACCT, artifacts_root=tmp_path, lease_owner="boot:next-process", clock=lambda: NOW_MS
    )
    next_process.close()

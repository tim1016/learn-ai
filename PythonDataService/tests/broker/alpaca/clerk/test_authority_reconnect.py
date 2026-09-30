"""A few seconds without Alpaca at startup no longer leaves a clerk down until a restart (#2582).

2026-09-29 04:07 UTC: the Live clerk's startup recovery could not read its
positions ("Could not reach Alpaca while fetching positions."), the selection
failed, nothing retried it, and every account read answered 503 until the
container was restarted by hand. Alpaca had answered again within seconds.

Every test runs the real selector against a broker double whose reads the
test steers, and the reconnect over the composition root's steps against the
real authority registry; only the backoff's sleep is replaced, and recorded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.broker.alpaca.clerk import authority_reconnect
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    get_active_clerk_runtime,
    get_alpaca_clerk,
    get_clerk_runtime,
    install_primary_clerk_runtime,
    register_clerk_runtime,
    reset_alpaca_clerk_for_testing,
    select_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.active_runtime import (
    ClerkStartupFailure,
    reconnecting_refusal,
    unavailable_runtime,
)
from app.broker.alpaca.clerk.authority_reconnect import (
    RECONNECT_FAILED,
    AuthoritySteps,
    ReconnectCounters,
    run_authority_reconnect,
)
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_MISSING
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import StartupBrokerTruthUnavailable
from app.broker.alpaca.errors import map_api_error
from app.broker.alpaca.fault_injection import _api_error
from app.broker.contract.errors import BrokerAuthError, BrokerError, BrokerUnreachable
from app.broker.contract.models import BrokerAccountSnapshot, BrokerPosition
from app.routers.broker_v2_panel import read_account_money_scoped
from app.schemas.broker_v2_panel import LaneAttentionItem
from app.services.bot_boot_recovery import BootAuthorityPreparationError
from app.services.broker_v2_panel.lane_summary import lane_attention_read, lane_counts
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable
from app.services.sqlite_account_pnl_attribution import sqlite_account_pnl_attribution
from app.services.sqlite_clerk_compat import failed_sqlite_projection
from app.services.sqlite_clerk_transaction_projection import sqlite_transaction_history
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


def _unreachable() -> BrokerUnreachable:
    return BrokerUnreachable(UNREACHABLE, broker="alpaca", detail="Connection aborted.")


@pytest.fixture(autouse=True)
def _no_primary() -> Iterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch: pytest.MonkeyPatch) -> ReconnectCounters:
    counters = ReconnectCounters()
    monkeypatch.setattr(authority_reconnect, "RECONNECT_COUNTERS", counters)
    return counters


class _LiveAlpacaThatBlinks(_LiveBroker):
    """The live account, with Alpaca failing its first ``outage`` positions reads."""

    def __init__(self, *, outage: int, error: BrokerError | None = None) -> None:
        super().__init__(now_ms=NOW_MS)
        self.outage = outage
        self.error = error or _unreachable()

    async def list_positions(self) -> list[BrokerPosition]:
        if self.outage:
            self.outage -= 1
            raise self.error
        return await super().list_positions()


class _PaperAlpacaThatBlinks(_Broker):
    """The paper account, with Alpaca timing out its first ``outage`` account reads."""

    def __init__(self, *, outage: int) -> None:
        self.outage = outage

    async def get_account(self) -> BrokerAccountSnapshot:
        if self.outage:
            self.outage -= 1
            raise BrokerUnreachable("Alpaca timed out while fetching account.", broker="alpaca")
        return await super().get_account()


def _live_selection(
    tmp_path: Path, broker: _LiveBroker, *, startup_recovery_timeout_s: float = 60.0
) -> Callable[[], Awaitable[ActiveClerkRuntime]]:
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
            startup_recovery_timeout_s=startup_recovery_timeout_s,
        )

    return _select


class _Backoff:
    """The reconnect's sleep, recorded instead of slept."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, delay_s: float) -> None:
        self.waits.append(delay_s)


@dataclass
class _CompositionRoot:
    """The composition root's boot steps over the real authority registry.

    Install makes a runtime the lane's primary, exactly as main.py does;
    retire closes it; boot recovery raises what the test queues for each
    authority it boots; acknowledgement raises ``acknowledgement_error``
    when one is set. Every call is recorded.
    """

    select: Callable[[], Awaitable[ActiveClerkRuntime]]
    boot_errors: list[Exception | None] = field(default_factory=list)
    events: list[tuple[str, ActiveClerkRuntime]] = field(default_factory=list)
    acknowledgement_error: Exception | None = None

    async def acknowledge(self, runtime: ActiveClerkRuntime) -> ActiveClerkRuntime:
        self.events.append(("acknowledge", runtime))
        if self.acknowledgement_error is not None:
            raise self.acknowledgement_error
        return runtime

    def install(self, runtime: ActiveClerkRuntime) -> None:
        self.events.append(("install", runtime))
        install_primary_clerk_runtime(runtime)

    async def retire(self, runtime: ActiveClerkRuntime) -> None:
        self.events.append(("retire", runtime))
        await runtime.close()

    async def boot(self, runtime: ActiveClerkRuntime) -> None:
        self.events.append(("boot", runtime))
        if runtime.clerk is not None and self.boot_errors:
            error = self.boot_errors.pop(0)
            if error is not None:
                raise error

    def steps(self) -> AuthoritySteps:
        return AuthoritySteps(
            select=self.select,
            acknowledge=self.acknowledge,
            install=self.install,
            retire=self.retire,
            boot=self.boot,
        )

    def names(self) -> list[str]:
        return [name for name, _runtime in self.events]


async def _reconnect(
    at_boot: ActiveClerkRuntime, root: _CompositionRoot, backoff: _Backoff | None = None, **kwargs: float
) -> ActiveClerkRuntime:
    install_primary_clerk_runtime(at_boot)
    return await run_authority_reconnect(at_boot, steps=root.steps(), sleep=backoff or _Backoff(), **kwargs)


def _next_process_can_take_the_lease(tmp_path: Path) -> None:
    """The account's execution lease is free: another owner opens it at once."""
    ClerkSqliteRepository.open(
        account_id=LIVE_ACCT, artifacts_root=tmp_path, lease_owner="boot:next-process", clock=lambda: NOW_MS
    ).close()


def _boot_cannot_reach_alpaca() -> BootAuthorityPreparationError:
    """Boot recovery's own failure when the Clerk's recover step could not read Alpaca."""
    error = BootAuthorityPreparationError("SQLite boot authority step 'recover' failed")
    error.__cause__ = StartupBrokerTruthUnavailable(_unreachable())
    return error


class _ShutdownAfter(_Backoff):
    """The reconnect's sleep, cut short by shutdown once it has waited ``waits`` times."""

    def __init__(self, waits: int) -> None:
        super().__init__()
        self.remaining = waits

    async def __call__(self, delay_s: float) -> None:
        if not self.remaining:
            raise asyncio.CancelledError
        self.remaining -= 1
        await super().__call__(delay_s)


async def _what_the_owner_sees(refusal: ActiveClerkRuntime, *, activation: ClerkStartupFailure) -> LaneAttentionItem:
    """While ``refusal`` serves: one Home line, and account panels that name the failed authority.

    ``activation`` is the boot's own refusal, which carries the account's
    activation as the selection read it; the refusal serving now must name
    the same generation and database.
    """
    assert get_active_clerk_runtime() is refusal
    assert refusal.startup_failure is not None
    attention = await lane_attention_read()
    assert attention.account_id == LIVE_ACCT
    [line] = attention.items
    projection = failed_sqlite_projection(account_id=LIVE_ACCT, strategy_instance_id=None)
    assert projection is not None
    assert projection.authority_health_reason == refusal.startup_failure.recovery
    assert activation.authority_generation is not None
    assert (projection.authority_generation, projection.db_identity_token) == (
        activation.authority_generation,
        activation.db_identity_token,
    )
    with pytest.raises(ClerkTransactionProjectionUnavailable):
        sqlite_account_pnl_attribution(account_id=LIVE_ACCT, from_ms=NOW_MS - 60_000, to_ms=NOW_MS)
    with pytest.raises(ClerkTransactionProjectionUnavailable):
        sqlite_transaction_history(
            account_id=LIVE_ACCT, limit=10, cursor=None, origin=None,
            lifecycle_state=None, strategy_instance_id=None, run_id=None,
        )
    return line


async def test_a_live_boot_that_briefly_cannot_reach_alpaca_installs_and_boots_its_authority(
    tmp_path: Path,
) -> None:
    """The incident: the first positions read fails, the second succeeds, authority serves."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))

    at_boot = await select()

    assert at_boot.clerk is None
    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.reason_code == "BROKER_UNREACHABLE_RECONNECTING"
    # The copy is the truth: why, that it is reconnecting, and that nobody needs to act.
    assert UNREACHABLE in at_boot.startup_failure.recovery
    assert "will take over this account on its own" in at_boot.startup_failure.recovery
    assert "no restart is needed" in at_boot.startup_failure.recovery

    root = _CompositionRoot(select)
    backoff = _Backoff()
    serving = await _reconnect(at_boot, root, backoff)
    try:
        assert serving.clerk is not None
        assert serving.selected_account_authority_kind == "real_live"
        assert get_active_clerk_runtime() is serving
        assert root.names() == ["acknowledge", "install", "boot"]
        assert backoff.waits == [2.0]
    finally:
        await serving.close()


async def test_a_longer_outage_backs_off_and_caps_its_wait(tmp_path: Path) -> None:
    """Every attempt that still cannot reach Alpaca doubles the wait, up to its cap."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=6))
    backoff = _Backoff()

    serving = await _reconnect(await select(), _CompositionRoot(select), backoff, first_delay_s=2.0, max_delay_s=10.0)
    try:
        assert serving.clerk is not None
        assert backoff.waits == [2.0, 4.0, 8.0, 10.0, 10.0, 10.0]
    finally:
        await serving.close()


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

    serving = await _reconnect(at_boot, _CompositionRoot(_select))
    try:
        assert serving.selected_account_authority_kind == "real_paper"
    finally:
        await serving.close()


@pytest.mark.parametrize("status", [500, 503, 504])
async def test_alpaca_failing_on_its_own_side_reconnects(tmp_path: Path, status: int) -> None:
    """A 5xx is Alpaca not answering yet, exactly like a dropped connection."""
    error = map_api_error(_api_error(status, "internal error"), broker="alpaca")
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1, error=error))

    at_boot = await select()

    assert at_boot.reconnecting is True


@pytest.mark.parametrize("status", [404, 409])
async def test_an_answer_no_mapping_recognized_is_final(tmp_path: Path, status: int) -> None:
    """The ``BrokerUnavailable`` catch-all is a misconfiguration a retry cannot fix: never "no restart"."""
    error = map_api_error(_api_error(status, "not found"), broker="alpaca")
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1, error=error))

    at_boot = await select()

    assert at_boot.reconnecting is False
    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
    assert "It will not retry on its own; restart the Clerk once that is fixed." in at_boot.startup_failure.recovery
    assert "no restart is needed" not in at_boot.startup_failure.recovery


async def test_a_rate_limited_startup_reconnects_after_alpaca_s_own_wait(tmp_path: Path) -> None:
    """HTTP 429 is Alpaca asking for time; the reconnect waits at least the time it asked for."""
    throttled = map_api_error(_api_error(429, "too many requests", headers={"Retry-After": "7"}), broker="alpaca")
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1, error=throttled))
    backoff = _Backoff()

    at_boot = await select()
    assert at_boot.reconnecting is True
    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.retry_after_ms == 7_000

    serving = await _reconnect(at_boot, _CompositionRoot(select), backoff)
    try:
        assert serving.clerk is not None
        assert backoff.waits == [7.0]
    finally:
        await serving.close()


async def test_a_startup_recovery_that_runs_out_of_time_reconnects(tmp_path: Path) -> None:
    """Recovery outrunning its own deadline is Alpaca answering too slowly: retried, not final."""

    class _AlpacaTooSlow(_LiveBroker):
        def __init__(self) -> None:
            super().__init__(now_ms=NOW_MS)
            self.slow = True

        async def list_positions(self) -> list[BrokerPosition]:
            if self.slow:
                self.slow = False
                await asyncio.sleep(1.0)
            return await super().list_positions()

    select = _live_selection(tmp_path, _AlpacaTooSlow(), startup_recovery_timeout_s=0.05)

    at_boot = await select()

    assert at_boot.reconnecting is True
    assert at_boot.startup_failure is not None
    assert "did not finish within 0.05 seconds" in at_boot.startup_failure.recovery
    _next_process_can_take_the_lease(tmp_path)


async def test_a_refused_credential_is_final_and_never_retried(tmp_path: Path) -> None:
    """Alpaca answered and said no: that is not a blip, and no copy promises a retry."""
    select = _live_selection(
        tmp_path,
        _LiveAlpacaThatBlinks(
            outage=1, error=BrokerAuthError("Alpaca rejected our credentials: forbidden", broker="alpaca")
        ),
    )

    at_boot = await select()

    assert at_boot.reconnecting is False
    assert at_boot.startup_failure is not None
    assert at_boot.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
    assert "Alpaca rejected our credentials" in at_boot.startup_failure.recovery
    assert "It will not retry on its own; restart the Clerk once that is fixed." in at_boot.startup_failure.recovery
    assert "reconnecting" not in at_boot.startup_failure.recovery


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


async def test_every_reconnect_attempt_is_logged_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, _fresh_counters: ReconnectCounters
) -> None:
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=2))
    caplog.set_level(logging.INFO, logger="app.broker.alpaca.clerk.authority_reconnect")

    serving = await _reconnect(await select(), _CompositionRoot(select))
    try:
        scheduled = [
            record for record in caplog.records if getattr(record, "action", None) == "clerk_authority_reconnect_scheduled"
        ]
        assert [record.attempt for record in scheduled] == [1, 2]
        assert all(UNREACHABLE in record.getMessage() for record in scheduled)
        # Counted, not only logged: two attempts, the first still unreachable.
        assert _fresh_counters == ReconnectCounters(attempts=2, still_unreachable=1, installed=1, final=0)
        (done,) = [
            record for record in caplog.records if getattr(record, "action", None) == "clerk_authority_reconnected"
        ]
        assert done.attempts == 2
        # Success is news, not a warning -- and it is logged only once the authority serves.
        assert done.levelno == logging.INFO
    finally:
        await serving.close()


async def test_a_reconnect_the_binding_refuses_is_never_logged_as_serving(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, _fresh_counters: ReconnectCounters
) -> None:
    """Acknowledgement can still refuse what the attempt selected; success is not claimed first."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    at_boot = await select()
    root = _CompositionRoot(select)

    async def _refuse(runtime: ActiveClerkRuntime) -> ActiveClerkRuntime:
        root.events.append(("acknowledge", runtime))
        await runtime.close()
        return unavailable_runtime(
            "WORKER_BINDING_REFUSED", account_id=LIVE_ACCT, recovery="The applied binding changed; restart the Clerk."
        )

    root.acknowledge = _refuse  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="app.broker.alpaca.clerk.authority_reconnect")

    ended = await _reconnect(at_boot, root)

    assert ended.clerk is None
    assert root.names() == ["acknowledge", "install", "boot"]
    assert not [record for record in caplog.records if getattr(record, "action", None) == "clerk_authority_reconnected"]
    assert _fresh_counters.final == 1


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


@pytest.mark.parametrize(
    "reason_code", ["ACTIVATION_REQUIRED", "LIVE_ENVELOPE_MISSING", "DEVELOPER_RESET_REACTIVATION_REQUIRED"]
)
async def test_an_account_awaiting_activation_still_says_activate_it(reason_code: str) -> None:
    """Only a reconnecting authority speaks for itself on the money read; the rest keep their copy.

    An authority that never started for want of the owner's step must not
    show internal recovery text in its place.
    """
    set_active_clerk_runtime(
        unavailable_runtime(
            reason_code,
            account_id=None,
            recovery="Complete the supervised SQLite Clerk cutover and activation before starting Alpaca custody.",
        )
    )

    with pytest.raises(HTTPException) as refused:
        await read_account_money_scoped("alpaca", LIVE_ACCT)

    assert isinstance(refused.value.detail, dict)
    assert refused.value.detail["why"] == "This account's custody authority is unavailable. Activate it in Settings."


async def test_the_reconnected_authority_leaves_every_dry_run_authority_serving(tmp_path: Path) -> None:
    """Installing the reconnect's authority drops nothing registered meanwhile.

    A Dry Run's own authority registered while the lane reconnected keeps its
    execution lease and its running bot; a reset would drop it unclosed.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    unreachable = await select()
    install_primary_clerk_runtime(unreachable)
    dry_run = ActiveClerkRuntime(authority_kind="synthetic", clerk=object(), account_id="sim:dry-1")  # type: ignore[arg-type]
    register_clerk_runtime(dry_run)

    serving = await run_authority_reconnect(unreachable, steps=_CompositionRoot(select).steps(), sleep=_Backoff())
    try:
        assert get_alpaca_clerk() is serving.clerk
        assert get_clerk_runtime(LIVE_ACCT) is serving
        assert get_clerk_runtime("sim:dry-1") is dry_run
    finally:
        await serving.close()


async def test_a_reconnected_authority_whose_boot_cannot_reach_alpaca_is_retired_and_retried(
    tmp_path: Path, _fresh_counters: ReconnectCounters
) -> None:
    """One attempt is select-to-boot: Alpaca failing boot recovery retires the authority and reconnects.

    Left installed, the authority would serve with no reconciliation sweep, no
    lane-quiet probe and Start waiting on a boot recovery that never reran.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    root = _CompositionRoot(select, boot_errors=[_boot_cannot_reach_alpaca()])
    backoff = _Backoff()

    serving = await _reconnect(await select(), root, backoff)
    try:
        assert root.names() == [
            "acknowledge", "install", "boot",  # attempt 1: boot recovery cannot reach Alpaca
            "install", "retire", "boot",  # its refusal replaces it before it closes
            "acknowledge", "install", "boot",  # attempt 2 serves
        ]
        first, refusal = root.events[1][1], root.events[3][1]
        assert root.events[4] == ("retire", first)
        assert refusal.reconnecting is True
        assert refusal.startup_failure is not None
        assert UNREACHABLE in refusal.startup_failure.recovery
        # Boot recovery runs for the reconnecting refusal too, so Start reads a finished report.
        assert root.events[5] == ("boot", refusal)
        assert serving.clerk is not None
        assert get_active_clerk_runtime() is serving
        assert backoff.waits == [2.0, 4.0]
        assert _fresh_counters.still_unreachable == 1
    finally:
        await serving.close()


async def test_a_reconnected_authority_whose_boot_fails_otherwise_is_retired_and_final(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Any other boot failure retires the authority and installs a final refusal that says restart.

    The refusal is the one a failed boot composition installs, for the same
    activation: Home keeps a blocking line and the account panels keep
    naming the failed authority instead of going quiet.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    at_boot = await select()
    assert at_boot.startup_failure is not None
    root = _CompositionRoot(select, boot_errors=[RuntimeError("boot recovery could not project a bot")])
    caplog.set_level(logging.ERROR, logger="app.broker.alpaca.clerk.authority_reconnect")

    final = await _reconnect(at_boot, root)

    assert root.names() == ["acknowledge", "install", "boot", "install", "retire", "boot"]
    retired = root.events[4][1]
    assert retired is root.events[1][1]
    # Retired means closed: its execution lease is free for the next process.
    _next_process_can_take_the_lease(tmp_path)
    assert final.reconnecting is False
    assert final.startup_failure is not None
    line = await _what_the_owner_sees(final, activation=at_boot.startup_failure)
    assert (line.condition_id, line.severity) == ("account:authority-failed:SQLITE_CLERK_STARTUP_FAILED", "blocking")
    assert "boot recovery could not project a bot" in final.startup_failure.recovery
    assert "restart the Clerk once that is fixed" in final.startup_failure.recovery
    assert root.events[-1] == ("boot", final)
    (logged,) = [r for r in caplog.records if getattr(r, "action", None) == "clerk_authority_reconnect_boot_failed"]
    assert "boot recovery could not project a bot" in logged.getMessage()


async def test_a_control_meta_read_that_raises_retires_the_authority_it_just_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2620: the identity read sat outside the composition guard, so its own
    failure escaped to the catch-all, which installed the final refusal
    without retiring the authority the attempt had just installed — its
    execution lease and consumer stayed open. It is a failed composition like
    any other: retire, then refuse."""
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    at_boot = await select()
    assert at_boot.startup_failure is not None

    # Arm the failure only once selection has answered, so the attempt's own
    # identity read is the one that raises — selection reads the snapshot too.
    armed = False
    real_snapshot = ClerkSqliteRepository.control_meta_snapshot

    def unreadable(self: ClerkSqliteRepository) -> object:
        if armed:
            raise RuntimeError("control meta could not be read")
        return real_snapshot(self)

    monkeypatch.setattr(ClerkSqliteRepository, "control_meta_snapshot", unreadable)

    async def select_then_arm() -> ActiveClerkRuntime:
        nonlocal armed
        runtime = await select()
        armed = True
        return runtime

    root = _CompositionRoot(select_then_arm)

    final = await _reconnect(at_boot, root)

    assert root.names() == ["acknowledge", "install", "install", "retire", "boot"]
    assert root.events[3] == ("retire", root.events[1][1])  # the installed authority is closed
    _next_process_can_take_the_lease(tmp_path)  # retired means its lease is free
    assert final.reconnecting is False
    assert final.startup_failure is not None
    assert final.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
    assert final.startup_failure.account_id == LIVE_ACCT
    assert final.startup_failure.authority_generation is None  # nothing invents an unread identity
    assert final.startup_failure.db_identity_token is None
    assert "control meta could not be read" in final.startup_failure.recovery
    assert root.events[-1] == ("boot", final)


async def test_an_install_that_raises_retires_the_authority_it_half_installed(tmp_path: Path) -> None:
    """#2620 review: the install sat outside the composition guard too.

    An install that raised after making the authority primary -- its
    trade-updates consumer or a tap failing to start -- escaped to the
    catch-all, which installed the final refusal without retiring it: its
    execution lease stayed held against the next owner.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    at_boot = await select()
    root = _CompositionRoot(select)
    install = root.install

    def install_breaks_once_primary(runtime: ActiveClerkRuntime) -> None:
        install(runtime)
        if runtime.clerk is not None:
            raise RuntimeError("the trade-updates consumer would not start")

    root.install = install_breaks_once_primary  # type: ignore[method-assign]

    final = await _reconnect(at_boot, root)

    assert root.names() == ["acknowledge", "install", "install", "retire", "boot"]
    assert root.events[3] == ("retire", root.events[1][1])  # the half-installed authority is closed
    _next_process_can_take_the_lease(tmp_path)
    assert get_active_clerk_runtime() is final
    assert final.startup_failure is not None
    assert final.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
    assert "the trade-updates consumer would not start" in final.startup_failure.recovery


async def test_a_retired_authority_reconnecting_keeps_its_line_on_home_and_its_panels(tmp_path: Path) -> None:
    """Between its retirement and the next attempt the account is reconnecting, never gone from Home.

    Shutdown ends the reconnect during that wait, so the refusal the
    retirement installed is what the lane serves.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    at_boot = await select()
    assert at_boot.startup_failure is not None
    root = _CompositionRoot(select, boot_errors=[_boot_cannot_reach_alpaca()])

    with pytest.raises(asyncio.CancelledError):
        await _reconnect(at_boot, root, _ShutdownAfter(1))

    assert root.names() == ["acknowledge", "install", "boot", "install", "retire", "boot"]
    reconnecting = root.events[3][1]
    assert reconnecting.reconnecting is True
    line = await _what_the_owner_sees(reconnecting, activation=at_boot.startup_failure)
    assert (line.condition_id, line.severity) == ("account:authority-reconnecting", "warning")
    assert line.action.destination == "settings"  # order records answer 503 while reconnecting (#2620)
    _next_process_can_take_the_lease(tmp_path)


async def test_a_reconnecting_lane_whose_account_read_failed_keeps_a_home_line() -> None:
    """#2620: the account-identity read failing is the most common outage
    shape, and its refusal carries no activation evidence — Home showed
    nothing at all. The reconnecting line shows anyway."""
    set_active_clerk_runtime(reconnecting_refusal(_unreachable(), account_id=None))

    attention = await lane_attention_read()

    [line] = attention.items
    assert line.condition_id == "account:authority-reconnecting"
    assert line.severity == "warning"
    assert line.action.destination == "settings"


async def test_a_reconnect_that_ended_final_keeps_a_home_line_without_activation_evidence() -> None:
    """#2620: the catch-all copies the activation evidence of the refusal the
    attempt started from, so a reconnect that began at the account read ends
    final with none — and Home used to go silent on that path too."""
    set_active_clerk_runtime(unavailable_runtime(
        RECONNECT_FAILED,
        account_id=None,
        recovery="Its reconnect to Alpaca failed. Restart the Clerk once that is fixed.",
    ))

    attention = await lane_attention_read()

    [line] = attention.items
    assert line.condition_id == "account:authority-failed:CLERK_RECONNECT_FAILED"
    assert line.severity == "blocking"
    # The lane has no repository, so order records answer 503; Settings loads.
    assert line.action.destination == "settings"


@pytest.mark.parametrize(
    "refusal",
    [
        BrokerAuthError("Alpaca rejected our credentials: forbidden", broker="alpaca"),
        map_api_error(_api_error(404, "not found"), broker="alpaca"),
    ],
    ids=["revoked-key", "unmapped-answer"],
)
async def test_a_reconnect_that_ends_on_a_refused_account_read_keeps_a_home_line(
    tmp_path: Path, refusal: BrokerError
) -> None:
    """#2620 review: Alpaca came back and refused the account read itself.

    That refusal is final and carries no activation evidence -- no account
    was identified -- so Home went from the reconnecting line to nothing,
    while the lane serves no authority until a restart.
    """

    class _RefusesOnceItAnswers(_PaperAlpacaThatBlinks):
        async def get_account(self) -> BrokerAccountSnapshot:
            if self.outage:
                return await super().get_account()  # times out, counting the outage down
            raise refusal

    broker = _RefusesOnceItAnswers(outage=1)

    async def _select() -> ActiveClerkRuntime:
        return await select_active_clerk_runtime(
            read=broker, trade=broker, artifacts_root=tmp_path, activation_store=_ActivationStore(_activation()),
        )

    at_boot = await _select()
    assert at_boot.reconnecting is True

    final = await _reconnect(at_boot, _CompositionRoot(_select))

    assert final.reconnecting is False
    assert final.startup_failure is not None
    assert final.startup_failure.activation_detected is False
    [line] = (await lane_attention_read()).items
    assert (line.condition_id, line.severity) == (
        f"account:authority-failed:{final.startup_failure.reason_code}", "blocking",
    )
    assert line.action.destination == "settings"
    # No account was identified, yet the account card counts the bell's line.
    assert (await lane_counts()).attention_count == 1


@pytest.mark.parametrize(
    "reason_code",
    [
        "ACTIVATION_REQUIRED",
        "SHADOW_ACTIVATION_REQUIRED",
        "SYNTHETIC_ACTIVATION_REQUIRED",
        LIVE_ENVELOPE_MISSING,
        "DEVELOPER_RESET_REACTIVATION_REQUIRED",
    ],
)
async def test_an_account_awaiting_activation_has_no_home_line(reason_code: str) -> None:
    """The owner's own step still to take is not a failure, whatever evidence it carries (#2620).

    Home names every authority that is not serving except these: Settings
    is where activation is taken, and says so.
    """
    set_active_clerk_runtime(unavailable_runtime(
        reason_code, account_id=LIVE_ACCT, recovery="Activate this account in Settings.", activation_detected=True,
    ))

    attention = await lane_attention_read()

    assert attention.items == []


async def test_a_reconnect_that_breaks_stops_promising_a_reconnect(
    caplog: pytest.LogCaptureFixture, _fresh_counters: ReconnectCounters
) -> None:
    """Never a silent death: logged, counted, and the lane's copy turns final."""
    unreachable = reconnecting_refusal(_unreachable(), account_id=LIVE_ACCT)

    async def _select_breaks() -> ActiveClerkRuntime:
        raise OSError("the activation ledger could not be read")

    root = _CompositionRoot(_select_breaks)
    caplog.set_level(logging.ERROR, logger="app.broker.alpaca.clerk.authority_reconnect")

    final = await _reconnect(unreachable, root)

    # Acknowledged like every refusal an attempt installs (#2620: a pending
    # Apply is recorded as refused), then installed; boot recovery runs for
    # the final refusal too, so Start reads a finished report.
    assert root.names() == ["acknowledge", "install", "boot"]
    assert root.events[0] == ("acknowledge", final)
    assert root.events[-1] == ("boot", final)
    assert get_active_clerk_runtime() is final
    assert final.reconnecting is False
    assert final.startup_failure is not None
    assert final.startup_failure.reason_code == "CLERK_RECONNECT_FAILED"
    assert "the activation ledger could not be read" in final.startup_failure.recovery
    assert "will not retry on its own" in final.startup_failure.recovery
    assert any(getattr(record, "action", None) == "clerk_authority_reconnect_failed" for record in caplog.records)
    assert _fresh_counters.final == 1


async def test_a_reconnect_that_breaks_returns_the_refusal_it_installed_and_booted(
    _fresh_counters: ReconnectCounters,
) -> None:
    """Acknowledgement can replace the refusal it was handed (#2620 review).

    ``acknowledge_runtime_binding`` answers a fresh refusal of its own when
    recording the binding's refusal breaks the arming ledger's rules; the
    runtime the reconnect returns must be the one it installed and booted --
    the one the lane serves -- not the one it composed.
    """
    unreachable = reconnecting_refusal(_unreachable(), account_id=LIVE_ACCT)

    async def _select_breaks() -> ActiveClerkRuntime:
        raise OSError("the activation ledger could not be read")

    root = _CompositionRoot(_select_breaks)
    replacement = unavailable_runtime(
        "LIVE_ARMING_LEDGER_INVALID",
        account_id=LIVE_ACCT,
        recovery="The configuration arming evidence could not be read; restore it and restart.",
    )

    async def _acknowledge_replaces(runtime: ActiveClerkRuntime) -> ActiveClerkRuntime:
        root.events.append(("acknowledge", runtime))
        return replacement

    root.acknowledge = _acknowledge_replaces  # type: ignore[method-assign]

    returned = await _reconnect(unreachable, root)

    assert root.names() == ["acknowledge", "install", "boot"]
    assert root.events[1] == ("install", replacement)
    assert root.events[-1] == ("boot", replacement)
    assert get_active_clerk_runtime() is replacement
    assert returned is replacement


async def test_a_reconnect_that_breaks_while_retiring_boots_its_final_refusal_and_keeps_home_s_line(
    tmp_path: Path,
) -> None:
    """Retirement raising after a failed boot recovery already cleared Start's report.

    The final refusal is booted, so Start reads a finished report instead of
    waiting forever on a sweep nothing reruns, and it keeps the account's
    activation, so Home and the panels keep naming the failed authority.
    """
    select = _live_selection(tmp_path, _LiveAlpacaThatBlinks(outage=1))
    at_boot = await select()
    assert at_boot.startup_failure is not None
    root = _CompositionRoot(select, boot_errors=[RuntimeError("boot recovery could not project a bot")])

    async def _retire_breaks(runtime: ActiveClerkRuntime) -> None:
        root.events.append(("retire", runtime))
        await runtime.close()
        raise OSError("the trade-updates consumer would not stop")

    root.retire = _retire_breaks  # type: ignore[method-assign]

    final = await _reconnect(at_boot, root)

    assert root.names() == [
        "acknowledge", "install", "boot",  # the failed boot recovery
        "install", "retire",  # its refusal replaces the authority before it closes
        "acknowledge", "install", "boot",  # the catch-all's final refusal, acknowledged (#2620)
    ]
    assert root.events[-1] == ("boot", final)
    assert final.startup_failure is not None
    assert "the trade-updates consumer would not stop" in final.startup_failure.recovery
    assert "restart the Clerk once that is fixed" in final.startup_failure.recovery
    line = await _what_the_owner_sees(final, activation=at_boot.startup_failure)
    assert (line.condition_id, line.severity) == ("account:authority-failed:CLERK_RECONNECT_FAILED", "blocking")


async def test_a_final_refusal_whose_acknowledgement_fails_is_still_installed_and_booted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second failure acknowledging the final refusal never leaves the lane serving nothing.

    It is logged with its cause, then the refusal is installed
    unacknowledged and boot-recovered like any other (#2620).
    """

    async def _select_breaks() -> ActiveClerkRuntime:
        raise OSError("the activation ledger could not be read")

    root = _CompositionRoot(
        _select_breaks, acknowledgement_error=RuntimeError("the binding receipt could not be written")
    )
    caplog.set_level(logging.ERROR, logger="app.broker.alpaca.clerk.authority_reconnect")

    final = await _reconnect(reconnecting_refusal(_unreachable(), account_id=LIVE_ACCT), root)

    assert root.names() == ["acknowledge", "install", "boot"]
    assert root.events[1:] == [("install", final), ("boot", final)]
    assert get_active_clerk_runtime() is final
    assert final.startup_failure is not None
    assert final.startup_failure.reason_code == RECONNECT_FAILED
    (logged,) = [
        record for record in caplog.records
        if getattr(record, "action", None) == "clerk_authority_reconnect_final_ack_failed"
    ]
    assert logged.exc_info is not None
    assert str(logged.exc_info[1]) == "the binding receipt could not be written"


async def test_a_final_refusal_whose_boot_recovery_also_fails_ends_the_reconnect_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown awaits the reconnect before custody comes down; it must not raise there."""

    async def _select_breaks() -> ActiveClerkRuntime:
        raise OSError("the activation ledger could not be read")

    root = _CompositionRoot(_select_breaks)

    async def _boot_breaks(runtime: ActiveClerkRuntime) -> None:
        root.events.append(("boot", runtime))
        raise OSError("a bot's lifecycle record could not be read")

    root.boot = _boot_breaks  # type: ignore[method-assign]
    caplog.set_level(logging.ERROR, logger="app.broker.alpaca.clerk.authority_reconnect")

    final = await _reconnect(reconnecting_refusal(_unreachable(), account_id=LIVE_ACCT), root)

    assert root.names() == ["acknowledge", "install", "boot"]
    assert get_active_clerk_runtime() is final
    assert final.reconnecting is False
    (logged,) = [
        record for record in caplog.records
        if getattr(record, "action", None) == "clerk_authority_reconnect_final_boot_failed"
    ]
    assert logged.exc_info is not None
    assert str(logged.exc_info[1]) == "a bot's lifecycle record could not be read"


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

    _next_process_can_take_the_lease(tmp_path)

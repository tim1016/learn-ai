"""The reconciliation sweep settles a dead bot's run, no restart (#2589).

A dead bot whose last run never settled used to wait for a Clerk restart
(boot recovery) or a lease revival before Clear would take it; one sealed on
a ``shadow:`` store after its live account graduated was never settled at
all. Now, after every sweep pass, the runner re-reads its own bots and gives
each one whose runner is gone -- and whose run its Clerk has closed -- the boot
scan's own repair. Level-triggered: whichever reconcile closed the run, and
however often a settle failed, the next pass finds it again. A shadow-sealed
bot is settled through its own store, never the installed authority (ADR
0050).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import get_alpaca_clerk, set_alpaca_clerk
from app.broker.alpaca.clerk.active_authority import (
    activate_shadow_clerk_authority,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.run_ownership import NO_RUNNER_REASON
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjector
from app.services.bot_runner import BotTaskRegistry
from app.services.bot_runner_errors import BotRunnerError
from tests._helpers.bot_runner.custody import admission_guard_for
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.test_active_authority import _LiveBroker
from tests.services.test_bot_runner_ema_resume import (
    _STRATEGY_INSTANCE_ID,
    _first_resumed_bar,
    _FlatBroker,
    _ResumeFeed,
    _tradable_market_liveness,
    _wait_for,
)

_SID = _STRATEGY_INSTANCE_ID


def _account(
    account_id: str, clerk_root: Path
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker]:
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=clerk_root)
    broker = _FlatBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    return repo, clerk, broker


def _compose(
    tmp_path: Path, feed: _ResumeFeed, *, account_id: str, clerk_root: Path
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker, BotTaskRegistry]:
    repo, clerk, broker = _account(account_id, clerk_root)
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        boot_recovery_required=False,
        start_custody_guard=admission_guard_for(clerk),
        market_liveness=_tradable_market_liveness,
    )
    return repo, clerk, broker, registry


def _sweep(
    repo: ClerkSqliteRepository,
    clerk: SqliteAlpacaClerkFacade,
    broker: _FlatBroker,
    registry: BotTaskRegistry,
    *,
    passes: int = 1,
) -> ReconciliationSweep:
    """The account's sweep, wired to the runner the way ``main.py`` wires it."""
    return ReconciliationSweep(
        repo=repo,
        read=broker,
        trade=broker,
        intake=clerk.intake,
        run_ownership=clerk.run_ownership,
        max_passes=passes,
        sleep=lambda _delay: asyncio.sleep(0),
        pricing=UNPRICEABLE_RECOVERY,
        on_duty_settle=registry.settle_dead_runs,
    )


def _current_clerk_guard():
    """A start guard that resolves the *installed* Clerk at call time.

    The production default reads the installed authority lazily; a guard
    closed over one Clerk would pin the rehearsal authority past its
    graduation.
    """

    @asynccontextmanager
    async def guard(sid: str) -> AsyncIterator[object]:
        clerk = get_alpaca_clerk()
        assert clerk is not None
        async with clerk.start_admission_snapshot(sid) as snapshot:
            yield snapshot, clerk.program_leg_policy, clerk.exit_terms_for_instance(sid)

    return guard


async def _deploy(registry: BotTaskRegistry) -> None:
    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
    )


async def _crash_without_settling(
    monkeypatch: pytest.MonkeyPatch, clerk: SqliteAlpacaClerkFacade, registry: BotTaskRegistry
) -> None:
    """End the supervise task with its terminal commit failing: the run stays
    ACTIVE in the Clerk and the lifecycle record stays ON_DUTY."""

    async def _stop_commit_fails(**_kwargs: object) -> None:
        raise RuntimeError("SQLite STOP commit failed")

    monkeypatch.setattr(clerk, "stop_strategy_run", _stop_commit_fails)
    await _deploy(registry)
    await _wait_for(lambda: not registry.any_running())
    # Let the task's done-callbacks run.
    await asyncio.sleep(0)
    assert registry.status("alpaca", _SID).phase == "ON_DUTY"


def _lifecycle(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "runner" / "live_state" / _SID / "lifecycle_state.json").read_text(encoding="utf-8")
    )


def _assert_settled_as_runner_gone(tmp_path: Path) -> None:
    record = _lifecycle(tmp_path)
    assert record["phase"] == "OFF_DUTY"
    assert record["duty_outcome"]["kind"] == "EXITED_UNVERIFIED"
    assert record["duty_outcome"]["reason_code"] == "INTERRUPTED_BY_RUNNER_GONE"


def _dead_bot(
    tmp_path: Path,
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker, BotTaskRegistry]:
    """A paper account and a bot whose feed dies right after its first bar."""
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))
    return _compose(tmp_path, feed, account_id="PA-TEST", clerk_root=tmp_path / "clerk")


@pytest.mark.asyncio
async def test_one_sweep_pass_settles_a_dead_bot_so_clear_needs_no_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass retires the runner-less run (#2369) and the runner settles its
    duty record right after: OFF_DUTY with interrupted evidence, the desired
    state STOPPED, and Clear takes the bot off the roster. No restart
    happened; the registry never left this process."""
    repo, clerk, broker, registry = _dead_bot(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        assert repo.active_run(_SID) is not None

        await _sweep(repo, clerk, broker, registry).run()

        assert repo.active_run(_SID) is None
        _assert_settled_as_runner_gone(tmp_path)
        assert registry.desired_state(_SID).value == "STOPPED"

        await registry.archive("alpaca", _SID, updated_by="operator")

        assert registry.status("alpaca", _SID).phase == "RETIRED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_run_another_reconcile_closed_is_settled_by_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clear first (P1-1): Clear's own batch pass -- like Start's admission and
    Reconcile now -- also retires a run whose runner is gone, before any sweep
    pass sees it. The bot is refused as not yet settled, the next pass settles
    it anyway, and Clear then takes it with no restart."""
    repo, clerk, broker, registry = _dead_bot(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        await clerk.reconcile_through()  # Clear's batch pass comes first
        assert repo.active_run(_SID) is None
        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")
        assert refused.value.reason_code == "BOT_DUTY_NOT_SETTLED"

        await _sweep(repo, clerk, broker, registry).run()

        _assert_settled_as_runner_gone(tmp_path)
        await registry.archive("alpaca", _SID, updated_by="operator")
        assert registry.status("alpaca", _SID).phase == "RETIRED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_settle_that_failed_is_tried_again_on_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2: the first settle's write fails; nothing remembers that, and nothing
    has to -- the next pass reads the same on-duty record and settles it."""
    repo, clerk, broker, registry = _dead_bot(tmp_path)
    real_project_terminal = AlpacaLifecycleProjector.project_terminal
    attempts = {"settle": 0}

    def _first_write_fails(self: AlpacaLifecycleProjector, **kwargs: object):
        if kwargs.get("strategy_instance_id") == _SID and kwargs.get("updated_by") == "bot_runner_duty_settle":
            attempts["settle"] += 1
            if attempts["settle"] == 1:
                raise OSError("transient lifecycle write failure")
        return real_project_terminal(self, **kwargs)

    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        monkeypatch.setattr(AlpacaLifecycleProjector, "project_terminal", _first_write_fails)

        await _sweep(repo, clerk, broker, registry, passes=2).run()

        assert attempts["settle"] == 2
        _assert_settled_as_runner_gone(tmp_path)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_running_bot_is_never_settled_even_when_its_clerk_closed_the_run(
    tmp_path: Path,
) -> None:
    """P1-4: the one thing standing between this bot and a settle is its live
    task. Its Clerk already retired the run (``NO_RUNNER``), so nothing in the
    Clerk would refuse a terminal projection; the runner's own liveness must.
    The duty record stays ON_DUTY with no outcome."""
    feed = _ResumeFeed()  # no bars: the stream stays open, the bot keeps running
    repo, clerk, broker, registry = _compose(
        tmp_path, feed, account_id="PA-TEST", clerk_root=tmp_path / "clerk"
    )
    set_alpaca_clerk(clerk)
    try:
        await _deploy(registry)
        run = repo.active_run(_SID)
        assert run is not None
        submit_stop_run(
            repo,
            account_id="PA-TEST",
            strategy_instance_id=_SID,
            lifecycle_run_id=run.lifecycle_run_id,
            operator_reason=NO_RUNNER_REASON,
            clock=repo.clock,
        )
        assert repo.active_run(_SID) is None

        await _sweep(repo, clerk, broker, registry).run()

        assert registry.status("alpaca", _SID).running is True
        record = _lifecycle(tmp_path)
        assert (record["phase"], record["duty_outcome"]) == ("ON_DUTY", None)
    finally:
        managed = registry._bots.get(_SID)
        if managed is not None:
            managed.task.cancel()
            await asyncio.gather(managed.task, return_exceptions=True)
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_shadow_sealed_dead_bot_is_settled_through_its_own_store_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-3: a rehearsal bot sealed on ``shadow:<live_account>`` after
    graduation. Its run lives in that store, which the installed live
    authority never reads -- and must never write duty state for (ADR 0050).
    The live account's own sweep passes settle it anyway: through the sealed
    store, whose dead run is closed there first. Clear still refuses it, in
    words that say why, rather than promising a clear that never comes."""
    live_account_id = "9LIVE0001"
    sealed_account_id = f"shadow:{live_account_id}"
    runner_root = tmp_path / "runner"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))

    # The rehearsal: the shadow store is the installed authority, so the run
    # is admitted into it and the binding is sealed on it.
    await activate_shadow_clerk_authority(live_account_id=live_account_id, artifacts_root=runner_root)
    runtime = await select_active_clerk_runtime(
        read=_LiveBroker(),
        trade=_LiveBroker(),
        artifacts_root=runner_root,
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )
    assert runtime.clerk is not None
    registry = BotTaskRegistry(
        runner_root,
        feed_resolver=lambda: feed,
        boot_recovery_required=False,
        start_custody_guard=_current_clerk_guard(),
        market_liveness=_tradable_market_liveness,
    )
    set_alpaca_clerk(runtime.clerk)
    try:
        await _crash_without_settling(monkeypatch, runtime.clerk, registry)
        assert runtime.sqlite_repository.active_run(_SID) is not None
    finally:
        set_alpaca_clerk(None)
        await runtime.close()
    assert registry.binding_for_control("alpaca", _SID).sealed_account_id == sealed_account_id

    # Graduation: the installed authority is now the live account's own.
    live_repo, live_clerk, live_broker = _account("PA-LIVE-2", tmp_path / "clerk-live")
    installed_writes: list[str] = []
    for method in ("project_terminal", "refresh"):
        real = getattr(registry._lifecycle_projector, method)

        def _spy(*args: object, _real=real, _method=method, **kwargs: object):
            installed_writes.append(f"{_method}:{kwargs.get('strategy_instance_id')}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(registry._lifecycle_projector, method, _spy)

    set_alpaca_clerk(live_clerk)
    try:
        assert registry.status("alpaca", _SID).phase == "ON_DUTY"

        await _sweep(live_repo, live_clerk, live_broker, registry, passes=2).run()

        _assert_settled_as_runner_gone(tmp_path)
        assert installed_writes == []
        assert live_repo.strategy_instances() == []
        sealed = ClerkSqliteRepository.open(account_id=sealed_account_id, artifacts_root=runner_root)
        try:
            assert sealed.active_run(_SID) is None
        finally:
            sealed.close()

        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")
        assert refused.value.reason_code == "ARCHIVE_SEALED_ACCOUNT_CUSTODY"
        assert str(refused.value) == "This bot's account is no longer managed here."
        assert registry.status("alpaca", _SID).phase == "OFF_DUTY"
    finally:
        set_alpaca_clerk(None)
        live_repo.close()


@pytest.mark.asyncio
async def test_a_bot_sealed_on_another_real_account_is_left_alone_and_clear_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a ``shadow:`` store is opened for a foreign bot; any other account
    the installed Clerk does not hold has no authority here, so the bot's
    record is left exactly as it is -- and Clear refuses it for its account,
    not with a not-yet-settled promise."""
    repo, clerk, _broker, registry = _dead_bot(tmp_path)
    other_repo, other_clerk, other_broker = _account("PA-OTHER", tmp_path / "clerk-other")
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
    finally:
        set_alpaca_clerk(None)
    set_alpaca_clerk(other_clerk)
    try:
        await _sweep(other_repo, other_clerk, other_broker, registry).run()

        record = _lifecycle(tmp_path)
        assert (record["phase"], record["duty_outcome"]) == ("ON_DUTY", None)
        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")
        assert refused.value.reason_code == "ARCHIVE_SEALED_ACCOUNT_CUSTODY"
    finally:
        set_alpaca_clerk(None)
        other_repo.close()
        repo.close()

"""The reconciliation sweep settles a provably dead run, no restart (#2589).

A dead bot whose last run never settled used to wait for a Clerk restart
(boot recovery) or a lease revival before Clear would take it; one sealed on
a foreign ``shadow:`` account after graduation was never settled at all.
The sweep's pass now hands the runs it retired because their runner is gone
(#2369) to the runner, which settles each through the boot scan's own repair
— and settles a shadow-sealed dead bot through the sealed account's own
authority, never the installed one (ADR 0050).
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
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
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


def _compose(
    tmp_path: Path, feed: _ResumeFeed, *, account_id: str, clerk_root: Path
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker, BotTaskRegistry]:
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=clerk_root)
    broker = _FlatBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        boot_recovery_required=False,
        start_custody_guard=admission_guard_for(clerk),
        market_liveness=_tradable_market_liveness,
    )
    return repo, clerk, broker, registry


def _current_clerk_guard():
    """A start guard that resolves the *installed* Clerk at call time.

    The production default reads the installed authority lazily; a guard
    closed over one Clerk would pin this lane's rehearsal authority past its
    graduation, and the archive's custody pass would then run on a closed
    repository.
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


@pytest.mark.asyncio
async def test_one_sweep_settles_a_dead_bot_so_clear_needs_no_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass retires the runner-less run (#2369) and settles the runner's
    duty record in the same breath: OFF_DUTY with interrupted evidence, the
    desired state STOPPED, and Clear — archive — takes the bot off the
    roster. No restart happened; the registry never left this process."""
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))
    repo, clerk, broker, registry = _compose(
        tmp_path, feed, account_id="PA-TEST", clerk_root=tmp_path / "clerk"
    )
    sweep = ReconciliationSweep(
        repo=repo,
        read=broker,
        trade=broker,
        intake=clerk.intake,
        run_ownership=clerk.run_ownership,
        max_passes=1,
        sleep=lambda _delay: asyncio.sleep(0),
        pricing=UNPRICEABLE_RECOVERY,
        on_duty_settle=registry.settle_proven_dead_runs,
    )
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        assert repo.active_run(_SID) is not None

        await sweep.run()

        assert repo.active_run(_SID) is None
        assert registry.status("alpaca", _SID).phase == "OFF_DUTY"
        record = _lifecycle(tmp_path)
        assert record["duty_outcome"]["kind"] == "EXITED_UNVERIFIED"
        assert record["duty_outcome"]["reason_code"] == "INTERRUPTED_BY_RUNNER_GONE"

        await registry.archive("alpaca", _SID, updated_by="operator")

        assert registry.status("alpaca", _SID).phase == "RETIRED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_running_bot_is_never_settled_by_the_sweep(tmp_path: Path) -> None:
    """The settle's bar: no live process owns the run. A bot that is still
    running keeps its ON_DUTY record and its ACTIVE Clerk run, whatever the
    sweep hands over."""
    feed = _ResumeFeed()  # no bars: the stream stays open, the bot keeps running
    repo, clerk, _broker, registry = _compose(
        tmp_path, feed, account_id="PA-TEST", clerk_root=tmp_path / "clerk"
    )
    set_alpaca_clerk(clerk)
    try:
        await _deploy(registry)
        run = repo.active_run(_SID)
        assert run is not None

        await registry.settle_proven_dead_runs([(_SID, run.lifecycle_run_id)])

        assert registry.status("alpaca", _SID).phase == "ON_DUTY"
        assert repo.active_run(_SID) is not None
    finally:
        await registry.stop("alpaca", _SID)
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_shadow_sealed_dead_bot_is_settled_through_its_sealed_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehearsal bot sealed on ``shadow:<live_account>`` after graduation:
    its run lives in the sealed account's own SQLite, so the installed live
    authority never sees it — and must never write duty state for it (ADR
    0050). The settle opens the sealed account the way Dry Run restoration
    opens a ``sim:`` account, closes the dead run there, and settles through
    that authority; Clear then works from this UI process, no restart."""
    live_account_id = "9LIVE0001"
    sealed_account_id = f"shadow:{live_account_id}"
    runner_root = tmp_path / "runner"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))

    # The rehearsal lane: its authority is the shadow account itself, so the
    # run is admitted into the sealed account's own SQLite.
    await activate_shadow_clerk_authority(
        live_account_id=live_account_id, artifacts_root=runner_root
    )
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

    # Graduation: the sealed account's lane is gone; the installed authority
    # is the live account. The binding stays sealed on the shadow account.
    live_repo = ClerkSqliteRepository.initialize(
        account_id="PA-LIVE-2", artifacts_root=tmp_path / "clerk-live"
    )
    live_broker = _FlatBroker()
    live_clerk = SqliteAlpacaClerkFacade(
        repo=live_repo, read=live_broker, trade=live_broker, account_mode="paper"
    )
    instance_path = runner_root / "live_state" / _SID / "strategy_instance.json"
    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    instance["sealed_account_id"] = sealed_account_id
    instance_path.write_text(json.dumps(instance), encoding="utf-8")

    set_alpaca_clerk(live_clerk)
    try:
        assert registry.status("alpaca", _SID).phase == "ON_DUTY"

        await registry.settle_proven_dead_runs([])

        assert registry.status("alpaca", _SID).phase == "OFF_DUTY"
        record = _lifecycle(tmp_path)
        assert record["duty_outcome"]["kind"] == "EXITED_UNVERIFIED"
        assert record["duty_outcome"]["reason_code"] == "INTERRUPTED_BY_RUNNER_GONE"
        # The installed live authority wrote nothing for the instance: no run
        # was ever admitted through it, and none was written by the settle.
        assert live_repo.strategy_instances() == []

        reopened = ClerkSqliteRepository.open(
            account_id=sealed_account_id, artifacts_root=runner_root
        )
        try:
            assert reopened.active_run(_SID) is None
        finally:
            reopened.close()

        # Clear itself still waits: proving this bot holds nothing needs the
        # sealed account's own evidence, which a graduated lane can no longer
        # read (the live account now holds its orders, so the shadow
        # composition's namespace check refuses by design). The refusal is a
        # named owner sentence, never an opaque unknown-instance crash.
        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")
        assert refused.value.reason_code == "ARCHIVE_SEALED_ACCOUNT_CUSTODY"
        assert registry.status("alpaca", _SID).phase == "OFF_DUTY"
    finally:
        set_alpaca_clerk(None)
        live_repo.close()

"""The runner's supervise task owns its Clerk run for exactly as long as it lives (#2369).

Composes the real :class:`BotTaskRegistry` with the real SQLite Clerk facade
and its reconciliation pass (the clerk-side unit tests are in
``tests/broker/alpaca/clerk/sqlite/test_run_ownership.py``). A bot whose task
ended but whose STOP commit failed leaves its run ACTIVE; the next pass must
retire it. A bot that is still running must never be retired by a pass.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.services.bot_runner import BotTaskRegistry
from tests._helpers.bot_runner.custody import admission_guard_for
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.services.test_bot_runner_ema_resume import (
    _STRATEGY_INSTANCE_ID,
    _first_resumed_bar,
    _FlatBroker,
    _ResumeFeed,
    _tradable_market_liveness,
    _wait_for,
)


def _compose(
    tmp_path: Path, feed: _ResumeFeed
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker, BotTaskRegistry]:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    broker = _FlatBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        boot_recovery_required=False,
        start_custody_guard=admission_guard_for(clerk),
        market_liveness=_tradable_market_liveness,
    )
    return repo, clerk, broker, registry


async def _deploy(registry: BotTaskRegistry) -> None:
    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_STRATEGY_INSTANCE_ID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
    )


@pytest.mark.asyncio
async def test_a_crashed_bot_whose_stop_commit_failed_is_retired_on_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2369's trigger: the task ended, its STOP never landed, the run stayed ACTIVE."""
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))
    repo, clerk, _broker, registry = _compose(tmp_path, feed)

    async def _stop_commit_fails(**_kwargs: object) -> None:
        raise RuntimeError("SQLite STOP commit failed")

    set_alpaca_clerk(clerk)
    try:
        monkeypatch.setattr(clerk, "stop_strategy_run", _stop_commit_fails)
        await _deploy(registry)
        await _wait_for(lambda: not registry.any_running())
        # Let the task's done-callbacks run.
        await asyncio.sleep(0)
        assert repo.active_run(_STRATEGY_INSTANCE_ID) is not None

        await clerk.reconcile_account(trigger="AUTOMATIC")

        assert repo.active_run(_STRATEGY_INSTANCE_ID) is None
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_running_bot_is_never_retired_by_the_sweep(tmp_path: Path) -> None:
    feed = _ResumeFeed()  # no bars: the stream stays open, the bot keeps running
    repo, clerk, broker, registry = _compose(tmp_path, feed)
    sweep = ReconciliationSweep(
        repo=repo,
        read=broker,
        trade=broker,
        intake=clerk.intake,
        on_result=clerk.publish_sweep_reconciliation,
        run_ownership=clerk.run_ownership,
        max_passes=3,
        sleep=lambda _delay: asyncio.sleep(0),
        pricing=UNPRICEABLE_RECOVERY,
    )
    set_alpaca_clerk(clerk)
    try:
        await _deploy(registry)

        await sweep.run()

        assert registry.any_running()
        assert repo.active_run(_STRATEGY_INSTANCE_ID) is not None
        await registry.stop("alpaca", _STRATEGY_INSTANCE_ID)
        assert repo.active_run(_STRATEGY_INSTANCE_ID) is None
    finally:
        set_alpaca_clerk(None)
        repo.close()

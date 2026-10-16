"""Focused resume regression coverage for issue #1692."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.bot_runner import BotTaskRegistry
from tests._helpers.bot_runner.custody import admission_guard_for
from tests._helpers.bot_runner.market import _tradable_market_liveness
from tests._helpers.bot_runner.resume import (
    _STRATEGY_INSTANCE_ID,
    _first_resumed_bar,
    _FlatBroker,
    _ResumeFeed,
    _wait_for,
)
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

pytestmark = pytest.mark.usefixtures("legacy_run_registration")


def _registry_with_sqlite_clerk(
    tmp_path: Path,
    feed: _ResumeFeed,
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, BotTaskRegistry]:
    repository = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path / "clerk",
    )
    broker = _FlatBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repository, read=broker, trade=broker, account_mode="paper")
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        boot_recovery_required=False,
        start_custody_guard=admission_guard_for(clerk),
        market_liveness=_tradable_market_liveness,
    )
    return repository, clerk, registry


@pytest.mark.asyncio
async def test_unhandled_error_is_preserved_only_on_immutable_run_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test proves crash-diagnostic plumbing, not deploy admission.
    crash_message = "TradeBar.__init__() got an unexpected keyword argument 'start_ms'"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=TypeError(crash_message))
    repository, clerk, registry = _registry_with_sqlite_clerk(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
        )
        await _wait_for(lambda: not registry.any_running())

        status = registry.status("alpaca", _STRATEGY_INSTANCE_ID)
        current_run = registry.current_run("alpaca", _STRATEGY_INSTANCE_ID)
        assert status.duty_outcome is not None
        assert status.duty_outcome.kind == "CRASHED"
        assert current_run.terminal_outcome is not None
        diagnostic = current_run.terminal_outcome.crash_diagnostic
        assert diagnostic is not None
        assert diagnostic.exception_type == "TypeError"
        assert diagnostic.message == crash_message
        assert diagnostic.source_file.endswith(
            "tests/_helpers/bot_runner/resume.py"
        )
        assert diagnostic.source_line > 0

        outcome_path = (
            tmp_path
            / "runner/live_state"
            / _STRATEGY_INSTANCE_ID
            / "run_outcomes"
            / f"{current_run.run_id}.json"
        )
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        assert outcome["crash_diagnostic"] == diagnostic.model_dump(mode="json")
        lifecycle = (
            tmp_path
            / "runner/live_state"
            / _STRATEGY_INSTANCE_ID
            / "lifecycle_state.json"
        ).read_text(encoding="utf-8")
        assert "crash_diagnostic" not in lifecycle
    finally:
        set_alpaca_clerk(None)
        repository.close()


@pytest.fixture(autouse=True)
def _open_start_window(monkeypatch):
    from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness
    patch_fresh_live_market_liveness(monkeypatch)

"""Startup words and progress are authored from the program's canonical count."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.indicator_convergence import IndicatorProgress
from app.services.bot_trade_strategy import _convergence_recorder
from app.services.broker_v2_panel.bot_page_projection import bot_own_status
from app.services.indicator_spawning import deploy_spawn_estimate, spawning_view
from app.services.source_bar_ledger import SourceBarLedger


def progress(done: int = 20, needed: int = 109) -> IndicatorProgress:
    return IndicatorProgress(bars_done=done, bars_needed=needed, converged=done >= needed, decision_timeframe_ms=900_000)


def test_deploy_estimate_distinguishes_history_sessions_and_long_bars() -> None:
    assert deploy_spawn_estimate('rsi_mean_reversion', {}) == 'Ready to decide at start when history is complete.'
    assert deploy_spawn_estimate('deployment_validation', {}) == 'Ready to decide at start when history is complete.'
    sessions = deploy_spawn_estimate('sma_crossover', {'long_window': 500})
    assert sessions == 'Spawning: needs about 10 more trading sessions of live bars before it decides. Spawn progress is lost if the bot stops.'
    long_bars = deploy_spawn_estimate('sma_crossover', {'resolution_minutes': 1440})
    assert long_bars == 'Spawning: needs 30 more bars of 1440 minutes before it decides. Spawn progress is lost if the bot stops.'


def test_progress_is_run_owned_and_survives_a_read_only_projection(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id='sim:spawn')
    try:
        ledger.record_indicator_progress(run_id='one', progress=progress())
        assert ledger.indicator_progress(run_id='one') == progress()
        assert ledger.indicator_progress(run_id='two') is None
        ledger.record_indicator_progress(run_id='one', progress=progress(109))
        assert ledger.indicator_progress(run_id='one') == progress(109)
    finally:
        ledger.close()
    readonly = SourceBarLedger(artifacts_root=tmp_path, account_id='sim:spawn', read_only=True)
    try:
        assert readonly.indicator_progress(run_id='one') == progress(109)
    finally:
        readonly.close()


@pytest.mark.parametrize(('running', 'done', 'state'), [(True, 20, 'spawning'), (True, 109, 'running'), (False, 20, 'finished')])
def test_bot_status_says_spawning_only_while_indicators_are_pending(running: bool, done: int, state: str) -> None:
    view = spawning_view(progress(done), running=running)
    panel = SimpleNamespace(health=SimpleNamespace(running=running, duty_outcome=None), spawning=view,
                            startup_join=None, exposure={})
    status = bot_own_status(panel, bot_owns_custody_problem=False, execution_coverage_complete=True)
    assert status.state == state
    if view is not None:
        assert status.label == 'Spawning'
        assert view.sessions_left == 4
        assert '20 of 109' in view.explanation
    else:
        assert status.label != 'Spawning'


def test_a_ledger_that_cannot_take_progress_does_not_end_the_run(caplog: pytest.LogCaptureFixture) -> None:
    def locked(*, run_id: str, progress: IndicatorProgress) -> None:
        raise sqlite3.OperationalError('database is locked')

    ledger = SimpleNamespace(record_indicator_progress=locked)
    binding = SimpleNamespace(run_id='run-1', strategy_instance_id='spawn')
    record = _convergence_recorder(ledger, binding)
    assert record is not None
    record(progress())
    assert [r.action for r in caplog.records] == ['bot_indicator_progress_unsaved']
    assert _convergence_recorder(None, binding) is None

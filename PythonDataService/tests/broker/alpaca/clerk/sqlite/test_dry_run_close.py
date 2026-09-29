"""Which ended runs' positions the simulation closes (owner decision 2026-09-29).

Only a Dry Run (a ``sim:`` authority) is exempt from the dead-run rule; a real
account and a live account's shadow keep an ended run's position open for the
operator. The end-to-end close -- price, timing, restarts -- is driven in
``tests/broker/v2panel/test_dry_run_recovery.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.dry_run_close import closes_owed
from app.broker.alpaca.clerk.sqlite.exit import accept_recovery_exit
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import _make_held_position

SID = "dry-bot"
RUN_ID = "run-1"


def _held_repo(tmp_path: Path, account_id: str) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=account_id, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    return repo


@pytest.fixture
def sim_repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    repo = _held_repo(tmp_path, f"sim:{SID}")
    yield repo
    repo.close()


def _stop(repo: ClerkSqliteRepository) -> None:
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)


@pytest.mark.parametrize(
    ("account_id", "owed"),
    [(f"sim:{SID}", 1), ("shadow:318420190", 0), ("PA-REAL", 0)],
)
async def test_closes_owed_names_an_ended_run_position_only_on_a_dry_run_authority(
    tmp_path: Path, account_id: str, owed: int,
) -> None:
    repo = _held_repo(tmp_path, account_id)
    try:
        await _make_held_position(repo, account_id=account_id, strategy_instance_id=SID, run_id=RUN_ID, quantity=1)
        _stop(repo)

        closes = closes_owed(repo)

        assert len(closes) == owed
        assert [(close.strategy_instance_id, close.symbol) for close in closes] == [(SID, "SPY")] * owed
    finally:
        repo.close()


async def test_closes_owed_leaves_a_running_dry_run_alone(sim_repo: ClerkSqliteRepository) -> None:
    await _make_held_position(sim_repo, account_id=sim_repo.account_id, strategy_instance_id=SID, run_id=RUN_ID, quantity=1)

    assert closes_owed(sim_repo) == []


async def test_closes_owed_never_names_an_exposure_whose_close_was_accepted(
    sim_repo: ClerkSqliteRepository,
) -> None:
    """One close per exposure: re-running the pass must never sell twice."""
    await _make_held_position(sim_repo, account_id=sim_repo.account_id, strategy_instance_id=SID, run_id=RUN_ID, quantity=1)
    _stop(sim_repo)
    [close] = closes_owed(sim_repo)

    accept_recovery_exit(
        sim_repo, account_id=sim_repo.account_id, strategy_instance_id=SID,
        decision_id=close.decision_id, entry_order_ref=close.entry_order_ref, forbid_active_run=True,
    )

    assert closes_owed(sim_repo) == []


async def test_closes_owed_is_empty_for_a_flat_ended_dry_run(sim_repo: ClerkSqliteRepository) -> None:
    _stop(sim_repo)

    assert closes_owed(sim_repo) == []

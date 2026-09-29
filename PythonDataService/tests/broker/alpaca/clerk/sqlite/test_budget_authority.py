"""Upgrade is one irreversible fact; old arming never regains authority."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.budget_authority import authority_review_token, commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, SchemaVersionMismatch
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock


def test_cutover_requires_stop_and_retains_unresolved_order(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="upgrade", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        repo.register_strategy_instance(strategy_instance_id="old", symbol="SPY", config_hash="old-seal")
        submit_start_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="old-run", clock=repo.clock)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="old-run", decision_id="working", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1))
        token = authority_review_token(repo)
        with pytest.raises(BudgetUnavailable):
            commit_budget_authority_cutover(repo, actor="owner", reviewed_token=token, stop_receipt="not-stopped")
        assert repo.budget_authority_version() == 1
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="old-run", clock=repo.clock)
        order = repo.order(accepted.order_ref)
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token=token, stop_receipt="stopped")
        assert repo.budget_authority_version() == 2
        assert repo.order(accepted.order_ref) == order
        assert repo.deployment_budget("old") is None
        with pytest.raises(BudgetUnavailable):
            submit_start_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="resurrect", clock=repo.clock)
        with pytest.raises(sqlite3.IntegrityError):
            repo._conn.execute("UPDATE control_meta SET authorization_version=1 WHERE id=1")
    finally:
        repo.close()


def test_cutover_survives_response_loss_and_mirror_rebuild(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="upgrade", artifacts_root=tmp_path, clock=_TestClock(NOON))
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token=authority_review_token(repo), stop_receipt="empty")
    committed = repo.custody_transitions()
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="retry", stop_receipt="retry")
    assert repo.custody_transitions() == committed
    database = repo.db_path
    repo.close()
    database.rename(database.with_suffix(".saved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="upgrade", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        assert rebuilt.budget_authority_version() == 2
        assert rebuilt.custody_transitions() == committed
    finally:
        rebuilt.close()


def test_precommit_failure_leaves_only_legacy_authority(tmp_path: Path, monkeypatch) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="upgrade", artifacts_root=tmp_path, clock=_TestClock(NOON))
    try:
        def fail(*_args, **_kwargs):
            raise OSError("simulated durable write failure before commit")
        monkeypatch.setattr(repo, "append_transition", fail)
        with pytest.raises(OSError):
            commit_budget_authority_cutover(repo, actor="owner", reviewed_token="review", stop_receipt="empty")
        assert repo.budget_authority_version() == 1 and not repo.custody_transitions()
    finally:
        repo.close()


def test_older_schema_writer_cannot_acquire_upgraded_authority(tmp_path: Path, monkeypatch) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="upgrade", artifacts_root=tmp_path, clock=_TestClock(NOON))
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="review", stop_receipt="empty")
    repo.close()
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 20)
    with pytest.raises(SchemaVersionMismatch):
        ClerkSqliteRepository.open(account_id="upgrade", artifacts_root=tmp_path, clock=_TestClock(NOON))



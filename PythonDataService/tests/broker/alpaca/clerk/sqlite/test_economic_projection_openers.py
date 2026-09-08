"""Read-only openers the twin reconciliation reads a foreign authority with (ADR 0059 D2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionUnavailable,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

_ACCOUNT_ID = "PA-TWIN-OPENERS"
_SID = "twin-openers"


def _repository(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_786_368_000_000),
    )
    repo.register_strategy_instance(
        strategy_instance_id=_SID,
        symbol="SPY",
        config_hash="twin-openers-config",
    )
    return repo


def test_from_database_path_reads_a_running_authority_and_refuses_a_foreign_file(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-1",
    )

    # The repository still holds its execution lease on this database.
    reader = SqliteEconomicProjectionReader.from_database_path(repo.db_path)
    try:
        assert [run.run_id for run in reader.runs_for_strategy(_SID)] == [f"{_SID}:l-1"]
    finally:
        reader.close()
    assert repo.active_run(_SID) is not None
    repo.close()

    stranger = tmp_path / "not-a-clerk.db"
    connection = sqlite3.connect(stranger)
    try:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(EconomicProjectionUnavailable, match="not a readable clerk database"):
        SqliteEconomicProjectionReader.from_database_path(stranger)


def test_runs_for_strategy_returns_every_run_oldest_first(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-1",
    )
    repo.clock.advance(1_000)
    submit_stop_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-1",
    )
    repo.clock.advance(1_000)
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-2",
    )

    reader = SqliteEconomicProjectionReader.from_database_path(repo.db_path)
    try:
        runs = reader.runs_for_strategy(_SID)
    finally:
        reader.close()
        repo.close()

    assert [(run.run_id, run.state) for run in runs] == [
        (f"{_SID}:l-1", "STOPPED"),
        (f"{_SID}:l-2", "ACTIVE"),
    ]
    assert runs[0].started_at_ms < runs[1].started_at_ms
    assert runs[0].stopped_at_ms is not None and runs[1].stopped_at_ms is None

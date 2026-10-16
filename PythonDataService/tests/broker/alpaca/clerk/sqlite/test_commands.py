"""Stop command behavior and replay of historical Start facts (#1376)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import (
    InvalidIdentityError,
    NoActiveRunError,
    UnknownStrategyInstanceError,
    run_stop_reason,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"


def _clock_seq():
    counter = {"t": 1_700_000_000_000}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    return clock


@pytest.fixture
def repo(tmp_path: Path):
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    yield r
    r.close()

def test_stop_deactivates_the_run(repo: ClerkSqliteRepository) -> None:
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    submission = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )

    assert submission.command.state == "succeeded"
    assert submission.command.action == "STOP"
    assert repo.active_run(SID) is None


def test_run_stop_reason_is_the_reason_its_first_stop_committed_under(repo: ClerkSqliteRepository) -> None:
    """#2607: the runner tells the Clerk's STOP at a bot's end from any other Stop by this reason."""
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    assert run_stop_reason(repo, strategy_instance_id=SID, lifecycle_run_id="run-1") is None  # still ACTIVE

    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1",
        operator_reason="scheduled_end",
    )
    # A later Stop of the same run is the STOP already committed, under its first reason.
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1",
        operator_reason="operator stop",
    )

    assert run_stop_reason(repo, strategy_instance_id=SID, lifecycle_run_id="run-1") == "scheduled_end"
    assert run_stop_reason(repo, strategy_instance_id=SID, lifecycle_run_id="another-run") is None


def test_stop_with_no_active_run_raises_without_writing_a_command(repo: ClerkSqliteRepository) -> None:
    before = len(repo.custody_transitions())
    with pytest.raises(NoActiveRunError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
        )
    assert len(repo.custody_transitions()) == before


def test_stop_lifecycle_run_id_not_matching_the_active_run_raises(
    repo: ClerkSqliteRepository,
) -> None:
    """A caller-supplied ``lifecycle_run_id`` that isn't the currently
    active run's is a fresh identity with no active run to bind — typed
    404, not a silent resolve against whichever run happens to be active."""
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    with pytest.raises(NoActiveRunError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2"
        )
    assert repo.active_run(SID) is not None  # run-1 is untouched


def test_stop_command_id_is_scoped_to_the_lifecycle_run_id(repo: ClerkSqliteRepository) -> None:
    """Stop commands for different lifecycle_run_ids never collide — each
    embeds its own run identity in the content-addressed key. (Repeated
    Stop of the *same* run is covered separately by
    test_stop_retry_after_run_already_stopped_replays_the_completed_result
    below.)"""
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    first_stop = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    second_stop = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2"
    )
    assert first_stop.command.command_id != second_stop.command.command_id
    assert first_stop.command.command_id.split(":")[3] == "run-1"
    assert second_stop.command.command_id.split(":")[3] == "run-2"


def test_stop_retry_after_run_already_stopped_replays_the_completed_result(
    repo: ClerkSqliteRepository,
) -> None:
    """Corrective foundation slice, the exact defect the fix closes (a Stop
    retry losing the active-run identity): a lost-response retry of a Stop
    must return the original completed command even though the run it targeted is no longer active
    — and even after a *different* run has since started and stopped."""
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    stop1 = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")

    retry = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    assert not retry.created
    assert retry.command.command_id == stop1.command.command_id


def test_get_command_returns_the_full_resource(repo: ClerkSqliteRepository) -> None:
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    fetched = repo.get_command(f"cmd:{ACCOUNT_ID}:{SID}:run-1:START:ACTIVE")
    assert fetched is not None
    assert fetched.state == "succeeded"
    assert fetched.action == "START"
    assert fetched.run_id == f"{SID}:run-1"


def test_state_machine_survives_restart(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    repo.close()

    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    fetched = reopened.get_command(f"cmd:{ACCOUNT_ID}:{SID}:run-1:START:ACTIVE")
    assert fetched is not None
    assert fetched.state == "succeeded"
    active = reopened.active_run(SID)
    assert active is not None and active.lifecycle_run_id == "run-1"
    reopened.close()

def test_stop_rejects_a_colon_in_strategy_instance_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="a:b", lifecycle_run_id="run-1"
        )


def test_stop_rejects_a_colon_in_lifecycle_run_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="a:b"
        )

def test_stop_on_unknown_strategy_instance_is_a_typed_error_not_a_raw_500(
    repo: ClerkSqliteRepository,
) -> None:
    with pytest.raises(UnknownStrategyInstanceError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="never-registered",
            lifecycle_run_id="run-1",
        )

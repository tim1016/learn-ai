"""Command-lifecycle domain tests (#1376) — R2's idempotency split, plus the
acceptance-criteria list from the issue: concurrent duplicate POSTs produce
exactly one effect, transport retry is silent, genuine conflict is typed,
the state machine survives restart, GET returns the full resource.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import (
    DurableConflictError,
    InvalidIdentityError,
    NoActiveRunError,
    UnknownStrategyInstanceError,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

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


def test_start_creates_an_active_run_and_a_succeeded_command(repo: ClerkSqliteRepository) -> None:
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    assert submission.created
    assert submission.command.state == "succeeded"
    assert submission.command.kind == "operator_lifecycle"
    assert submission.command.action == "START"
    active = repo.active_run(SID)
    assert active is not None
    assert active.lifecycle_run_id == "run-1"
    assert active.state == "ACTIVE"


def test_transport_retry_returns_existing_result_no_error_no_new_effect(
    repo: ClerkSqliteRepository,
) -> None:
    first = submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    before = len(repo.custody_transitions())

    retry = submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")

    assert not retry.created
    assert retry.command.command_id == first.command.command_id
    assert len(repo.custody_transitions()) == before  # no second effect


def test_same_identity_different_payload_is_a_durable_conflict_no_effect(
    repo: ClerkSqliteRepository,
) -> None:
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    before = len(repo.custody_transitions())

    with pytest.raises(DurableConflictError) as exc_info:
        submit_start_run(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            lifecycle_run_id="run-1",
            operator_reason="a different reason changes the hash",
        )
    assert exc_info.value.command.command_id.endswith(":run-1:START:ACTIVE")
    assert len(repo.custody_transitions()) == before  # no effect


def test_start_while_already_active_is_rejected_not_a_second_run(repo: ClerkSqliteRepository) -> None:
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    second = submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")

    assert second.created  # a new command was reserved and decided...
    assert second.command.state == "rejected"  # ...but rejected, not accepted
    active = repo.active_run(SID)
    assert active is not None
    assert active.lifecycle_run_id == "run-1"  # the original run is untouched


def test_stop_deactivates_the_run(repo: ClerkSqliteRepository) -> None:
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    submission = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )

    assert submission.command.state == "succeeded"
    assert submission.command.action == "STOP"
    assert repo.active_run(SID) is None


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
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
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
    in test_corrective_foundation.py.)"""
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    first_stop = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    second_stop = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2"
    )
    assert first_stop.command.command_id != second_stop.command.command_id
    assert first_stop.command.command_id.split(":")[3] == "run-1"
    assert second_stop.command.command_id.split(":")[3] == "run-2"


def test_stop_retry_after_run_already_stopped_replays_the_completed_result(
    repo: ClerkSqliteRepository,
) -> None:
    """Corrective foundation slice, the exact defect the fix closes
    (open-pr-review-2026-08-05.md P2 "Stop retry loses the active-run
    identity"): a lost-response retry of a Stop must return the original
    completed command even though the run it targeted is no longer active
    — and even after a *different* run has since started and stopped."""
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    stop1 = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")

    retry = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    assert not retry.created
    assert retry.command.command_id == stop1.command.command_id


def test_run2_stop_cannot_collide_with_run1_stop(repo: ClerkSqliteRepository) -> None:
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    stop1 = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    stop2 = submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2"
    )

    assert stop1.command.idempotency_key != stop2.command.idempotency_key
    # A stop for a lifecycle_run_id that was never active is the correct
    # typed outcome, not a silent reuse of either prior stop's result.
    with pytest.raises(NoActiveRunError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-3"
        )


def test_get_command_returns_the_full_resource(repo: ClerkSqliteRepository) -> None:
    submission = submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    fetched = repo.get_command(submission.command.command_id)
    assert fetched == submission.command


def test_get_unknown_command_returns_none(repo: ClerkSqliteRepository) -> None:
    assert repo.get_command("cmd:does-not-exist") is None


def test_state_machine_survives_restart(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submission = submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    repo.close()

    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    fetched = reopened.get_command(submission.command.command_id)
    assert fetched is not None
    assert fetched.state == "succeeded"
    active = reopened.active_run(SID)
    assert active is not None and active.lifecycle_run_id == "run-1"
    reopened.close()


def test_concurrent_duplicate_starts_produce_exactly_one_effect(repo: ClerkSqliteRepository) -> None:
    """The write lock (not a SELECT-then-INSERT race) is what makes this
    safe — N threads calling submit_start_run with the identical
    lifecycle_run_id must all observe the same single accepted run."""
    results: list[object] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)
    transitions_before = len(repo.custody_transitions())  # fixture already wrote one

    def worker() -> None:
        barrier.wait()
        try:
            results.append(
                submit_start_run(
                    repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1"
                )
            )
        except Exception as exc:  # pragma: no cover - would fail the test below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 8
    command_ids = {r.command.command_id for r in results}
    assert len(command_ids) == 1  # every thread converged on the same command
    assert sum(1 for r in results if r.created) == 1  # exactly one created it
    assert len(repo.custody_transitions()) == transitions_before + 1  # exactly one new effect
    active = repo.active_run(SID)
    assert active is not None and active.lifecycle_run_id == "run-1"


def test_concurrent_starts_with_different_run_ids_serialize_to_one_active_run(
    repo: ClerkSqliteRepository,
) -> None:
    """Different lifecycle_run_ids don't collide at reserve_command's
    idempotency key (unlike the identical-key case above), so this
    exercises a different atomicity guarantee: repo.serialized() spanning
    the whole reserve -> active_run() check -> append sequence, not the
    reservation lock alone. Before that fix (#1376 review), both threads
    could observe "no active run" before either committed and both append
    RUN_STARTED, racing on ux_runs_one_active_per_instance with a raw,
    unhandled sqlite3.IntegrityError and a permanently stuck 'reserved'
    command for the loser."""
    results: list[object] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(lifecycle_run_id: str) -> None:
        barrier.wait()
        try:
            results.append(
                submit_start_run(
                    repo,
                    account_id=ACCOUNT_ID,
                    strategy_instance_id=SID,
                    lifecycle_run_id=lifecycle_run_id,
                )
            )
        except Exception as exc:  # pragma: no cover - would fail the test below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("run-A",)),
        threading.Thread(target=worker, args=("run-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 2
    assert sorted(r.command.state for r in results) == ["rejected", "succeeded"]
    active = repo.active_run(SID)
    assert active is not None
    winner = next(r for r in results if r.command.state == "succeeded")
    assert winner.command.command_id.split(":")[3] == active.lifecycle_run_id


def test_start_rejects_a_colon_in_strategy_instance_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        submit_start_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="a:b", lifecycle_run_id="run-1"
        )


def test_start_rejects_a_colon_in_lifecycle_run_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        submit_start_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="a:b"
        )


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


def test_start_on_unknown_strategy_instance_is_a_typed_error_not_a_raw_500(
    repo: ClerkSqliteRepository,
) -> None:
    with pytest.raises(UnknownStrategyInstanceError):
        submit_start_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="never-registered",
            lifecycle_run_id="run-1",
        )


def test_stop_on_unknown_strategy_instance_is_a_typed_error_not_a_raw_500(
    repo: ClerkSqliteRepository,
) -> None:
    with pytest.raises(UnknownStrategyInstanceError):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="never-registered",
            lifecycle_run_id="run-1",
        )

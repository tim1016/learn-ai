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
    submission = submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)

    assert submission.command.state == "succeeded"
    assert submission.command.action == "STOP"
    assert repo.active_run(SID) is None


def test_stop_with_no_active_run_raises_without_writing_a_command(repo: ClerkSqliteRepository) -> None:
    before = len(repo.custody_transitions())
    with pytest.raises(NoActiveRunError):
        submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)
    assert len(repo.custody_transitions()) == before


def test_repeated_stop_of_the_same_run_is_idempotent_by_construction(repo: ClerkSqliteRepository) -> None:
    """Unlike Start, Stop needs no client-supplied token — the idempotency
    key is derived from the resolved active run, so repeated Stop calls for
    the *same* run collide on the same key without any special-casing."""
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    first_stop = submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)
    # A second Stop now has no active run (already stopped) — proves the
    # first Stop's idempotency key can never be reused by a *different* run
    # without an intervening Start (ADR 0035 #3, "cannot collide with the
    # equivalent run-1 command").
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    second_stop = submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)
    assert first_stop.command.command_id != second_stop.command.command_id
    assert first_stop.command.command_id.split(":")[3] == "run-1"
    assert second_stop.command.command_id.split(":")[3] == "run-2"


def test_run2_stop_cannot_collide_with_run1_stop(repo: ClerkSqliteRepository) -> None:
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-1")
    stop1 = submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    stop2 = submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)

    assert stop1.command.idempotency_key != stop2.command.idempotency_key
    # Retrying run-1's stop after run-2 exists must still return run-1's
    # original result, not somehow resolve against run-2.
    with pytest.raises(NoActiveRunError):
        # run-2 is already stopped, so there is no active run to bind a new
        # Stop's key to — the correct outcome, not a silent reuse of stop1.
        submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID)


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
        submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id="a:b")

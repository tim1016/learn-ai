"""Best-effort persistence: a storage failure yields a null run id and a log, never a failed run."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs import service
from app.research.backtest_runs.records import RunPayloadError, record_from_payload
from app.utils.background_loop import CallerStoppedWaitingError
from tests.research.backtest_runs.payloads import engine_payload, lean_payload


def test_a_database_failure_leaves_the_run_id_null_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing_run_sync(coroutine):
        coroutine.close()
        raise ConnectionError("database down")

    monkeypatch.setattr(service, "run_sync", failing_run_sync)

    with caplog.at_level(logging.ERROR):
        run_id = service.persist_run_payload_sync(engine_payload())

    assert run_id is None
    assert "Run not persisted" in caplog.text and "database down" in caplog.text


def test_an_invalid_payload_never_reaches_the_writer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reached: list = []
    monkeypatch.setattr(service, "run_sync", lambda coroutine: reached.append(coroutine) or coroutine.close())

    with caplog.at_level(logging.ERROR):
        run_id = service.persist_run_payload_sync(engine_payload(symbol=""))

    assert run_id is None and reached == []
    assert "symbol is required" in caplog.text


def test_a_failed_settle_is_logged_and_leaves_the_run_persisted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def stub_insert(fn, /, *args, **kwargs):
        return repo.InsertOutcome(run_id=5, created=True)

    async def exploding_settle(**kwargs):
        raise RuntimeError("compare exploded")

    monkeypatch.setattr(service, "with_connection", stub_insert)
    monkeypatch.setattr(service, "settle_parity_for_lean_run", exploding_settle)
    monkeypatch.setattr(service, "run_sync", asyncio.run)

    with caplog.at_level(logging.ERROR):
        run_id = service.persist_run_payload_sync(
            lean_payload("companion-pg-0", parity_group_id="pg-0", requested_engine="both")
        )

    assert run_id == 5  # the row landed; only the verdict did not
    assert "verdict left as it stands" in caplog.text


def test_a_caller_that_stops_waiting_is_not_told_the_run_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#1977: ``run_sync`` does not cancel, so this is outcome-unknown, not failure."""

    def abandoning_run_sync(coroutine):
        coroutine.close()
        raise CallerStoppedWaitingError("still running")

    monkeypatch.setattr(service, "run_sync", abandoning_run_sync)

    with caplog.at_level(logging.WARNING):
        run_id = service.persist_run_payload_sync(engine_payload())

    assert run_id is None
    assert "outcome unknown" in caplog.text
    assert "Run not persisted" not in caplog.text


def test_a_write_that_timed_out_on_the_database_is_still_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#1977 M1: ``asyncio.TimeoutError`` *is* ``TimeoutError`` since 3.11.

    asyncpg raises it for the pool's ``command_timeout``, from inside the
    coroutine — the write stopped and rolled back. Reporting that as
    outcome-unknown would drop the traceback exactly when an operator is
    chasing a lost run.
    """

    def command_timeout_run_sync(coroutine):
        coroutine.close()
        raise TimeoutError("query timed out")

    monkeypatch.setattr(service, "run_sync", command_timeout_run_sync)

    with caplog.at_level(logging.WARNING):
        run_id = service.persist_run_payload_sync(engine_payload())

    assert run_id is None
    assert "Run not persisted" in caplog.text
    assert "outcome unknown" not in caplog.text


def test_the_companions_failure_detail_reaches_the_settle_from_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1977: the payload's failure detail is validated onto the record, then routed."""
    settled: list[dict] = []

    async def stub_insert(fn, /, *args, **kwargs):
        return repo.InsertOutcome(run_id=5, created=True)

    async def stub_settle(**kwargs):
        settled.append(kwargs)
        return True

    monkeypatch.setattr(service, "with_connection", stub_insert)
    monkeypatch.setattr(service, "settle_parity_for_lean_run", stub_settle)
    monkeypatch.setattr(service, "run_sync", asyncio.run)

    run_id = service.persist_run_payload_sync(
        lean_payload(
            "companion-pg-1",
            parity_group_id="pg-1",
            requested_engine="both",
            parity_failure_detail="No normalized/result.json — LEAN run did not produce output",
        )
    )

    assert run_id == 5
    assert settled == [
        {
            "right_run_id": 5,
            "parity_group_id": "pg-1",
            "failure_detail": "No normalized/result.json — LEAN run did not produce output",
        }
    ]


def test_a_companion_with_a_real_result_carries_no_failure_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    settled: list[dict] = []

    async def stub_insert(fn, /, *args, **kwargs):
        return repo.InsertOutcome(run_id=5, created=True)

    async def stub_settle(**kwargs):
        settled.append(kwargs)
        return True

    monkeypatch.setattr(service, "with_connection", stub_insert)
    monkeypatch.setattr(service, "settle_parity_for_lean_run", stub_settle)
    monkeypatch.setattr(service, "run_sync", asyncio.run)

    service.persist_run_payload_sync(lean_payload("companion-pg-2", parity_group_id="pg-2", requested_engine="both"))

    assert settled == [{"right_run_id": 5, "parity_group_id": "pg-2", "failure_detail": None}]


def test_a_failure_detail_without_a_group_is_refused_by_the_converter() -> None:
    """The detail is a routing instruction for a parity group; it needs one."""
    with pytest.raises(RunPayloadError, match="lean-sidecar"):
        record_from_payload(engine_payload() | {"parity_failure_detail": "boom"})


@pytest.mark.asyncio
async def test_the_coroutine_form_runs_the_same_write_off_the_calling_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "persist_run_payload_sync", lambda payload: 99)

    assert await service.persist_run_payload(engine_payload()) == 99

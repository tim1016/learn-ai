"""Best-effort persistence: a storage failure yields a null run id and a log, never a failed run."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs import service
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
    async def exploding_settle(fn, /, *args, **kwargs):
        if fn.__name__ == "insert_run":
            return repo.InsertOutcome(run_id=5, created=True)
        raise RuntimeError("compare exploded")

    monkeypatch.setattr(service, "with_connection", exploding_settle)
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
    """#1977: ``run_sync`` does not cancel, so a timeout is outcome-unknown, not failure."""

    def timing_out_run_sync(coroutine):
        coroutine.close()
        raise TimeoutError

    monkeypatch.setattr(service, "run_sync", timing_out_run_sync)

    with caplog.at_level(logging.WARNING):
        run_id = service.persist_run_payload_sync(engine_payload())

    assert run_id is None
    assert "outcome unknown" in caplog.text
    assert "Run not persisted" not in caplog.text


def test_the_insert_and_the_settle_reach_the_writer_loop_as_one_coroutine(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1977: chained on the writer loop, the settle survives a caller that timed out."""
    submitted: list = []

    def capturing_run_sync(coroutine):
        submitted.append(coroutine)
        coroutine.close()
        raise TimeoutError

    monkeypatch.setattr(service, "run_sync", capturing_run_sync)

    service.persist_run_payload_sync(engine_payload())

    assert [c.__qualname__ for c in submitted] == ["_insert_and_settle"]


def test_a_companion_that_produced_no_result_settles_its_group_instead_of_comparing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1977: a zero-trade failed row must not be compared against a real Python run."""
    calls: list[tuple[str, dict]] = []

    async def recording_with_connection(fn, /, *args, **kwargs):
        calls.append((fn.__name__, {"args": args, "kwargs": kwargs}))
        return (None, False) if fn.__name__ == "mark_parity_failed" else repo.InsertOutcome(run_id=5, created=True)

    monkeypatch.setattr(service, "with_connection", recording_with_connection)
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
    assert [name for name, _ in calls] == ["insert_run", "mark_parity_failed"]
    assert calls[1][1]["kwargs"]["status"] == "run_failed"


def test_a_companion_with_a_real_result_still_freezes_the_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def recording_with_connection(fn, /, *args, **kwargs):
        called.append(fn.__name__)
        return repo.InsertOutcome(run_id=5, created=True) if fn.__name__ == "insert_run" else True

    monkeypatch.setattr(service, "with_connection", recording_with_connection)
    monkeypatch.setattr(service, "run_sync", asyncio.run)

    run_id = service.persist_run_payload_sync(
        lean_payload("companion-pg-2", parity_group_id="pg-2", requested_engine="both")
    )

    assert run_id == 5
    assert called == ["insert_run", "freeze_parity_for_lean_run"]


@pytest.mark.asyncio
async def test_the_coroutine_form_runs_the_same_write_off_the_calling_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "persist_run_payload_sync", lambda payload: 99)

    assert await service.persist_run_payload(engine_payload()) == 99

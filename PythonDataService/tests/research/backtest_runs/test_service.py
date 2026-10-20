"""Best-effort persistence: a storage failure yields a null run id and a log, never a failed run."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs import service
from app.research.backtest_runs.records import RunPayloadError, record_from_payload
from app.research.backtest_runs.service import SaveOutcome
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
        save = service.persist_run_payload_sync(engine_payload())

    assert save == service.SAVE_FAILED
    assert "Run not persisted" in caplog.text and "database down" in caplog.text


def test_an_invalid_payload_never_reaches_the_writer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reached: list = []
    monkeypatch.setattr(service, "run_sync", lambda coroutine: reached.append(coroutine) or coroutine.close())

    with caplog.at_level(logging.ERROR):
        save = service.persist_run_payload_sync(engine_payload(symbol=""))

    assert save.status == "failed" and save.run_id is None and reached == []
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
        save = service.persist_run_payload_sync(
            lean_payload("companion-pg-0", parity_group_id="pg-0", requested_engine="both")
        )

    assert save.status == "saved" and save.run_id == 5  # the row landed; only the verdict did not
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
        save = service.persist_run_payload_sync(engine_payload())

    # #2464: a wait that expired before the insert committed is UNKNOWN —
    # the writer loop keeps going uncancelled, so the row may still land.
    # On master this came back as the same ``None`` a failure does, which
    # is exactly the collapse the typed outcome exists to prevent.
    assert save == service.SAVE_UNKNOWN
    assert save.status != "failed"
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
        save = service.persist_run_payload_sync(engine_payload())

    assert save == service.SAVE_FAILED
    assert "Run not persisted" in caplog.text
    assert "outcome unknown" not in caplog.text


_NO_RESULT_DETAIL = "No normalized/result.json — LEAN run did not produce output"


@pytest.mark.parametrize(
    ("parity_group_id", "failure_fields", "failure_detail"),
    [
        # #1977: the payload's failure detail is validated onto the record, then routed.
        ("pg-1", {"parity_failure_detail": _NO_RESULT_DETAIL}, _NO_RESULT_DETAIL),
        ("pg-2", {}, None),
    ],
    ids=["failure-detail-reaches-the-settle-1977", "real-result-carries-no-failure-detail"],
)
def test_the_companions_failure_detail_reaches_the_settle_from_the_record(
    monkeypatch: pytest.MonkeyPatch, parity_group_id: str, failure_fields: dict, failure_detail: str | None
) -> None:
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
        lean_payload(f"companion-{parity_group_id}", parity_group_id=parity_group_id, requested_engine="both", **failure_fields)
    )

    assert run_id == SaveOutcome(status="saved", run_id=5)
    assert settled == [{"right_run_id": 5, "parity_group_id": parity_group_id, "failure_detail": failure_detail}]


def test_a_failure_detail_without_a_group_is_refused_by_the_converter() -> None:
    """The detail is a routing instruction for a parity group; it needs one."""
    with pytest.raises(RunPayloadError, match="lean-sidecar"):
        record_from_payload(engine_payload() | {"parity_failure_detail": "boom"})

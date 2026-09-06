"""Best-effort persistence: a storage failure yields a null run id and a log, never a failed run."""

from __future__ import annotations

import logging

import pytest

from app.research.backtest_runs import service
from tests.research.backtest_runs.payloads import engine_payload


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


def test_a_failed_verdict_freeze_is_logged_and_leaves_the_run_persisted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing_run_sync(coroutine):
        coroutine.close()
        raise RuntimeError("compare exploded")

    monkeypatch.setattr(service, "run_sync", failing_run_sync)

    with caplog.at_level(logging.ERROR):
        service.freeze_parity_sync(right_run_id=7, parity_group_id="pg-1")

    assert "verdict left pending" in caplog.text


@pytest.mark.asyncio
async def test_the_coroutine_form_runs_the_same_write_off_the_calling_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "persist_run_payload_sync", lambda payload: 99)

    assert await service.persist_run_payload(engine_payload()) == 99

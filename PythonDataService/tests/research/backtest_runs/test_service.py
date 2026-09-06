"""Best-effort persistence: a storage failure yields a null run id and a log, never a failed run."""

from __future__ import annotations

import logging

import pytest

from app.research.backtest_runs import service
from app.research.backtest_runs.repository import InsertOutcome
from tests.research.backtest_runs.payloads import engine_payload, lean_payload


def _run_sync_returning(monkeypatch: pytest.MonkeyPatch, outcome: InsertOutcome) -> None:
    """Stand in for the writer loop: close the coroutine (never awaited) and return ``outcome``."""

    def fake_run_sync(coroutine):
        coroutine.close()
        return outcome

    monkeypatch.setattr(service, "run_sync", fake_run_sync)


def test_a_database_failure_leaves_the_run_id_null_and_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def failing_run_sync(coroutine):
        coroutine.close()
        raise ConnectionError("database down")

    monkeypatch.setattr(service, "run_sync", failing_run_sync)

    with caplog.at_level(logging.ERROR):
        run_id = service.persist_run_payload_sync(engine_payload())

    assert run_id is None
    assert "Run not persisted" in caplog.text and "database down" in caplog.text


def test_an_invalid_payload_never_reaches_the_writer(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    reached: list = []
    monkeypatch.setattr(service, "run_sync", lambda coroutine: reached.append(coroutine) or coroutine.close())

    with caplog.at_level(logging.ERROR):
        run_id = service.persist_run_payload_sync(engine_payload(symbol=""))

    assert run_id is None and reached == []
    assert "symbol is required" in caplog.text


def test_a_persisted_engine_run_returns_its_id_and_freezes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_sync_returning(monkeypatch, InsertOutcome(run_id=42, created=True))
    frozen: list = []
    monkeypatch.setattr(service, "freeze_parity_sync", lambda **kwargs: frozen.append(kwargs))

    assert service.persist_run_payload_sync(engine_payload(parity_group_id="pg-1", requested_engine="both")) == 42
    assert frozen == []


def test_a_persisted_lean_companion_freezes_its_group_after_the_row_is_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_sync_returning(monkeypatch, InsertOutcome(run_id=7, created=False))
    frozen: list = []
    monkeypatch.setattr(service, "freeze_parity_sync", lambda **kwargs: frozen.append(kwargs))

    assert service.persist_run_payload_sync(lean_payload("companion-pg-1", parity_group_id="pg-1", requested_engine="both")) == 7
    assert frozen == [{"right_run_id": 7, "parity_group_id": "pg-1"}]


def test_a_failed_verdict_freeze_is_logged_and_leaves_the_run_persisted(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
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

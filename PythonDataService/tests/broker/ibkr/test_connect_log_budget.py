"""#2080 — 350 lines per clerk per 30 min of an expected outage is an ops hazard."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.connect_log_budget import CONNECT_LOG_BUDGET


@pytest.fixture(autouse=True)
def _fresh_budget():
    CONNECT_LOG_BUDGET.reset_for_testing()
    yield
    CONNECT_LOG_BUDGET.reset_for_testing()


def _refusing_ib() -> tuple:
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError(111, "Connection refused"))
    fake_ib.disconnect = MagicMock(return_value=None)
    fake_ib.isConnected = MagicMock(return_value=False)
    fake_ib.client = MagicMock()
    return fake_ib, MagicMock(return_value=fake_ib)


@pytest.mark.asyncio
async def test_a_sustained_outage_logs_once_not_once_per_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)
    _fake_ib, fake_class = _refusing_ib()
    caplog.set_level(logging.WARNING, logger="app.broker.ibkr.client")

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings)
        for _ in range(10):
            with pytest.raises(BrokerError):
                await client.connect()

    failures = [r for r in caplog.records if "IBKR connect attempt" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].__dict__["action"] == "ibkr_connect_failed"


@pytest.mark.asyncio
async def test_the_suppressed_count_is_reported_when_the_window_elapses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)
    _fake_ib, fake_class = _refusing_ib()
    clock = {"now": 1_700_000_000_000}
    CONNECT_LOG_BUDGET.reset_for_testing(now_ms=lambda: clock["now"])
    caplog.set_level(logging.WARNING, logger="app.broker.ibkr.client")

    with patch("ib_async.IB", fake_class):
        client = IbkrClient(settings)
        for _ in range(5):
            with pytest.raises(BrokerError):
                await client.connect()
        clock["now"] += 15 * 60 * 1000
        with pytest.raises(BrokerError):
            await client.connect()

    summaries = [r for r in caplog.records if r.__dict__.get("action") == "ibkr_connect_still_failing"]
    assert len(summaries) == 1
    assert summaries[0].__dict__["suppressed_attempts"] == 4


def test_the_ib_async_duplicate_errors_are_dropped_only_while_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ib_logger = logging.getLogger("ib_async.client")
    caplog.set_level(logging.ERROR, logger="ib_async.client")

    ib_logger.error("API connection failed: ConnectionRefusedError(111, 'Connection refused')")
    ib_logger.error("Make sure API port on TWS/IBG is open")
    CONNECT_LOG_BUDGET.note_failure(ConnectionRefusedError(111, "Connection refused"))
    ib_logger.error("API connection failed: ConnectionRefusedError(111, 'Connection refused')")
    ib_logger.error("Make sure API port on TWS/IBG is open")
    ib_logger.error("API connection failed: TimeoutError()")

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Make sure API port on TWS/IBG is open") == 1
    assert "API connection failed: TimeoutError()" in messages

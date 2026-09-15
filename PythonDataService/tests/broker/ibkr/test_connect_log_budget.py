"""#2080 — 350 lines per clerk per 30 min of an expected outage is an ops hazard."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.connect_log_budget import (
    CONNECT_LOG_BUDGET,
    SUPPRESSION_WINDOW_MS,
    ConnectLogBudget,
    _IbAsyncConnectNoiseFilter,
    install_ib_async_noise_filter,
)


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
        # 3 is enough to prove "once, not once per attempt" — the assertion
        # below is len == 1 regardless of how many attempts run.
        for _ in range(3):
            with pytest.raises(BrokerError):
                await client.connect()

    failures = [r for r in caplog.records if "IBKR connect attempt" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].__dict__["action"] == "ibkr_connect_failed"


@pytest.mark.asyncio
async def test_a_shape_change_reports_its_discarded_suppressed_count_on_the_wire(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The client's ``ibkr_connect_failed`` WARNING for a shape-change
    report carries ``suppressed_attempts`` so the count CONNECT_LOG_BUDGET
    discards on the reset (#2113) actually reaches the log, not just the
    budget's own field."""
    settings = IbkrSettings(mode="paper", port=4002, connect_attempts=1, _env_file=None)
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock(
        side_effect=[
            ConnectionRefusedError(111, "Connection refused"),
            ConnectionRefusedError(111, "Connection refused"),
            ConnectionRefusedError(111, "Connection refused"),
            TimeoutError(),
        ]
    )
    fake_ib.disconnect = MagicMock(return_value=None)
    fake_ib.isConnected = MagicMock(return_value=False)
    fake_ib.client = MagicMock()
    caplog.set_level(logging.WARNING, logger="app.broker.ibkr.client")

    with patch("ib_async.IB", MagicMock(return_value=fake_ib)):
        client = IbkrClient(settings)
        for _ in range(4):
            with pytest.raises(BrokerError):
                await client.connect()

    failures = [r for r in caplog.records if r.__dict__.get("action") == "ibkr_connect_failed"]
    assert len(failures) == 2
    assert failures[0].__dict__["suppressed_attempts"] == 0
    assert failures[1].__dict__["suppressed_attempts"] == 2


def test_the_suppressed_count_is_reported_when_the_window_elapses() -> None:
    """Direct against ``ConnectLogBudget`` — no ``IbkrClient``, no socket,
    no ``asyncio.sleep``. Five failures inside the window: one
    ``report_first`` then four ``suppress``. A sixth failure after the
    window elapses reports the summary, naming the 4 it suppressed."""
    clock = {"now": 1_700_000_000_000}
    budget = ConnectLogBudget(now_ms=lambda: clock["now"])
    exc = ConnectionRefusedError(111, "Connection refused")

    verdicts = [budget.note_failure(exc) for _ in range(5)]
    clock["now"] += SUPPRESSION_WINDOW_MS
    verdicts.append(budget.note_failure(exc))

    assert verdicts == ["report_first", "suppress", "suppress", "suppress", "suppress", "report_summary"]
    assert budget.suppressed_attempts == 4


def test_a_shape_change_reports_the_suppressed_count_it_resets() -> None:
    """Regression for the #2113 P2 finding on #2089: a shape change (a new
    exception type mid-outage) resets ``_suppressed`` to start counting the
    new shape, but that reset used to be invisible -- the discarded count
    was never copied anywhere the caller could log it. Three suppressed
    ``ConnectionRefusedError`` attempts, then a ``TimeoutError`` (a shape
    change) must surface those 3 on ``suppressed_attempts`` exactly as the
    window-elapsed summary path already does, not silently drop them."""
    clock = {"now": 1_700_000_000_000}
    budget = ConnectLogBudget(now_ms=lambda: clock["now"])
    refused = ConnectionRefusedError(111, "Connection refused")
    timed_out = TimeoutError()

    verdicts = [budget.note_failure(refused) for _ in range(4)]
    verdicts.append(budget.note_failure(timed_out))

    assert verdicts == ["report_first", "suppress", "suppress", "suppress", "report_first"]
    assert budget.suppressed_attempts == 3


def test_the_ib_async_duplicate_errors_are_dropped_only_while_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_ib_async_noise_filter()
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


def test_installing_the_noise_filter_twice_leaves_exactly_one() -> None:
    ib_logger = logging.getLogger("ib_async.client")
    for existing in [f for f in ib_logger.filters if isinstance(f, _IbAsyncConnectNoiseFilter)]:
        ib_logger.removeFilter(existing)

    install_ib_async_noise_filter()
    install_ib_async_noise_filter()

    installed = [f for f in ib_logger.filters if isinstance(f, _IbAsyncConnectNoiseFilter)]
    assert len(installed) == 1

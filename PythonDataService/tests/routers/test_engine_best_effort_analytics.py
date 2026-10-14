"""The two best-effort blocks of the aggregation stage have a stated contract (#1999).

Both used to be ``try: ... except Exception: log`` buried mid-function, where
the handler read as a shrug and there was no name for what a caller gets when
it fires. As functions they have one: ``None`` means *this run has no such
figures*, and a failure to compute them is a missing panel, never a failed
backtest. That is the property worth pinning — a future edit that lets either
raise turns an analytics hiccup into a lost run.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.strategy.base import LoggedTrade
from app.services import engine_backtest_service as engine_module
from app.services.engine_backtest_service import _lean_parity_statistics, _validation_analytics


class _Result:
    """The parts of ``BacktestResult`` these two read."""

    def __init__(self, *, bars: list[Any] | None = None, equity_curve: list[Any] | None = None) -> None:
        self.bars = bars if bars is not None else []
        self.equity_curve = equity_curve if equity_curve is not None else []
        self.order_events: list[Any] = []
        self.insights: list[Any] = []
        self.initial_cash = Decimal("100000")
        self.total_fees = Decimal("0")


def test_lean_statistics_are_absent_rather_than_wrong_when_there_is_nothing_to_compare() -> None:
    """This optional panel stays absent; aggregation separately rejects zero-bar runs."""
    assert _lean_parity_statistics(result=_Result(bars=[], equity_curve=[]), trades=[]) is None
    assert _lean_parity_statistics(result=_Result(bars=[object()]), trades=[]) is None


def test_a_lean_statistics_failure_leaves_the_run_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison is an oracle. Losing it must not lose the run it describes."""
    reached: list[bool] = []

    def boom(*args: object, **kwargs: object) -> None:
        reached.append(True)
        raise ValueError("statistics refused this shape")

    monkeypatch.setattr(engine_module, "compute_lean_statistics", boom)
    bar = SimpleNamespace(start_ms=1, open=1, high=1, low=1, close=1, volume=1)
    trade = LoggedTrade(
        entry_time_ms=0, entry_price=Decimal(1), exit_time_ms=1, exit_price=Decimal(2), quantity=1,
        pnl_pts=Decimal(1), pnl_pct=Decimal("0.01"), result="WIN",
    )

    assert _lean_parity_statistics(result=_Result(bars=[bar]), trades=[trade]) is None
    assert reached == [True], "the failing statistics were never reached; the absence proved nothing"


def test_a_validation_analytics_failure_is_reported_and_absent_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An analytics failure is a missing panel; the operator is told, not the stack."""
    logs: list[str] = []

    with caplog.at_level(logging.ERROR, logger="app.services.engine_backtest_service"):
        result = _validation_analytics(
            result=_Result(),
            request=object(),  # type: ignore[arg-type] - rejected before it is read
            formatted_trades=[object()],  # type: ignore[list-item] - not a trade; the builder refuses it
            equity_curve=[],
            on_log=logs.append,
        )

    assert result is None
    assert logs and "Validation analytics unavailable" in logs[0]
    assert caplog.records, "the failure was swallowed without a traceback"

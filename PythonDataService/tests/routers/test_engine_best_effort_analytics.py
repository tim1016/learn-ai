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
from typing import Any

import pytest

from app.routers import engine as engine_module
from app.routers.engine import _lean_parity_statistics, _validation_analytics


class _Result:
    """The parts of ``BacktestResult`` these two read."""

    def __init__(self, *, bars: list[Any] | None = None, equity_curve: list[Any] | None = None) -> None:
        self.bars = bars if bars is not None else []
        self.equity_curve = equity_curve if equity_curve is not None else []
        self.order_events: list[Any] = []
        self.insights: list[Any] = []


def test_lean_statistics_are_absent_rather_than_wrong_when_there_is_nothing_to_compare() -> None:
    """No bars or no trades is not a failure — it is a run with no comparison."""
    assert _lean_parity_statistics(result=_Result(bars=[], equity_curve=[]), trades=[]) is None
    assert _lean_parity_statistics(result=_Result(bars=[object()]), trades=[]) is None


def test_a_lean_statistics_failure_leaves_the_run_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison is an oracle. Losing it must not lose the run it describes."""

    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("statistics refused this shape")

    monkeypatch.setattr(engine_module, "compute_lean_statistics", boom)

    assert _lean_parity_statistics(result=_Result(bars=[object()]), trades=[object()]) is None


def test_a_validation_analytics_failure_is_reported_and_absent_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An analytics failure is a missing panel; the operator is told, not the stack."""
    logs: list[str] = []

    with caplog.at_level(logging.ERROR, logger="app.routers.engine"):
        result = _validation_analytics(
            result=_Result(),
            request=object(),  # type: ignore[arg-type] - rejected before it is read
            strategy=object(),  # type: ignore[arg-type]
            formatted_trades=[object()],  # type: ignore[list-item] - not a trade; the builder refuses it
            equity_curve=[],
            on_log=logs.append,
        )

    assert result is None
    assert logs and "Validation analytics unavailable" in logs[0]
    assert caplog.records, "the failure was swallowed without a traceback"

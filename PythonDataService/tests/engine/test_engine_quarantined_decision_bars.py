"""A backtest says which decision bars its session refused (#2909).

A 15-minute bucket whose last minute has no bar -- a thin symbol, an extended
session -- arrives one minute short, and the session quarantines it rather
than deciding on it: the strategy's indicators skip it and no decision is
recorded. Before #2909 the run dropped that bar in silence; now the refusal
is the result's, with its window and reason, and the run's log says it.

Whether the engine should instead evaluate such a bar, as LEAN's
consolidator reportedly does, is unsettled (issue #2909): it needs a LEAN
run to decide. Saying which bars were refused does not wait for that.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, BacktestResult, QuarantinedDecisionBar
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import Strategy
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.lean_sidecar import trading_calendar
from app.utils.timestamps import ny_datetime

_MINUTE_MS = 60_000
_BUCKET_MS = 15 * _MINUTE_MS

REGULAR_DAY = date(2026, 2, 9)


def _session_minutes(day: date, base_price: Decimal) -> list[TradeBar]:
    """Every regular-session minute of ``day``, each priced uniquely."""
    window = trading_calendar.session_window_for_date(day)
    bars: list[TradeBar] = []
    for index, start_ms in enumerate(range(window.open_ms_utc, window.close_ms_utc, _MINUTE_MS)):
        price = base_price + Decimal(index) / 100
        bars.append(
            TradeBar(
                symbol="SPY",
                start_ms=start_ms,
                end_ms=start_ms + _MINUTE_MS,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1_000,
            )
        )
    return bars


class _Stream:
    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        yield from self._bars


class _ScriptedSignalProgram(Strategy):
    """A registered-style Signal Program that ENTERs on one bucket's close."""

    def __init__(self, *, first_day: date, last_day: date, enter_at_ms: int) -> None:
        super().__init__()
        self._first_day = first_day
        self._last_day = last_day
        self._enter_at_ms = enter_at_ms
        self.signal_program = SignalProgram.create(
            self, program_key="quarantine-probe", program_version="v1", timeframe_ms=_BUCKET_MS
        )

    def initialize(self) -> None:
        self.set_start_date(self._first_day.year, self._first_day.month, self._first_day.day)
        self.set_end_date(self._last_day.year, self._last_day.month, self._last_day.day)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(milliseconds=_BUCKET_MS), self._signal_program_handler())

    def evaluate_signal_bar(self, bar: TradeBar) -> SignalDecision:
        kind = (
            SignalIntentKind.ENTER
            if bar.end_ms == self._enter_at_ms
            else None
        )
        return SignalDecision(
            intent=None if kind is None else SignalIntent(kind=kind, bar_close_ms=bar.end_ms, intended_price=bar.close),
            ready=True,
            relation_facts={},
            signal_facts={},
            reason_evidence={},
            action_plan_request=None,
        )

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None:
        assert self.ctx is not None
        self.ctx.emit_signal_intent(intent)

    def signal_program_settings(self) -> dict[str, str]:
        return {}


def _run(strategy: Strategy, bars: list[TradeBar]) -> BacktestResult:
    return BacktestEngine(data_source=_Stream(bars), fill_model=FillModel(mode=FillMode.SIGNAL_BAR_CLOSE)).run(strategy)


def test_a_bucket_whose_last_minute_has_no_bar_is_reported_as_refused() -> None:
    window = trading_calendar.session_window_for_date(REGULAR_DAY)
    # The 10:00-10:15 bucket: complete when every minute trades, one minute
    # short once the 10:14 minute is gone.
    bucket_start_ms = window.open_ms_utc + 30 * _MINUTE_MS
    bucket_close_ms = bucket_start_ms + _BUCKET_MS
    bars = [
        bar for bar in _session_minutes(REGULAR_DAY, Decimal("100"))
        if bar.start_ms != bucket_close_ms - _MINUTE_MS
    ]
    strategy = _ScriptedSignalProgram(
        first_day=REGULAR_DAY, last_day=REGULAR_DAY, enter_at_ms=bucket_close_ms
    )

    result = _run(strategy, bars)

    assert result.quarantined_decision_bars == [
        QuarantinedDecisionBar(
            bar_start_ms=bucket_start_ms,
            bar_end_ms=bucket_close_ms - _MINUTE_MS,
            reason="TIMEFRAME_MISMATCH",
            expected_timeframe_ms=_BUCKET_MS,
        )
    ]
    assert result.order_events == []
    assert [line for line in result.log_lines if line.startswith("[QUARANTINE]")] == [
        "[QUARANTINE] TIMEFRAME_MISMATCH refused the decision bar ending "
        f"{ny_datetime(bucket_close_ms - _MINUTE_MS)}; the run made no decision on it (#2909)"
    ]


def test_a_complete_session_records_no_refused_decision_bar() -> None:
    window = trading_calendar.session_window_for_date(REGULAR_DAY)
    bucket_start_ms = window.open_ms_utc + 30 * _MINUTE_MS
    strategy = _ScriptedSignalProgram(
        first_day=REGULAR_DAY, last_day=REGULAR_DAY, enter_at_ms=bucket_start_ms + _BUCKET_MS
    )

    result = _run(strategy, _session_minutes(REGULAR_DAY, Decimal("100")))

    assert result.quarantined_decision_bars == []
    assert result.order_events != []

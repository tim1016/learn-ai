"""The closing-bar rule inside ``BacktestEngine`` (#2607).

Live decides the bucket that ends at the session close only after the close,
so it cannot trade it. The backtest therefore does not either: outside the
LEAN-compatibility profile, a Signal Program decision on the closing bar
settles DISCARD -- the refused-decision path live takes for the same bar. An
ENTER is dropped; an EXIT stays due and fires on the next session's first
decision. The LEAN profile keeps filling such a decision at the next open,
because LEAN does.

Every session boundary below comes from the canonical calendar
(``app.lean_sidecar.trading_calendar``); no session time is written here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, BacktestResult, pin_strategy_window
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode
from app.engine.strategy.base import Strategy
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.lean_sidecar import trading_calendar
from tests._helpers.bot_runner.ema_parity import EMA_LAST_BAR_ENTER_DAY, lean_cell_bars

_MINUTE_MS = 60_000
_BUCKET_MS = 15 * _MINUTE_MS

# 2026-02-09 / 2026-02-10: consecutive regular sessions.
REGULAR_DAY = date(2026, 2, 9)
NEXT_REGULAR_DAY = date(2026, 2, 10)
# 2025-11-28, the day after Thanksgiving, is an early close; 2025-12-01 follows it.
HALF_DAY = date(2025, 11, 28)
AFTER_HALF_DAY = date(2025, 12, 1)

_NON_LEAN_MODES = (FillMode.SIGNAL_BAR_CLOSE, FillMode.NEXT_BAR_OPEN)


def _session_minutes(day: date, base_price: Decimal) -> list[TradeBar]:
    """Every regular-session minute of ``day``, each priced uniquely.

    The price climbs a cent a minute from ``base_price``, so a fill's price
    names the minute it came from.
    """
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


def _minute_at(bars: list[TradeBar], start_ms: int) -> TradeBar:
    return next(bar for bar in bars if bar.start_ms == start_ms)


class _Stream:
    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        yield from self._bars


class _ScriptedSignalProgram(Strategy):
    """A registered-style Signal Program whose decisions are scripted by bucket close.

    ENTER on the bucket closing at ``enter_at_ms`` while flat. EXIT on every
    bucket closing at or after ``exit_from_ms`` while holding -- a level, like
    the sealed programs' exits, so a refused EXIT is due again on the next
    bucket. Custody moves only inside ``commit_signal_decision``, as every
    Signal Program's must.
    """

    def __init__(self, *, first_day: date, last_day: date, enter_at_ms: int, exit_from_ms: int | None = None) -> None:
        super().__init__()
        self._first_day = first_day
        self._last_day = last_day
        self._enter_at_ms = enter_at_ms
        self._exit_from_ms = exit_from_ms
        self._in_position = False
        self.signal_program = SignalProgram.create(
            self, program_key="closing-bar-probe", program_version="v1", timeframe_ms=_BUCKET_MS
        )

    def initialize(self) -> None:
        self.set_start_date(self._first_day.year, self._first_day.month, self._first_day.day)
        self.set_end_date(self._last_day.year, self._last_day.month, self._last_day.day)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(milliseconds=_BUCKET_MS), self._signal_program_handler())

    def evaluate_signal_bar(self, bar: TradeBar) -> SignalDecision:
        kind: SignalIntentKind | None = None
        if not self._in_position and bar.end_ms == self._enter_at_ms:
            kind = SignalIntentKind.ENTER
        elif self._in_position and self._exit_from_ms is not None and bar.end_ms >= self._exit_from_ms:
            kind = SignalIntentKind.EXIT
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
        self._in_position = intent.kind is SignalIntentKind.ENTER

    def signal_program_settings(self) -> dict[str, str]:
        return {}


def _run(strategy: Strategy, bars: list[TradeBar], fill_model: FillModel) -> BacktestResult:
    return BacktestEngine(data_source=_Stream(bars), fill_model=fill_model).run(strategy)


@pytest.mark.parametrize("fill_mode", _NON_LEAN_MODES)
@pytest.mark.parametrize("next_session_held", [True, False], ids=["next-session-held", "data-ends-at-the-close"])
def test_a_closing_bar_enter_produces_no_trade(fill_mode: FillMode, next_session_held: bool) -> None:
    close_ms = trading_calendar.session_close_ms_utc(REGULAR_DAY)
    day_one = _session_minutes(REGULAR_DAY, Decimal("100"))
    bars = day_one + (_session_minutes(NEXT_REGULAR_DAY, Decimal("200")) if next_session_held else [])
    strategy = _ScriptedSignalProgram(first_day=REGULAR_DAY, last_day=NEXT_REGULAR_DAY, enter_at_ms=close_ms)

    result = _run(strategy, bars, FillModel(mode=fill_mode))

    assert result.order_events == []
    assert [(skip.bar_close_ms, skip.intent) for skip in result.closing_bar_skips] == [
        (close_ms, SignalIntentKind.ENTER)
    ]
    assert result.closing_bar_skips[0].close_price == day_one[-1].close


@pytest.mark.parametrize("fill_mode", _NON_LEAN_MODES)
def test_a_closing_bar_exit_fires_on_the_next_sessions_first_decision(fill_mode: FillMode) -> None:
    close_ms = trading_calendar.session_close_ms_utc(REGULAR_DAY)
    next_open_ms = trading_calendar.session_open_ms_utc(NEXT_REGULAR_DAY)
    day_one = _session_minutes(REGULAR_DAY, Decimal("100"))
    day_two = _session_minutes(NEXT_REGULAR_DAY, Decimal("200"))
    strategy = _ScriptedSignalProgram(
        first_day=REGULAR_DAY,
        last_day=NEXT_REGULAR_DAY,
        enter_at_ms=close_ms - _BUCKET_MS,
        exit_from_ms=close_ms,
    )

    result = _run(strategy, day_one + day_two, FillModel(mode=fill_mode, commission_per_order=Decimal(0)))

    entry, exit_ = result.order_events
    assert entry.direction is Direction.LONG
    assert exit_.direction is Direction.SHORT
    first_decision_close_ms = next_open_ms + _BUCKET_MS
    if fill_mode is FillMode.SIGNAL_BAR_CLOSE:
        # Filled at the close of the bucket that decided it: the next
        # session's first bucket, not the closing bar the EXIT was first due on.
        assert exit_.filled_at_ms == first_decision_close_ms
        assert exit_.fill_price == _minute_at(day_two, first_decision_close_ms - _MINUTE_MS).close
    else:
        # The minute after the one that completed the next session's first bucket.
        assert exit_.filled_at_ms == first_decision_close_ms + _MINUTE_MS
        assert exit_.fill_price == _minute_at(day_two, first_decision_close_ms + _MINUTE_MS).open
    assert [(skip.bar_close_ms, skip.intent) for skip in result.closing_bar_skips] == [
        (close_ms, SignalIntentKind.EXIT)
    ]


@pytest.mark.parametrize("fill_mode", _NON_LEAN_MODES)
def test_an_early_close_bucket_is_the_closing_bar(fill_mode: FillMode) -> None:
    assert trading_calendar.is_early_close(HALF_DAY)
    close_ms = trading_calendar.session_close_ms_utc(HALF_DAY)
    bars = _session_minutes(HALF_DAY, Decimal("100")) + _session_minutes(AFTER_HALF_DAY, Decimal("200"))
    strategy = _ScriptedSignalProgram(first_day=HALF_DAY, last_day=AFTER_HALF_DAY, enter_at_ms=close_ms)

    result = _run(strategy, bars, FillModel(mode=fill_mode))

    assert result.order_events == []
    assert [(skip.bar_close_ms, skip.intent) for skip in result.closing_bar_skips] == [
        (close_ms, SignalIntentKind.ENTER)
    ]


def test_a_decision_before_the_closing_bar_is_untouched() -> None:
    close_ms = trading_calendar.session_close_ms_utc(REGULAR_DAY)
    day_one = _session_minutes(REGULAR_DAY, Decimal("100"))
    strategy = _ScriptedSignalProgram(first_day=REGULAR_DAY, last_day=REGULAR_DAY, enter_at_ms=close_ms - _BUCKET_MS)

    result = _run(strategy, day_one, FillModel(mode=FillMode.SIGNAL_BAR_CLOSE))

    (entry,) = result.order_events
    assert entry.filled_at_ms == close_ms - _BUCKET_MS
    assert result.closing_bar_skips == []


def test_the_lean_profile_still_fills_a_closing_bar_enter_at_the_next_open() -> None:
    close_ms = trading_calendar.session_close_ms_utc(REGULAR_DAY)
    next_open_ms = trading_calendar.session_open_ms_utc(NEXT_REGULAR_DAY)
    day_two = _session_minutes(NEXT_REGULAR_DAY, Decimal("200"))
    strategy = _ScriptedSignalProgram(first_day=REGULAR_DAY, last_day=NEXT_REGULAR_DAY, enter_at_ms=close_ms)

    result = _run(
        strategy,
        _session_minutes(REGULAR_DAY, Decimal("100")) + day_two,
        FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fill_stale_signal_at_current_open=True),
    )

    (entry,) = result.order_events
    assert entry.filled_at_ms == next_open_ms + _MINUTE_MS
    assert entry.fill_price == day_two[0].open
    assert result.closing_bar_skips == []


# QQQ's retained LEAN input (the cross-engine golden cell), whose first
# sealed-EMA ENTER is decided on 2026-02-03's closing bar. The same bars drive
# the live runner's refusal of that ENTER (``test_trade_bot_last_bar_enter.py``).
_QQQ_CELL = "QQQ_W3mo_2026-02-02_to_2026-04-30"
_QQQ_CELL_FIRST_DAY = date(2026, 2, 2)


def _run_sealed_ema_on_qqq(fill_model: FillModel) -> BacktestResult:
    """The registered EMA program over the QQQ cell, through the session after its closing-bar ENTER."""
    through_day = trading_calendar.next_trading_day(EMA_LAST_BAR_ENTER_DAY)
    bars = [
        TradeBar(
            symbol=bar.symbol, start_ms=bar.start_ms, end_ms=bar.end_ms, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume,
        )
        for bar in lean_cell_bars(
            _QQQ_CELL, symbol="QQQ", stop_after_ms=trading_calendar.session_close_ms_utc(through_day) - _MINUTE_MS
        )
    ]
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    strategy = registration.build(registration.param_schema(symbol="QQQ"))
    pin_strategy_window(strategy, _QQQ_CELL_FIRST_DAY, through_day)
    return _run(strategy, bars, fill_model)


@pytest.mark.parametrize("fill_mode", _NON_LEAN_MODES)
def test_the_sealed_emas_closing_bar_enter_on_a_golden_cell_produces_no_trade(fill_mode: FillMode) -> None:
    close_ms = trading_calendar.session_close_ms_utc(EMA_LAST_BAR_ENTER_DAY)
    next_open_ms = trading_calendar.session_open_ms_utc(trading_calendar.next_trading_day(EMA_LAST_BAR_ENTER_DAY))

    result = _run_sealed_ema_on_qqq(FillModel(mode=fill_mode))

    # Nothing entered at the close or on the next session's opening minutes:
    # the program's later trades are its own fresh decisions.
    entries = [event.filled_at_ms for event in result.order_events if event.direction is Direction.LONG]
    assert all(filled_at_ms > next_open_ms + _BUCKET_MS for filled_at_ms in entries)
    assert [(skip.bar_close_ms, skip.intent) for skip in result.closing_bar_skips] == [
        (close_ms, SignalIntentKind.ENTER)
    ]


def test_the_lean_profile_fills_the_same_golden_enter_at_the_next_open() -> None:
    next_open_ms = trading_calendar.session_open_ms_utc(trading_calendar.next_trading_day(EMA_LAST_BAR_ENTER_DAY))

    result = _run_sealed_ema_on_qqq(FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fill_stale_signal_at_current_open=True))

    entry = result.order_events[0]
    assert (entry.direction, entry.filled_at_ms) == (Direction.LONG, next_open_ms + _MINUTE_MS)
    assert result.closing_bar_skips == []

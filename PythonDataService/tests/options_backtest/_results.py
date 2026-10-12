"""Sessions built by hand, so a statistic can be worked out on paper from the trades written down here.

The real dataclasses are built, not mocks. A trade's profit is written out by the plain arithmetic the owner would do:
(sold - bought) x 100 x contracts, less its fees. A leg marked, not sold, is a trade whose exit is a ``Mark``.
"""

from __future__ import annotations

from datetime import date

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.results import (
    Event,
    EventKind,
    ExitReason,
    Fill,
    LegTrade,
    Mark,
    SessionOutcome,
    SessionStatus,
    SkipReason,
)
from app.options_backtest.session_result import SessionPins, SessionResult
from tests.options_backtest._run_data import D, contract_on

NO_PINS = SessionPins(None, None, ())


def a_trade(
    leg_id: str,
    *,
    entry: tuple[str, str],
    exit: tuple[str, str] | None = None,
    mark: str | None = None,
    reason: ExitReason,
    contracts: int = 1,
    fees: tuple[str, str] = ("0", "0"),
    day: date = date(2026, 10, 7),
) -> LegTrade:
    """A trade with its prices as ``(seen, filled)``. A leg marked, not sold, gives ``mark`` and no exit."""
    option = contract_on(day, "call", 500)
    bought = Fill(leg_id, option, "buy", contracts, 100, 0, D(entry[0]), D(entry[1]))
    leaving: Fill | Mark
    if mark is not None:
        leaving, sold, exit_fee = Mark(0, D(mark)), D(mark), D("0")
    else:
        assert exit is not None
        leaving, sold, exit_fee = (
            Fill(leg_id, option, "sell", contracts, 100, 0, D(exit[0]), D(exit[1])),
            D(exit[1]),
            D(fees[1]),
        )
    profit = (sold - D(entry[1])) * 100 * contracts - D(fees[0]) - exit_fee
    return LegTrade(
        leg_id=leg_id,
        entry_number=0,
        contract=option,
        contracts=contracts,
        multiplier=100,
        starting_price=None,
        momentum_level=None,
        stop_level=None,
        entry_decided_at_ms=0,
        entry=bought,
        exit_reason=reason,
        exit_group=1,
        exit_decided_at_ms=0,
        exit=leaving,
        entry_fee_usd=D(fees[0]),
        exit_fee_usd=exit_fee,
        pnl_usd=profit,
    )


def an_event(kind: EventKind, leg_id: str = "a", reason: str | None = None) -> Event:
    return Event(kind, 0, leg_id, 0, reason=reason)


def a_session(
    day: date,
    cash_before: str,
    *,
    status: SessionStatus,
    trades: tuple[LegTrade, ...] = (),
    events: tuple[Event, ...] = (),
    skip_reason: SkipReason | None = None,
) -> SessionResult:
    """A session whose cash after is its cash before plus its profit, as a run carries it."""
    marked_value = sum(
        (
            trade.exit_price * trade.multiplier * trade.contracts
            for trade in trades
            if trade.exit_reason is ExitReason.MARKED_NOT_SOLD
        ),
        start=D("0"),
    )
    profit = sum((trade.pnl_usd for trade in trades), start=D("0"))
    outcome = SessionOutcome(status, skip_reason, trades, events, profit - marked_value)
    window = session_window_for_date(day)
    return SessionResult(
        trading_date=day,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        cash_before_usd=D(cash_before),
        outcome=outcome,
        cash_after_usd=D(cash_before) + profit,
        pins=NO_PINS,
    )

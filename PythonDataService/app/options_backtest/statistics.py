"""What an owner reads of an options backtest run: counts, dollars and rates, all exact (#3030).

Formula: net profit        = Σ trade profit, a trade's profit being after its fees and slippage
    gross profit / loss = Σ profit of the won trades / Σ -profit of the lost trades (a positive number)
    profit factor       = gross profit / gross loss, none when no trade lost
    daily win rate      = profitable sessions / traded sessions;  trade win rate = won trades / trades
    equity after session k = starting cash + Σ profit of sessions 1..k, marked legs at their marks
    drawdown            = the largest fall of equity below its running peak, the peak starting at the starting
                          cash: in dollars (peak - trough) and as a fraction (peak - trough) / peak
    slippage paid       = Σ over fills of |filled - seen| · multiplier · contracts
Reference: the owner's definitions in #3030 (a trade is one leg round trip; daily and trade win rates name what they
  count; an incomplete day stays in the totals at its mark). Maximum drawdown as a fraction of the running peak:
  Bacon, Practical Portfolio Performance Measurement (2e), §8.2.
Canonical implementation: this file, for the figures of an options run in exact dollars. The stock engine's
  ``app/engine/results/statistics.py`` is float, percent-based and built for all-in stock trades, so it cannot answer
  these; the one figure both have, the fraction a drawdown falls, is held to it by a parity test.
Validated against: tests/options_backtest/test_statistics.py and tests/options_backtest/test_drawdown.py, whose
  expectations are worked by hand; the drawdown fraction is compared with the engine's ``max_drawdown`` at
  ``atol=1e-12, rtol=0``.

Everything is exact ``Decimal`` dollars and whole counts; nothing is a float. A rate is a ``Ratio`` of two numbers and
nothing is rounded: ``rounded_to_6_places`` gives the one rounded figure for display, half-even, and says so in its
name. The drawdown is measured on the equity at the end of each session, and not inside a day.

What the cash could carry is a verdict apart from these totals, which keep every session at its mark. The capital
evidence, the cash ladder and the stamps a run carries beside them come from ``capital_evidence.py``,
``cash_ladder.py`` and ``run_stamps.py``: ``summarize_run`` leaves them unrecorded, and no figure here depends on them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Literal

from app.options_backtest import fills
from app.options_backtest.capital_evidence import CapitalEvidence
from app.options_backtest.cash_ladder import CashLevel
from app.options_backtest.market import PickRefusal
from app.options_backtest.results import EventKind, ExitReason, Fill, LegTrade, SessionStatus, SkipReason
from app.options_backtest.run_stamps import RunStamps
from app.options_backtest.session_result import SessionResult

_PLACES = 6
_UNIT_IN_THE_LAST_PLACE = 10**_PLACES


@dataclass(frozen=True)
class Ratio:
    """A rate as the two numbers it divides, which is exact where a quotient is not."""

    numerator: Decimal
    denominator: Decimal

    def __post_init__(self) -> None:
        if not self.denominator > 0:
            raise ValueError(f"a ratio divides by something above zero; got {self.denominator}")

    def exact(self) -> Decimal | None:
        """The quotient when it ends, or None when it does not (a third has no last digit)."""
        with localcontext(fills.EXACT):
            try:
                return self.numerator / self.denominator
            except Inexact:
                return None

    def rounded_to_6_places(self) -> Decimal:
        """The quotient rounded half-even to six places, for display. A tie goes to the even last digit."""
        scaled = round(Fraction(self.numerator) / Fraction(self.denominator) * _UNIT_IN_THE_LAST_PLACE)
        with localcontext(fills.EXACT):
            return Decimal(scaled).scaleb(-_PLACES)


@dataclass(frozen=True)
class Drawdown:
    """A fall of equity from a peak to a later trough, measured at the ends of sessions.

    Of two equal falls the one whose trough comes first is reported, and equity that comes back to a peak without
    passing it leaves the peak at the first time it was reached. The comparison is exact (``Decimal`` dollars and
    whole fractions).

    Equity can reach zero or go below it: a sale is never refused and pays its fee even when it fills at nothing, so
    cash can end below zero by a closing fee. The fall is then the whole peak or more, and ``fall_ratio`` is 1 or above
    1: above 1 says the account ended owing money. Nothing is clamped to 1.
    """

    peak_equity_usd: Decimal
    trough_equity_usd: Decimal
    fall_usd: Decimal
    # The fall over the peak.
    fall_ratio: Ratio
    # The sessions are named by their open in int64 ms UTC. None when the peak is the starting cash, before the first
    # session.
    peak_session_open_ms: int | None
    trough_session_open_ms: int
    measured_at: Literal["session_end"] = "session_end"


@dataclass(frozen=True)
class DayRow:
    """One session, as a row of the by-day table."""

    # The session's open in int64 ms UTC, the 09:30 New York anchor of its trading date.
    session_open_ms: int
    status: SessionStatus
    skip_reason: SkipReason | None
    trades: int
    net_profit_usd: Decimal
    fees_usd: Decimal
    # Trades, not sessions: a session of two trades can add to both of these.
    trades_won: int
    trades_lost: int
    trades_break_even: int


@dataclass(frozen=True)
class MonthRow:
    """The sessions of one calendar month that the run covered, the month being that of their New York date."""

    year: int
    month: int
    sessions: int
    sessions_traded: int
    trades: int
    net_profit_usd: Decimal
    fees_usd: Decimal
    # Trades, not sessions or days.
    trades_won: int
    trades_lost: int
    trades_break_even: int
    sessions_by_status: tuple[tuple[SessionStatus, int], ...]


@dataclass(frozen=True)
class RunSummary:
    """Every figure of a run an owner reads, each named for what it counts."""

    # --- sessions
    sessions_tested: int
    # At least one leg trade, an incomplete session included.
    sessions_traded: int
    sessions_no_trade: int
    sessions_skipped: int
    skipped_by_reason: tuple[tuple[SkipReason, int], ...]
    # A leg marked, not sold. A subset of the traded sessions.
    sessions_incomplete: int
    # Among the traded sessions, by the session's profit above, below or exactly zero.
    sessions_profitable: int
    sessions_losing: int
    sessions_break_even: int
    # Profitable sessions over traded sessions.
    daily_win_rate: Ratio | None
    # --- trades (one leg round trip each)
    trades: int
    trades_won: int
    trades_lost: int
    trades_break_even: int
    # Won trades over trades.
    trade_win_rate: Ratio | None
    exits_by_reason: tuple[tuple[ExitReason, int], ...]
    # --- money, in dollars
    starting_cash_usd: Decimal
    net_profit_usd: Decimal
    gross_profit_usd: Decimal
    # The losses of the lost trades added up, as a positive number.
    gross_loss_usd: Decimal
    profit_factor: Ratio | None
    total_fees_usd: Decimal
    # What the fills cost beyond the prices seen: slippage and the rounding to the price step together, since both
    # are in the gap between the price seen and the price filled.
    slippage_paid_usd: Decimal
    # Starting cash plus net profit, a leg marked at its mark.
    ending_cash_usd: Decimal
    # --- drawdown, on equity at each session's end
    drawdown_by_fraction: Drawdown | None
    drawdown_by_dollars: Drawdown | None
    # --- what did not trade
    # Sessions in which a leg took a starting price and no momentum was reached in any leg.
    sessions_without_momentum: int
    entries_refused_for_cash: int
    re_entries_blocked_by_cutoff: int
    # An entry whose contract did not trade in the minute it filled.
    entries_dropped: int
    picks_refused_by_reason: tuple[tuple[PickRefusal, int], ...]
    # A leg whose contract did not trade at the minute its starting price was due.
    legs_without_a_starting_price: int
    # A re-entry that ended without entering for want of market data: its pick was refused, its contract had no
    # starting price, or its entry was dropped at once. Each of those is also counted under its own heading above,
    # so this overlaps them and is not to be added to them: it says how many of those incidents ended a re-entry.
    # A re-entry refused for want of cash is not among them; it is an entry refused for cash.
    re_entries_lost: int
    # --- tables
    by_day: tuple[DayRow, ...]
    by_month: tuple[MonthRow, ...]
    # --- whether the cash could have carried the run (#3108), apart from every figure above, which it never changes.
    # None reads "not recorded": the run was stored before they were, or this summary was made without them.
    capital_evidence: CapitalEvidence | None = None
    cash_ladder: tuple[CashLevel, ...] | None = None
    stamps: RunStamps | None = None


def _counts[E: StrEnum](members: type[E], seen: Iterable[E]) -> tuple[tuple[E, int], ...]:
    """How many times each member of an enum was seen, every member listed in its own order, zeros included."""
    counter = Counter(seen)
    return tuple((member, counter[member]) for member in members)


def _wins_losses_even(trades: Iterable[LegTrade]) -> tuple[int, int, int]:
    won = lost = even = 0
    for trade in trades:
        if trade.pnl_usd > 0:
            won += 1
        elif trade.pnl_usd < 0:
            lost += 1
        else:
            even += 1
    return won, lost, even


def _sum(amounts: Iterable[Decimal]) -> Decimal:
    with localcontext(fills.EXACT):
        return sum(amounts, start=Decimal(0))


def _slippage_paid(trade: LegTrade) -> Decimal:
    """What slippage and the price step cost on the fills of one trade; a leg marked, not sold, has one fill."""
    with localcontext(fills.EXACT):
        paid = Decimal(0)
        for fill in (trade.entry, trade.exit):
            if isinstance(fill, Fill):
                paid += abs(fill.filled - fill.seen) * fill.multiplier * fill.contracts
        return paid


def _drawdowns(
    sessions: Sequence[SessionResult], starting_cash_usd: Decimal
) -> tuple[Drawdown | None, Drawdown | None]:
    """The worst fall by fraction and the worst by dollars, the earliest winning a tie."""
    # Points are (session, equity after it); the starting cash is a point before the first session.
    points: list[tuple[SessionResult | None, Decimal]] = [(None, starting_cash_usd)]
    equity = starting_cash_usd
    for session in sessions:
        equity = _sum([equity, session.pnl_usd])
        points.append((session, equity))
    peak_at, peak = 0, starting_cash_usd
    worst_by_fraction: tuple[Fraction, int, int] | None = None
    worst_by_dollars: tuple[Decimal, int, int] | None = None
    for at, (_, after) in enumerate(points):
        if after > peak:
            peak_at, peak = at, after
            continue
        with localcontext(fills.EXACT):
            fall = peak - after
        if fall <= 0:
            continue
        fraction = Fraction(fall) / Fraction(peak)
        if worst_by_fraction is None or fraction > worst_by_fraction[0]:
            worst_by_fraction = (fraction, peak_at, at)
        if worst_by_dollars is None or fall > worst_by_dollars[0]:
            worst_by_dollars = (fall, peak_at, at)

    def describe(peak_at: int, trough_at: int) -> Drawdown:
        peak_session, peak_equity = points[peak_at]
        trough_session, trough_equity = points[trough_at]
        assert trough_session is not None  # a trough comes after a peak, so it is a session
        with localcontext(fills.EXACT):
            fall = peak_equity - trough_equity
        return Drawdown(
            peak_equity_usd=peak_equity,
            trough_equity_usd=trough_equity,
            fall_usd=fall,
            fall_ratio=Ratio(fall, peak_equity),
            peak_session_open_ms=None if peak_session is None else peak_session.session_open_ms,
            trough_session_open_ms=trough_session.session_open_ms,
        )

    return (
        None if worst_by_fraction is None else describe(worst_by_fraction[1], worst_by_fraction[2]),
        None if worst_by_dollars is None else describe(worst_by_dollars[1], worst_by_dollars[2]),
    )


def _never_reached_its_momentum(session: SessionResult) -> bool:
    """Whether a leg took a starting price in the session and no leg's momentum was reached."""
    kinds = {event.kind for event in session.outcome.events}
    return EventKind.STARTING_PRICE_TAKEN in kinds and EventKind.MOMENTUM_HIT not in kinds


def _day_row(session: SessionResult) -> DayRow:
    won, lost, even = _wins_losses_even(session.outcome.trades)
    return DayRow(
        session_open_ms=session.session_open_ms,
        status=session.status,
        skip_reason=session.outcome.skip_reason,
        trades=len(session.outcome.trades),
        net_profit_usd=session.pnl_usd,
        fees_usd=session.fees_usd,
        trades_won=won,
        trades_lost=lost,
        trades_break_even=even,
    )


def _month_rows(sessions: Sequence[SessionResult], days: Sequence[DayRow]) -> tuple[MonthRow, ...]:
    """One row per New York month of the sessions, in order; ``days`` holds the row of each session, in the same order."""
    months: dict[tuple[int, int], list[DayRow]] = {}
    for session, day in zip(sessions, days, strict=True):
        months.setdefault((session.trading_date.year, session.trading_date.month), []).append(day)
    return tuple(
        MonthRow(
            year=year,
            month=month,
            sessions=len(rows),
            sessions_traded=sum(1 for row in rows if row.trades),
            trades=sum(row.trades for row in rows),
            net_profit_usd=_sum(row.net_profit_usd for row in rows),
            fees_usd=_sum(row.fees_usd for row in rows),
            trades_won=sum(row.trades_won for row in rows),
            trades_lost=sum(row.trades_lost for row in rows),
            trades_break_even=sum(row.trades_break_even for row in rows),
            sessions_by_status=_counts(SessionStatus, (row.status for row in rows)),
        )
        for (year, month), rows in months.items()
    )


def summarize_run(sessions: Sequence[SessionResult], *, starting_cash_usd: Decimal) -> RunSummary:
    """Every figure of a run, from its sessions in date order and the cash it started with."""
    traded = [session for session in sessions if session.outcome.trades]
    trades = [trade for session in sessions for trade in session.outcome.trades]
    events = [event for session in sessions for event in session.outcome.events]
    kinds = Counter(event.kind for event in events)
    won, lost, even = _wins_losses_even(trades)
    gross_profit = _sum(trade.pnl_usd for trade in trades if trade.pnl_usd > 0)
    gross_loss = _sum(-trade.pnl_usd for trade in trades if trade.pnl_usd < 0)
    net_profit = _sum(trade.pnl_usd for trade in trades)
    profitable = sum(1 for session in traded if session.pnl_usd > 0)
    days = tuple(_day_row(session) for session in sessions)
    by_fraction, by_dollars = _drawdowns(sessions, starting_cash_usd)
    return RunSummary(
        sessions_tested=len(sessions),
        sessions_traded=len(traded),
        sessions_no_trade=sum(1 for session in sessions if session.status is SessionStatus.NO_TRADE),
        sessions_skipped=sum(1 for session in sessions if session.status is SessionStatus.SKIPPED),
        skipped_by_reason=_counts(
            SkipReason, (session.outcome.skip_reason for session in sessions if session.outcome.skip_reason)
        ),
        sessions_incomplete=sum(1 for session in sessions if session.status is SessionStatus.INCOMPLETE),
        sessions_profitable=profitable,
        sessions_losing=sum(1 for session in traded if session.pnl_usd < 0),
        sessions_break_even=sum(1 for session in traded if session.pnl_usd == 0),
        daily_win_rate=Ratio(Decimal(profitable), Decimal(len(traded))) if traded else None,
        trades=len(trades),
        trades_won=won,
        trades_lost=lost,
        trades_break_even=even,
        trade_win_rate=Ratio(Decimal(won), Decimal(len(trades))) if trades else None,
        exits_by_reason=_counts(ExitReason, (trade.exit_reason for trade in trades)),
        starting_cash_usd=starting_cash_usd,
        net_profit_usd=net_profit,
        gross_profit_usd=gross_profit,
        gross_loss_usd=gross_loss,
        profit_factor=Ratio(gross_profit, gross_loss) if gross_loss > 0 else None,
        total_fees_usd=_sum(trade.entry_fee_usd + trade.exit_fee_usd for trade in trades),
        slippage_paid_usd=_sum(_slippage_paid(trade) for trade in trades),
        ending_cash_usd=_sum([starting_cash_usd, net_profit]),
        drawdown_by_fraction=by_fraction,
        drawdown_by_dollars=by_dollars,
        sessions_without_momentum=sum(1 for session in sessions if _never_reached_its_momentum(session)),
        entries_refused_for_cash=kinds[EventKind.ENTRY_REFUSED_FOR_CASH],
        re_entries_blocked_by_cutoff=kinds[EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF],
        entries_dropped=kinds[EventKind.ENTRY_DROPPED],
        picks_refused_by_reason=_counts(
            PickRefusal,
            (PickRefusal(event.reason) for event in events if event.kind is EventKind.PICK_REFUSED and event.reason),
        ),
        legs_without_a_starting_price=kinds[EventKind.NO_STARTING_PRICE],
        re_entries_lost=kinds[EventKind.RE_ENTRY_LOST],
        by_day=days,
        by_month=_month_rows(sessions, days),
    )

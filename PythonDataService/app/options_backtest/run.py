"""Replay a setup over many sessions, carrying cash from one to the next (#3030).

Formula: cash before session k+1 = cash before k + cash change k + marked value k, which is cash before k + profit k
    slippage tolerance = the highest ladder value s such that net profit is above zero at s and at every lower value
Reference: the owner's rules in #3030: an incomplete day stays in the totals at its mark; the slippage a result
  survives is read from replays at a ladder of slippage values, not from re-pricing fills.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_run_cash.py (the cash carried over sessions), and
  tests/options_backtest/test_run_slippage.py (the ladder and the figure drawn from it).

A run takes the sessions in date order and reads one at a time: the underlying's day and the listing, then the
contracts ``session_needs`` names, then the replay, and then it lets go of them. Only a ``SessionResult`` is kept, so
a run of two years does not hold two years of bars. What is not captured is never skipped quietly: a session whose
underlying day, listing or a needed contract day is missing raises ``SessionDataMissing``, and the service that calls
this checks first. A run that is asked to stop raises ``RunStopped``: a part of a run is never a result.

Slippage tolerance, done exactly. For each value on the ladder the whole session is replayed again at that slippage, on
the same data, with that value's own carried cash. A different slippage moves a stop level and the minute a re-entry
starts, and can turn a cash refusal, so re-pricing the fills of one replay would be wrong exactly where it matters.
The contracts a session can read do not depend on slippage, so one set of loaded days serves every value.

Cash and a leg marked. A bought leg marked, not sold, has paid out and not been paid back; the next session starts
with its mark in cash, and the session's profit counts it at its mark. A sold leg marked, not bought back, has been
paid and owes its mark: the next session starts with that mark out of its cash. The outcome signs the mark that way
(``SessionOutcome.marked_value_usd``), so the carried cash is the same formula. ``SessionResult`` keeps each figure.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, localcontext
from itertools import pairwise

from app.lean_sidecar.trading_calendar import SessionWindow, expected_sessions, session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.data import RunData
from app.options_backtest.fees import FeeRateNotPublished, fee_for_policy, unpublished_fee_types
from app.options_backtest.needs import session_needs
from app.options_backtest.replay import refuse_sold_leg_rules, replay_session
from app.options_backtest.results import SessionOutcome, SessionStatus
from app.options_backtest.session_market import DayMarket, bars_by_close, window_only_market
from app.options_backtest.session_result import PinnedDay, SessionPins, SessionResult
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_backtest.statistics import RunSummary, summarize_run

DEFAULT_SLIPPAGE_LADDER: tuple[Decimal, ...] = tuple(
    Decimal(percent) for percent in ("0", "0.1", "0.2", "0.3", "0.5", "0.75", "1", "1.5", "2", "3", "5")
)


class RunStopped(Exception):
    """The run was asked to stop between two sessions. Nothing of it is a result."""

    def __init__(self, sessions_done: int) -> None:
        self.sessions_done = sessions_done
        super().__init__(f"the run was stopped after {sessions_done} sessions")


class SessionDataMissing(Exception):
    """A session needs data that was not captured, so it cannot be replayed."""

    def __init__(
        self, trading_date: date, *, underlying: bool, listing: bool, contracts: tuple[OptionContract, ...]
    ) -> None:
        self.trading_date = trading_date
        self.underlying = underlying
        self.listing = listing
        self.contracts = contracts
        missing = []
        if underlying:
            missing.append("the underlying's minute bars")
        if listing:
            missing.append("the contract listing")
        if contracts:
            missing.append(f"the bars of {', '.join(contract.occ_symbol for contract in contracts)}")
        super().__init__(f"{trading_date} cannot be replayed: {' and '.join(missing)} not captured")


@dataclass(frozen=True)
class SlippagePoint:
    """A whole run replayed at one slippage."""

    slippage_percent: Decimal
    # The slippage the setup itself carries: this point is the run's own result.
    is_setups_own: bool
    net_profit_usd: Decimal
    trades: int
    traded_sessions: int
    # Traded sessions whose profit was above zero.
    winning_sessions: int


@dataclass(frozen=True)
class SlippageTolerance:
    """How much slippage the result survives, read off the ladder it was replayed at."""

    # The highest ladder value at which, and at every lower value, net profit is above zero; None when even the
    # lowest value is not.
    highest_profitable_percent: Decimal | None
    # The next ladder value up, where net profit is not above zero; None when every value was profitable, which
    # reads "survives the whole ladder", not "survives any slippage".
    first_unprofitable_percent: Decimal | None
    # True when no trade was made at any ladder value: there was no result to survive, which is not the same as a
    # result that lost at every value. Both percents are then None.
    nothing_traded: bool = False


def slippage_tolerance(points: Sequence[SlippagePoint]) -> SlippageTolerance:
    """Walk the ``points`` from the lowest slippage up and stop at the first that does not make a profit.

    A run that made no trade at any value has a profit of exactly nothing at each, and says so as ``nothing_traded``
    and not as a result that lost from the lowest value up.
    """
    if all(point.trades == 0 for point in points):
        return SlippageTolerance(None, None, nothing_traded=True)
    highest: Decimal | None = None
    for point in sorted(points, key=lambda each: each.slippage_percent):
        if point.net_profit_usd <= 0:
            return SlippageTolerance(highest, point.slippage_percent)
        highest = point.slippage_percent
    return SlippageTolerance(highest, None)


@dataclass(frozen=True)
class RunResult:
    """A setup replayed over a list of sessions."""

    setup: OptionsBacktestSetup
    sessions: tuple[SessionResult, ...]
    summary: RunSummary
    # One point per ladder value, lowest first, with the setup's own value among them.
    slippage: tuple[SlippagePoint, ...]
    slippage_tolerance: SlippageTolerance


def _ignore_progress(_done: int, _total: int, _day: date) -> None:
    return None


def _never_stop() -> bool:
    return False


@dataclass
class _Carried:
    """One slippage value's run so far: the setup at that value, the cash it carries, and what it has made."""

    setup: OptionsBacktestSetup
    cash: Decimal
    net_profit: Decimal = Decimal(0)
    trades: int = 0
    traded_sessions: int = 0
    winning_sessions: int = 0

    def take(self, outcome: SessionOutcome) -> None:
        """Carry on from a session: its cash that moved and its marks paid back at their marks, and what it made."""
        pnl = outcome.pnl_usd
        with localcontext(fills.EXACT):
            self.cash = self.cash + outcome.cash_change_usd + outcome.marked_value_usd
            self.net_profit = self.net_profit + pnl
        self.trades += len(outcome.trades)
        if outcome.trades:
            self.traded_sessions += 1
            self.winning_sessions += pnl > 0


def _check_the_run_can_start(setup: OptionsBacktestSetup, sessions: Sequence[date]) -> None:
    refuse_sold_leg_rules(setup)
    if not sessions:
        raise ValueError("a run needs at least one session")
    if any(later <= earlier for earlier, later in pairwise(sessions)):
        raise ValueError("sessions must be given in date order, each once")
    real = set(expected_sessions(sessions[0], sessions[-1]))
    for day in sessions:
        if day not in real:
            raise ValueError(f"{day} is not a NYSE session")
    if setup.fees.kind == "estimated":
        for day in sessions:
            if missing := unpublished_fee_types(day):
                raise FeeRateNotPublished(day, missing)


def _ladder(setup: OptionsBacktestSetup, requested: Sequence[Decimal]) -> dict[Decimal, _Carried]:
    """The run at each slippage value, lowest first, the setup's own value among them, each starting with the cash."""
    own = setup.slippage_percent
    return {
        value: _Carried(
            setup
            if value == own
            else OptionsBacktestSetup.model_validate({**setup.model_dump(mode="python"), "slippage_percent": value}),
            setup.starting_cash_usd,
        )
        for value in sorted({*requested, own})
    }


class _Run:
    """One run's state: what it reads from, how it prices fees, and each ladder value's run so far."""

    def __init__(self, setup: OptionsBacktestSetup, data: RunData, requested: Sequence[Decimal]) -> None:
        self.setup = setup
        self.data = data
        self.fee_for = fee_for_policy(setup.fees)
        self.ladder = _ladder(setup, requested)
        self.own = self.ladder[setup.slippage_percent]

    def replay(self, day: date) -> SessionResult:
        """Replay one session at every ladder value, carry each value's cash on, and keep the setup's own result.

        Everything read is local to this call, so nothing of it outlives the session.
        """
        window = session_window_for_date(day)
        if self.setup.window_fits(session_open_ms=window.open_ms_utc, session_close_ms=window.close_ms_utc):
            outcomes, pins = self._replay_with_data(day, window)
        else:
            # Nothing is read for a session the window rule skips, so that a data check that needs nothing there agrees.
            skipped = replay_session(self.setup, window_only_market(day), cash_usd=self.own.cash, fee_for=self.fee_for)
            outcomes, pins = dict.fromkeys(self.ladder, skipped), SessionPins(None, None, ())
        cash_before = self.own.cash
        for value, carried in self.ladder.items():
            carried.take(outcomes[value])
        return SessionResult(
            window.open_ms_utc,
            window.close_ms_utc,
            cash_before,
            outcomes[self.setup.slippage_percent],
            self.own.cash,
            pins,
        )

    def _replay_with_data(self, day: date, window: SessionWindow) -> tuple[dict[Decimal, SessionOutcome], SessionPins]:
        underlying, listing = self.data.underlying_day(day), self.data.listing(day)
        if underlying is None or listing is None:
            raise SessionDataMissing(day, underlying=underlying is None, listing=listing is None, contracts=())
        # A one-shot iterable would be read once and then look empty, so what is handed in is held before it is used.
        bars = tuple(underlying.value)
        listed = replace(listing.value, contracts=tuple(listing.value.contracts))
        if listed.underlying != self.setup.underlying:
            raise ValueError(
                f"the listing for {day} is of {listed.underlying}, and the setup trades {self.setup.underlying}"
            )
        bars_by_close(bars, trading_date=day, what="the underlying")  # refuse a malformed day before using it
        needs = session_needs(
            self.setup,
            trading_date=day,
            session_open_ms=window.open_ms_utc,
            session_close_ms=window.close_ms_utc,
            underlying=bars,
            listing=listed,
        )
        needed = sorted(needs.contracts, key=lambda contract: contract.occ_symbol)
        captured = {contract: self.data.option_day(contract, day) for contract in needed}
        absent = tuple(contract for contract in needed if captured[contract] is None)
        if absent:
            raise SessionDataMissing(day, underlying=False, listing=False, contracts=absent)
        days = {contract: found for contract, found in captured.items() if found is not None}
        market = DayMarket(
            day,
            underlying=bars,
            listing=listed,
            option_days={contract: found.value for contract, found in days.items()},
        )
        replayed = replay_session(self.setup, market, cash_usd=self.own.cash, fee_for=self.fee_for)
        # A skipped session is skipped at every slippage: the window and the underlying do not depend on it.
        outcomes = {
            value: replayed
            if carried is self.own or replayed.status is SessionStatus.SKIPPED
            else replay_session(carried.setup, market, cash_usd=carried.cash, fee_for=self.fee_for)
            for value, carried in self.ladder.items()
        }
        pins = SessionPins(
            underlying.sha256,
            listing.sha256,
            tuple(PinnedDay(contract, found.sha256) for contract, found in days.items()),
        )
        return outcomes, pins


def run_sessions(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    data: RunData,
    *,
    slippage_ladder: Sequence[Decimal] = DEFAULT_SLIPPAGE_LADDER,
    on_session: Callable[[int, int, date], None] = _ignore_progress,
    should_stop: Callable[[], bool] = _never_stop,
) -> RunResult:
    """Replay ``setup`` over ``sessions`` (in date order, each a NYSE session) reading what it needs from ``data``.

    ``on_session(done, total, day)`` is called after each session is replayed. ``should_stop()`` is asked before each
    session; when it says yes, ``RunStopped`` is raised and nothing is returned. Raises ``SessionDataMissing`` for a
    session whose data is not captured and ``FeeRateNotPublished`` before anything is read when the setup's fees are
    estimated and some session is on a date the fee model has no rate for. ``SoldLegRulesNotReplayed`` before anything
    is read when the setup has a sold leg and any leg carries a stop loss or a re-entry rule.
    """
    _check_the_run_can_start(setup, sessions)
    run = _Run(setup, data, slippage_ladder)
    kept: list[SessionResult] = []
    for done, day in enumerate(sessions):
        if should_stop():
            raise RunStopped(done)
        kept.append(run.replay(day))
        on_session(done + 1, len(sessions), day)
    points = tuple(
        SlippagePoint(
            value,
            value == setup.slippage_percent,
            carried.net_profit,
            carried.trades,
            carried.traded_sessions,
            carried.winning_sessions,
        )
        for value, carried in run.ladder.items()
    )
    summary = summarize_run(kept, starting_cash_usd=setup.starting_cash_usd)
    return RunResult(setup, tuple(kept), summary, points, slippage_tolerance(points))

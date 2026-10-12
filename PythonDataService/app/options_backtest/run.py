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

Cash and a leg marked, not sold. The leg has paid out and not been paid back; the next session starts with its mark in
cash, and the session's profit counts it at its mark. ``SessionResult`` keeps each figure.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from itertools import pairwise

from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.data import RunData
from app.options_backtest.fees import FeeRateNotPublished, fee_for_policy, unpublished_fee_types
from app.options_backtest.needs import session_needs
from app.options_backtest.replay import FeeFor, replay_session
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


def slippage_tolerance(points: Sequence[SlippagePoint]) -> SlippageTolerance:
    """Walk the ``points`` from the lowest slippage up and stop at the first that does not make a profit."""
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
class _Tally:
    """One slippage value's run so far: its own cash, and what it has made."""

    cash: Decimal
    net_profit: Decimal = Decimal(0)
    trades: int = 0
    traded_sessions: int = 0
    winning_sessions: int = 0

    def take(self, outcome: SessionOutcome) -> None:
        pnl = outcome.pnl_usd
        self.cash = _cash_after(self.cash, outcome)
        with localcontext(fills.EXACT):
            self.net_profit = self.net_profit + pnl
        self.trades += len(outcome.trades)
        if outcome.trades:
            self.traded_sessions += 1
            self.winning_sessions += pnl > 0


def _cash_after(cash_before: Decimal, outcome: SessionOutcome) -> Decimal:
    """What the next session starts with: the cash that moved, and the marks of legs not sold, paid back at their marks."""
    with localcontext(fills.EXACT):
        return cash_before + outcome.cash_change_usd + outcome.marked_value_usd


def _check_the_run_can_start(setup: OptionsBacktestSetup, sessions: Sequence[date]) -> None:
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


def _ladder(setup: OptionsBacktestSetup, requested: Sequence[Decimal]) -> dict[Decimal, OptionsBacktestSetup]:
    """The setup at each slippage value, lowest first, the setup's own value among them."""
    own = setup.slippage_percent
    return {
        value: setup
        if value == own
        else OptionsBacktestSetup.model_validate({**setup.model_dump(mode="python"), "slippage_percent": value})
        for value in sorted({*requested, own})
    }


def _replay_one_session(
    day: date,
    data: RunData,
    fee_for: FeeFor,
    variants: dict[Decimal, OptionsBacktestSetup],
    own: Decimal,
    own_cash: Decimal,
    cash_of: dict[Decimal, Decimal],
) -> tuple[SessionResult, dict[Decimal, SessionOutcome]]:
    """Read one session's data, replay it at every ladder value, and let the data go.

    Returns the session as it is kept, and the outcome at each other ladder value. Everything read is local to this
    call, so nothing of it outlives the session.
    """
    setup = variants[own]
    window = session_window_for_date(day)
    if setup.window_fits(session_open_ms=window.open_ms_utc, session_close_ms=window.close_ms_utc):
        outcome, others, pins = _replay_with_data(day, data, fee_for, variants, own, own_cash, cash_of)
    else:
        # Nothing is read for a session the window rule skips, so that a data check that needs nothing there agrees.
        outcome = replay_session(setup, window_only_market(day), cash_usd=own_cash, fee_for=fee_for)
        others, pins = {}, SessionPins(None, None, ())
    cash_after = _cash_after(own_cash, outcome)
    return SessionResult(day, window.open_ms_utc, window.close_ms_utc, own_cash, outcome, cash_after, pins), others


def _replay_with_data(
    day: date,
    data: RunData,
    fee_for: FeeFor,
    variants: dict[Decimal, OptionsBacktestSetup],
    own: Decimal,
    own_cash: Decimal,
    cash_of: dict[Decimal, Decimal],
) -> tuple[SessionOutcome, dict[Decimal, SessionOutcome], SessionPins]:
    setup = variants[own]
    window = session_window_for_date(day)
    underlying, listing = data.underlying_day(day), data.listing(day)
    if underlying is None or listing is None:
        raise SessionDataMissing(day, underlying=underlying is None, listing=listing is None, contracts=())
    if listing.value.underlying != setup.underlying:
        raise ValueError(
            f"the listing for {day} is of {listing.value.underlying}, and the setup trades {setup.underlying}"
        )
    bars_by_close(underlying.value, trading_date=day, what="the underlying")  # refuse a malformed day before using it
    needs = session_needs(
        setup,
        trading_date=day,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        underlying=underlying.value,
        listing=listing.value,
    )
    needed = sorted(needs.contracts, key=lambda contract: contract.occ_symbol)
    captured = {contract: data.option_day(contract, day) for contract in needed}
    absent = tuple(contract for contract in needed if captured[contract] is None)
    if absent:
        raise SessionDataMissing(day, underlying=False, listing=False, contracts=absent)
    days = {contract: found for contract, found in captured.items() if found is not None}
    market = DayMarket(
        day,
        underlying=underlying.value,
        listing=listing.value,
        option_days={contract: found.value for contract, found in days.items()},
    )
    outcome = replay_session(setup, market, cash_usd=own_cash, fee_for=fee_for)
    # A skipped session is skipped at every slippage: the window and the underlying do not depend on it.
    others = {
        value: outcome
        if outcome.status is SessionStatus.SKIPPED
        else replay_session(variant, market, cash_usd=cash_of[value], fee_for=fee_for)
        for value, variant in variants.items()
        if value != own
    }
    pins = SessionPins(
        underlying.sha256, listing.sha256, tuple(PinnedDay(contract, found.sha256) for contract, found in days.items())
    )
    return outcome, others, pins


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
    estimated and some session is on a date the fee model has no rate for.
    """
    _check_the_run_can_start(setup, sessions)
    variants = _ladder(setup, slippage_ladder)
    own = setup.slippage_percent
    fee_for = fee_for_policy(setup.fees)
    tallies = {value: _Tally(setup.starting_cash_usd) for value in variants}
    kept: list[SessionResult] = []
    for done, day in enumerate(sessions):
        if should_stop():
            raise RunStopped(done)
        cash_of = {value: tally.cash for value, tally in tallies.items()}
        result, others = _replay_one_session(day, data, fee_for, variants, own, cash_of[own], cash_of)
        kept.append(result)
        tallies[own].take(result.outcome)
        for value, outcome in others.items():
            tallies[value].take(outcome)
        on_session(done + 1, len(sessions), day)
    points = tuple(
        SlippagePoint(
            value,
            value == own,
            tally.net_profit,
            tally.trades,
            tally.traded_sessions,
            tally.winning_sessions,
        )
        for value, tally in tallies.items()
    )
    summary = summarize_run(kept, starting_cash_usd=setup.starting_cash_usd)
    return RunResult(setup, tuple(kept), summary, points, slippage_tolerance(points))

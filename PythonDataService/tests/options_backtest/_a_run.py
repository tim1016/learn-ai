"""A real run that uses every part of a result: the stored shapes are tested on what the replay really makes.

``a_busy_run`` replays four legs over five sessions on prices from a seeded random walk. The seed was searched for
(the first of the seeds from 0 that held every kind of thing below was 9) and ``what_the_busy_run_reaches`` is what the
tests assert it still does, so a change to the replay that made the run plain would fail there and not leave a round
trip that proves less than it says.
"""

from __future__ import annotations

import random
from datetime import date

from app.options_backtest.results import EventKind, ExitReason, Fill, Mark, SessionStatus, SkipReason
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from tests.options_backtest._run_data import FRI, HALF_DAY, THU, WED, Day, FakeRunData, fake_data
from tests.options_backtest.setup_fixtures import leg, setup_body

SEED = 9
STRIKES = (495, 500, 505, 510)
# The session the underlying did not trade at the entry time in.
NO_ENTRY_BAR = date(2026, 11, 30)


def _walk(rng: random.Random, start: float, step: float, floor: float, minutes: int = 390) -> list[float]:
    prices = []
    price = start
    for _ in range(minutes):
        price = max(floor, round(price + rng.uniform(-step, step), 2))
        prices.append(price)
    return prices


def _day(rng: random.Random) -> Day:
    underlying = {minute + 1: f"{price:.2f}" for minute, price in enumerate(_walk(rng, 500.0, 0.9, 490.0))}
    options = {}
    for right in ("call", "put"):
        for strike in STRIKES:
            base = max(0.4, 4.0 - abs(strike - 500) / 4.0)
            series = _walk(rng, base, 0.22, 0.05)
            # Some contracts stop trading before the close, so a leg held in one is marked, not sold.
            traded_until = rng.choice((390, 390, 375, 330)) if right == "call" else 390
            options[(right, strike)] = {
                minute + 1: (f"{price:.2f}", f"{series[min(minute + 1, 389)]:.2f}")
                for minute, price in enumerate(series)
                if minute + 1 <= traded_until
            }
    return Day(underlying=underlying, options=options, strikes=STRIKES)


def the_busy_setup() -> OptionsBacktestSetup:
    body = setup_body(
        entry={"anchor": "open", "offset_ms": 205 * MS_PER_MINUTE},
        exit={"anchor": "close", "offset_ms": 10 * MS_PER_MINUTE},
        no_reentry_after={"anchor": "close", "offset_ms": 150 * MS_PER_MINUTE},
        square_off="complete",
        legs=[
            leg(
                leg_id="call",
                expiry={"kind": "same_day"},
                stop_loss={"kind": "percent", "value": "20"},
                momentum={"kind": "percent_up", "value": "10"},
                reentry_on_stop={"kind": "on_momentum", "count": 1},
            ),
            # Three strikes above the money is off the four-strike listing: the pick is refused every time.
            leg(leg_id="far", expiry={"kind": "same_day"}, strike={"kind": "atm_offset", "steps": 3}),
            leg(
                leg_id="put",
                right="put",
                contracts=2,
                expiry={"kind": "same_day"},
                stop_loss={"kind": "points", "value": "0.40"},
                reentry_on_stop={"kind": "at_once", "count": 2},
            ),
        ],
        slippage_percent="0.3",
        fees={"kind": "per_contract", "per_contract_usd": "0.65"},
        starting_cash_usd="20000",
    )
    return OptionsBacktestSetup.model_validate(body)


def the_busy_data(seed: int = SEED) -> tuple[list[date], FakeRunData]:
    rng = random.Random(seed)
    days = {day: _day(rng) for day in (WED, THU, FRI, HALF_DAY)}
    # A full day on which the underlying has no bar at the entry minute (205 minutes after the open is minute 205).
    without_the_entry = _day(rng)
    without_the_entry.underlying.pop(205)
    days[NO_ENTRY_BAR] = without_the_entry
    sessions = sorted(days)
    return sessions, fake_data(days)


def a_busy_run(seed: int = SEED) -> RunResult:
    sessions, data = the_busy_data(seed)
    return run_sessions(the_busy_setup(), sessions, data)


def what_the_busy_run_reaches(result: RunResult) -> dict[str, bool]:
    trades = [trade for session in result.sessions for trade in session.outcome.trades]
    events = {event.kind for session in result.sessions for event in session.outcome.events}
    return {
        "a stop": any(trade.exit_reason is ExitReason.STOP for trade in trades),
        "a square-off": any(trade.exit_reason is ExitReason.SQUARE_OFF for trade in trades),
        "an exit at the exit time": any(trade.exit_reason is ExitReason.EXIT_TIME for trade in trades),
        "a leg marked, not sold": any(isinstance(trade.exit, Mark) for trade in trades),
        "a sale that filled": any(isinstance(trade.exit, Fill) for trade in trades),
        "a re-entry": any(trade.entry_number > 0 for trade in trades),
        "a leg that waited for momentum": any(trade.starting_price is not None for trade in trades),
        "a leg with a stop level": any(trade.stop_level is not None for trade in trades),
        "a leg bought straight away": any(trade.starting_price is None for trade in trades),
        "a window that did not fit": any(
            s.outcome.skip_reason is SkipReason.WINDOW_DOES_NOT_FIT for s in result.sessions
        ),
        "an underlying that did not trade": any(
            s.outcome.skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE for s in result.sessions
        ),
        "an incomplete session": any(s.status is SessionStatus.INCOMPLETE for s in result.sessions),
        "a traded session": any(s.status is SessionStatus.TRADED for s in result.sessions),
        "a re-entry blocked by the cutoff": EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF in events,
        "a leg cancelled": EventKind.LEG_CANCELLED in events,
        "a price on an event": any(event.price is not None for s in result.sessions for event in s.outcome.events),
        "a pick refused": EventKind.PICK_REFUSED in events,
    }

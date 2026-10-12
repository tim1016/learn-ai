"""A saved run with thousands of leg trades, built by hand and not replayed (#3030).

Replaying thousands of trades would test the replay. This is for what comes after it: that a run whose trades are many is
exported whole and listed page by page. The trades are drawn from a seeded generator, so the same run comes back every
time, and every figure is a real ``Decimal`` with two decimals. Each session holds ``per_session`` trades of the setup's
legs, each entered a minute or more after the one before it and sold (one in ten is marked, not sold, instead).
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options.contract import OptionContract, OptionRight
from app.options_backtest.results import ExitReason, Fill, LegTrade, Mark, SessionOutcome, SessionStatus
from app.options_backtest.run import RunResult, SlippagePoint, slippage_tolerance
from app.options_backtest.session_result import SessionResult
from app.options_backtest.setup import MS_PER_MINUTE
from app.options_backtest.statistics import summarize_run
from tests.options_backtest._detail_scene import the_setup
from tests.options_backtest._results import a_session_of

D = Decimal
LEGS = ("call", "put", "lag")
RIGHTS: dict[str, OptionRight] = {"call": "call", "put": "put", "lag": "put"}
REASONS = (ExitReason.STOP, ExitReason.SQUARE_OFF, ExitReason.EXIT_TIME)


def _cents(rng: random.Random, low: int, high: int) -> Decimal:
    return D(rng.randint(low, high)) / 100


def _trade(rng: random.Random, day: date, open_ms: int, minute: int) -> LegTrade:
    leg_id = rng.choice(LEGS)
    contract = OptionContract.of(root="SPY", expiry_date=day, right=RIGHTS[leg_id], strike=rng.choice((495, 500, 505)))
    contracts = rng.randint(1, 5)
    bought_seen = _cents(rng, 50, 900)
    bought = bought_seen + D("0.01")
    entry_close = open_ms + minute * MS_PER_MINUTE
    entry = Fill(leg_id, contract, "buy", contracts, 100, entry_close, bought_seen, bought)
    marked = rng.randrange(10) == 0
    reason = ExitReason.MARKED_NOT_SOLD if marked else rng.choice(REASONS)
    exit_decided = entry_close + rng.randint(1, 60) * MS_PER_MINUTE
    leaving: Fill | Mark
    if marked:
        sold = _cents(rng, 10, 900)
        leaving = Mark(exit_decided, sold)
        exit_fee = D("0")
    else:
        sold_seen = _cents(rng, 10, 900)
        sold = sold_seen - D("0.01") if sold_seen > D("0.01") else D("0")
        leaving = Fill(leg_id, contract, "sell", contracts, 100, exit_decided + MS_PER_MINUTE, sold_seen, sold)
        exit_fee = D("0.65") * contracts
    entry_fee = D("0.65") * contracts
    starting = _cents(rng, 50, 900) if rng.randrange(3) == 0 else None
    return LegTrade(
        leg_id=leg_id,
        entry_number=rng.randint(0, 2),
        contract=contract,
        contracts=contracts,
        multiplier=100,
        starting_price=starting,
        momentum_level=None if starting is None else starting * D("1.25"),
        stop_level=_cents(rng, 20, 400) if rng.randrange(2) == 0 else None,
        entry_decided_at_ms=entry_close - MS_PER_MINUTE,
        entry=entry,
        exit_reason=reason,
        exit_group=rng.randint(1, 9),
        exit_decided_at_ms=exit_decided,
        exit=leaving,
        entry_fee_usd=entry_fee,
        exit_fee_usd=exit_fee,
        pnl_usd=(sold - bought) * 100 * contracts - entry_fee - exit_fee,
    )


def a_big_run(sessions: int = 40, per_session: int = 100, seed: int = 7) -> RunResult:
    """``sessions`` consecutive sessions from August 2026, each with ``per_session`` trades in the order of their entries."""
    rng = random.Random(seed)
    setup = the_setup()
    days = expected_sessions(date(2026, 8, 3), date(2026, 12, 31))[:sessions]
    assert len(days) == sessions
    kept: list[SessionResult] = []
    cash = setup.starting_cash_usd
    for day in days:
        open_ms = session_window_for_date(day).open_ms_utc
        # Entries every three minutes, so they stay in the order the replay would record them.
        trades = tuple(_trade(rng, day, open_ms, 5 + at * 3) for at in range(per_session))
        status = (
            SessionStatus.INCOMPLETE
            if any(t.exit_reason is ExitReason.MARKED_NOT_SOLD for t in trades)
            else SessionStatus.TRADED
        )
        pnl = sum((t.pnl_usd for t in trades), start=D("0"))
        marked_value = sum(
            (t.exit_price * t.multiplier * t.contracts for t in trades if t.exit_reason is ExitReason.MARKED_NOT_SOLD),
            start=D("0"),
        )
        outcome = SessionOutcome(status, None, trades, (), pnl - marked_value)
        session = a_session_of(day, outcome, str(cash))
        kept.append(session)
        cash = session.cash_after_usd
    summary = summarize_run(kept, starting_cash_usd=setup.starting_cash_usd)
    points = (
        SlippagePoint(
            setup.slippage_percent,
            True,
            summary.net_profit_usd,
            summary.trades,
            summary.sessions_traded,
            summary.sessions_profitable,
        ),
    )
    return RunResult(setup, tuple(kept), summary, points, slippage_tolerance(points))

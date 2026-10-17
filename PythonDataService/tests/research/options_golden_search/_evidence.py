"""Hand-written exact Options histories used by evidence and evaluator tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.options_backtest.replay import replay_session
from app.options_backtest.run import load_session_market
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.options_golden_search.metrics import SessionEvidence
from tests.options_backtest._market import leg, no_fee, setup
from tests.options_backtest._run_data import (
    FakeRunData,
    contract_on,
    flat,
    listing_on,
    option_bars,
    steady,
    underlying_bars,
)


def scene(
    days: tuple[date, ...], *, exits: tuple[str, ...], trough: str = "1", missing_minute: int | None = None,
) -> tuple[OptionsBacktestSetup, FakeRunData]:
    options = {}
    for day, exit_price in zip(days, exits, strict=True):
        bars = {**flat("1", 5, 60), 7: ("1", trough), 61: (exit_price, exit_price)}
        if missing_minute is not None:
            bars.pop(missing_minute, None)
        options[(contract_on(day, "call", "500"), day)] = option_bars(day, bars)
    return setup(leg()), FakeRunData(
        underlying={day: underlying_bars(day, steady("500")) for day in days},
        listings={day: listing_on(day) for day in days}, options=options,
    )


def session_evidence(
    day: date, *, cash: str = "10000", exit_price: str = "2", trough: str = "1", no_trade: bool = False,
) -> SessionEvidence:
    written, data = scene((day,), exits=(exit_price,), trough=trough)
    if no_trade:
        written = setup(leg(momentum=("percent_up", "1000")))
    market, _pins = load_session_market(written, day, data)
    observed = []
    outcome = replay_session(written, market, cash_usd=Decimal(cash), fee_for=no_fee, observer=observed.append)
    return SessionEvidence(market.session_open_ms, market.session_close_ms, Decimal(cash), outcome, tuple(observed))

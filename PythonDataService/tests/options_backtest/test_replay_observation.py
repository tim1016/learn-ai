"""Hand-worked post-close evidence; observation never changes a canonical decision (#3333)."""

from __future__ import annotations

from app.options_backtest.observation import MinuteObservation
from app.options_backtest.replay import replay_session
from tests.options_backtest._market import (
    D,
    TableMarket,
    at,
    contract,
    fee_per_contract,
    flat,
    leg,
    no_fee,
    picked,
    setup,
    steady,
)


def test_multileg_intraday_loss_is_visible_despite_profitable_close_and_fees_are_once() -> None:
    call, put = contract("call", "500"), contract("put", "500")
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", call), ("put", "500", put)),
        options={
            call: {**flat("3", 5, 60), 7: ("3", "1"), 61: ("5", "5")},
            put: {**flat("2", 5, 60), 7: ("2", "0.5"), 61: ("3", "3")},
        },
    )
    written = setup(leg("call"), leg("put", "put"))
    observations: list[MinuteObservation] = []
    ordinary = replay_session(written, market, cash_usd=D("10000"), fee_for=fee_per_contract("1"))
    observed = replay_session(written, market, cash_usd=D("10000"), fee_for=fee_per_contract("1"), observer=observations.append)
    assert observed == ordinary
    assert observed.money_steps == ordinary.money_steps
    assert len(observations) == 391
    assert observations[0].at_ms == at(0)
    assert observations[0].equity_usd == D("10000")
    assert observations[6].cash_usd == D("9498")
    assert observations[6].equity_usd == D("9998")
    assert observations[7].equity_usd == D("9648")
    assert observations[60].held[0].close_pending
    assert not observations[61].held
    assert observations[-1].equity_usd == D("10296")
    assert observations[-1].equity_usd - observations[0].equity_usd == observed.pnl_usd


def test_signed_sold_marks_use_multiplier_and_collateral_is_not_a_second_liability() -> None:
    bought, sold = contract("call", "505"), contract("call", "500")
    market = TableMarket(
        underlying=steady("500"), picks=picked(("long", "500", bought), ("short", "500", sold)),
        options={
            bought: {**flat("1", 5, 60), 7: ("1", "0.5"), 61: ("1", "1")},
            sold: {**flat("3", 5, 60), 7: ("3", "4"), 61: ("2", "2")},
        },
    )
    observations: list[MinuteObservation] = []
    outcome = replay_session(setup(leg("long"), leg("short", side="sell")), market, cash_usd=D("10000"), fee_for=no_fee, observer=observations.append)
    assert observations[6].cash_usd == D("10200")
    assert observations[6].collateral_usd == D("500")
    assert observations[6].equity_usd == D("10000")
    assert [(item.signed_contracts, item.multiplier) for item in observations[7].held] == [(1, 100), (-1, 100)]
    assert observations[7].equity_usd == D("9850")
    assert outcome.pnl_usd == D("100")
    assert observations[-1].equity_usd == D("10100")


def test_missing_held_close_remains_missing_even_when_next_minute_fills_profitable_close() -> None:
    call = contract("call", "500")
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", call)),
        options={call: {**flat("1", 5, 60), 62: ("2", "2")}},
    )
    observations: list[MinuteObservation] = []
    written = setup(leg())
    ordinary = replay_session(written, market, cash_usd=D("10000"), fee_for=no_fee)
    outcome = replay_session(written, market, cash_usd=D("10000"), fee_for=no_fee, observer=observations.append)
    assert outcome == ordinary
    assert observations[61].missing_leg_ids == ("call",)
    assert observations[61].equity_usd is None
    assert observations[61].held[0].close_pending
    assert not observations[62].held
    assert observations[62].equity_usd == D("10100")
    assert not observations[63].missing_leg_ids


def test_reentry_observes_actual_new_held_position_and_flat_cash_between_attempts() -> None:
    call = contract("call", "500")
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", call)),
        options={call: {**flat("2", 5, 9), 10: ("2", "1.4"), **flat("1.4", 11, 61)}},
    )
    observations: list[MinuteObservation] = []
    outcome = replay_session(setup(leg(stop=("percent", "25"), reentry=("at_once", 1))), market, cash_usd=D("10000"), fee_for=no_fee, observer=observations.append)
    assert len(outcome.trades) == 2
    assert observations[10].held[0].close_pending
    assert observations[11].held == ()
    assert observations[11].cash_usd == D("9940")
    assert observations[12].held[0].signed_contracts == 1
    assert observations[12].equity_usd == D("9940")
    assert observations[-1].equity_usd == D("9940")


def test_no_trade_day_records_flat_capital_at_every_minute_without_mark_reads() -> None:
    call = contract("call", "500")
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", call)), options={call: flat("1", 5, 60)},
    )
    observations: list[MinuteObservation] = []
    outcome = replay_session(setup(leg(momentum=("percent_up", "50"))), market, cash_usd=D("10000"), fee_for=no_fee, observer=observations.append)
    assert not outcome.trades
    assert len(observations) == 391
    assert all(point.equity_usd == D("10000") and not point.held for point in observations)

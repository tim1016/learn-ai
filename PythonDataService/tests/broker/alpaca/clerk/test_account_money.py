"""Worked examples for the money bar: every dollar in exactly one place (PRD #2560).

Each example is hand-derived from the PRD's money semantics (PRD #2540) and
checked three ways: the exact Decimal parts equal cash plus shares at cost,
the drawn cents sum to ``total_cents``, and the widths sum to 10000 basis
points. Free to deploy is always the Deploy preview's own unreserved cents.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.account_money import (
    FULL_BAR_BPS,
    AccountMoney,
    Holding,
    MoneyBar,
    MoneyBarUnavailable,
    MoneyConservationError,
    account_money,
    money_bar,
    share_bps,
)
from app.broker.alpaca.clerk.budgets import AccountBudget, DeploymentBudget, account_budget, deployment_budget


def _bot(sid: str, *, cents: int, position: str = "0", pending: str = "0", fees: str = "0",
         realized: str = "0", active: bool = True) -> DeploymentBudget:
    return deployment_budget(
        strategy_instance_id=sid, committed_cents=cents, active=active, realized_gross=realized,
        fees=D(fees), position_cost=D(position), pending_orders=D(pending),
    )


def _drawn(money: AccountMoney, **kwargs: int) -> MoneyBar:
    bar = money_bar(money, **kwargs)
    assert sum(segment.cents for segment in bar.segments) == bar.total_cents
    assert sum(segment.bps for segment in bar.segments) == FULL_BAR_BPS
    assert all(segment.bps >= 1 for segment in bar.segments if segment.cents > 0)
    for segment in bar.segments:
        if segment.parts is not None:
            parts = segment.parts
            assert parts.in_shares_cents + parts.pending_cents + parts.free_cents == segment.cents
            assert parts.in_shares_bps + parts.pending_bps + parts.free_bps == FULL_BAR_BPS
    return bar


def _by_kind(bar: MoneyBar) -> list[tuple[str, str | None, int]]:
    return [(segment.kind, segment.strategy_instance_id, segment.cents) for segment in bar.segments]


def test_one_running_bot_is_its_whole_balance_beside_free_to_deploy() -> None:
    # $100,000 account; a $1,000 bot bought 1 SPY at $764.71 with a $0.01 fee
    # Alpaca has not taken yet. Cash already shows the purchase.
    bot = _bot("spy-ema", cents=100_000, position="764.71", fees="0.01")
    budget = account_budget(cash="99235.29", deployments=[bot], order_claims=D(0), fee_claims=D("0.01"))
    money = account_money(budget, unseen_fills=D(0), holdings=())

    assert money.total == D("100000.00")
    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "spy-ema", 99_999), ("charges", None, 1), ("free", None, 9_900_000)]
    parts = bar.segments[0].parts
    assert parts is not None and (parts.in_shares_cents, parts.pending_cents, parts.free_cents) == (76_471, 0, 23_528)
    assert bar.segments[0].shortfall_cents == 0
    assert bar.cents_of("free") == budget.unreserved_cents == 9_900_000
    assert bar.total_cents == 10_000_000 and bar.cash_cents == 9_923_529


def test_several_bots_with_pending_entries_keep_their_orders_inside_their_slices() -> None:
    a = _bot("a", cents=100_000, pending="500.01")
    b = _bot("b", cents=200_000, position="600", pending="300.01")
    budget = account_budget(cash=50_000, deployments=[b, a], order_claims=D("800.02"), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=())

    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "a", 100_000), ("bot", "b", 200_000), ("free", None, 4_760_000)]
    assert bar.total_cents == 5_060_000
    b_parts = bar.segments[1].parts
    assert b_parts is not None and (b_parts.in_shares_cents, b_parts.pending_cents, b_parts.free_cents) == (60_000, 30_001, 109_999)


def test_stopped_bot_still_holding_keeps_its_shares_and_releases_its_free_budget() -> None:
    stopped = _bot("old", cents=100_000, position="764.71", fees="0.01", active=False)
    budget = account_budget(cash="99235.29", deployments=[stopped], order_claims=D(0), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=())

    bar = _drawn(money)
    assert _by_kind(bar) == [("stopped", "old", 76_471), ("free", None, 9_923_529)]
    assert bar.segments[0].released_cents == 23_528
    assert bar.segments[0].still_claimed_cents == 0
    assert bar.total_cents == 10_000_000


def test_pre_budget_stopped_bot_and_its_working_order_stay_held() -> None:
    # A bot deployed before budgets holds $500 of shares and a working $100.01
    # entry; neither is anyone's budget, so both stay claimed, not free.
    budget = account_budget(cash=9_500, deployments=[], order_claims=D("100.01"), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=[Holding("bot:legacy", "legacy", D(500), D("100.01"))])

    bar = _drawn(money)
    assert _by_kind(bar) == [("stopped", "legacy", 60_001), ("free", None, 939_999)]
    assert bar.segments[0].still_claimed_cents == 10_001 and bar.segments[0].released_cents == 0
    assert bar.total_cents == 1_000_000


def test_a_flat_stopped_bot_with_nothing_claimed_is_finished_and_not_drawn() -> None:
    finished = _bot("done", cents=100_000, realized="12.50", active=False)
    budget = account_budget(cash="1012.50", deployments=[finished], order_claims=D(0), fee_claims=D(0))
    bar = _drawn(account_money(budget, unseen_fills=D(0), holdings=()))
    assert _by_kind(bar) == [("free", None, 101_250)]


def test_manual_shares_are_money_held_outside_any_bot() -> None:
    budget = account_budget(cash=8_500, deployments=[], order_claims=D(0), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=[Holding("manual-operator:owner", None, D(1_500), D(0))])

    bar = _drawn(money)
    assert _by_kind(bar) == [("outside", None, 150_000), ("free", None, 850_000)]
    assert money.total == D(10_000)


def test_account_charges_cash_does_not_show_yet_are_their_own_slice() -> None:
    budget = account_budget(cash=1_000, deployments=[], order_claims=D(0), fee_claims=D("0.07"))
    bar = _drawn(account_money(budget, unseen_fills=D(0), holdings=()))
    assert _by_kind(bar) == [("charges", None, 7), ("free", None, 99_993)]
    assert bar.total_cents == 100_000


def test_overrun_shows_its_shortfall_and_never_a_negative_slice() -> None:
    # A $1,000 bot filled at $1,005 plus a $0.01 fee: $5.01 beyond its balance.
    bot = _bot("slipped", cents=100_000, position="1005", fees="0.01")
    budget = account_budget(cash=8_995, deployments=[bot], order_claims=D(0), fee_claims=D("0.01"))
    bar = _drawn(account_money(budget, unseen_fills=D(0), holdings=()))

    assert _by_kind(bar) == [("bot", "slipped", 100_500), ("charges", None, 1), ("free", None, 899_499)]
    assert bar.segments[0].shortfall_cents == 501
    assert bar.segments[0].parts is not None and bar.segments[0].parts.free_cents == 0
    assert bar.total_cents == 1_000_000


def test_a_fill_the_cash_has_not_seen_is_counted_once_as_shares() -> None:
    # The bot's $500 buy is recorded; Alpaca's cash still shows $10,000.
    bot = _bot("fresh", cents=100_000, position="500")
    budget = account_budget(cash=10_000, deployments=[bot], order_claims=D(500), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(500), holdings=())

    assert money.cash == D(9_500) and money.total == D(10_000)
    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "fresh", 100_000), ("free", None, 900_000)]


def test_subcent_costs_round_per_part_and_the_drawn_cents_still_add_up() -> None:
    # 0.125 shares at $10.01 cost $1.25125.
    bot = _bot("frac", cents=100_000, position="1.25125")
    budget = account_budget(cash="998.74875", deployments=[bot], order_claims=D(0), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=())
    assert money.total == D(1_000)
    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "frac", 99_999), ("free", None, 0)]
    parts = bar.segments[0].parts
    assert parts is not None and (parts.in_shares_cents, parts.free_cents) == (125, 99_874)


def test_money_after_carves_the_new_slice_out_of_free_and_keeps_the_total() -> None:
    bot = _bot("a", cents=100_000)
    budget = account_budget(cash=10_000, deployments=[bot], order_claims=D(0), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=())

    before = _drawn(money)
    after = _drawn(money, new_cents=80_000)
    assert _by_kind(after) == [("bot", "a", 100_000), ("new", None, 80_000), ("free", None, 820_000)]
    assert after.total_cents == before.total_cents == 1_000_000
    with pytest.raises(MoneyBarUnavailable, match="more than is free"):
        money_bar(money, new_cents=900_001)


def test_overdrawn_account_names_the_shortfall_instead_of_drawing_a_false_bar() -> None:
    bot = _bot("a", cents=100_000)
    budget = account_budget(cash=900, deployments=[bot], order_claims=D(0), fee_claims=D(0))
    money = account_money(budget, unseen_fills=D(0), holdings=())
    with pytest.raises(MoneyBarUnavailable, match=r"\$100\.00 short"):
        money_bar(money)


def test_parts_that_do_not_add_up_refuse_instead_of_drawing() -> None:
    # An order claim with no pending entry or unseen fill behind it.
    budget = account_budget(cash=1_000, deployments=[], order_claims=D(1), fee_claims=D(0))
    with pytest.raises(MoneyConservationError):
        account_money(budget, unseen_fills=D(0), holdings=())


def test_empty_account_draws_all_free_and_still_sums_to_the_full_width() -> None:
    budget = AccountBudget(cash=D(0), active_free_claims=D(0), order_claims=D(0), fee_claims=D(0), available=D(0), deployments=())
    bar = _drawn(account_money(budget, unseen_fills=D(0), holdings=()))
    assert [(segment.kind, segment.bps) for segment in bar.segments] == [("free", FULL_BAR_BPS)]


def test_basis_points_follow_the_fee_rule_and_lift_a_tiny_slice_to_one() -> None:
    assert share_bps({"b": 1, "a": 1, "c": 1}, empty="c") == {"a": 3_334, "b": 3_333, "c": 3_333}
    widths = share_bps({"tiny": 1, "huge": 10_000_000}, empty="huge")
    assert widths == {"huge": 9_999, "tiny": 1}
    assert share_bps({"x": 0, "free": 0}, empty="free") == {"x": 0, "free": FULL_BAR_BPS}
    with pytest.raises(ValueError):
        share_bps({"x": -1}, empty="x")

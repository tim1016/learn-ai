"""Worked examples for the money bar: every dollar in exactly one place (PRD #2560).

Each example is hand-derived from the PRD's money semantics (PRD #2540) and
checked three ways: the exact Decimal parts equal cash plus shares at cost,
the drawn cents sum to ``total_cents``, and the widths sum to 10000 basis
points. Free to deploy is always the Deploy preview's own unreserved cents.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.account_money import (
    FULL_BAR_BPS,
    AccountMoney,
    Holding,
    MoneyBar,
    MoneyConservationError,
    account_money,
    bot_parts,
    bot_segment,
    money_bar,
    release_at_stop,
    share_bps,
)
from app.broker.alpaca.clerk.budgets import (
    AccountBudget,
    DeploymentBudget,
    ReleaseAtStop,
    account_budget,
    deployment_budget,
)


def _bot(sid: str, *, cents: int, position: str = "0", pending: str = "0", fees: str = "0",
         realized: str = "0", active: bool = True, release: ReleaseAtStop | None = None) -> DeploymentBudget:
    return deployment_budget(
        strategy_instance_id=sid, committed_cents=cents, active=active, realized_gross=realized,
        fees=D(fees), position_cost=D(position), pending_orders=D(pending), release=release,
    )


def _money(budget: AccountBudget, *, unseen_fills: D = D(0), unseen_sales: D = D(0),
           holdings: tuple[Holding, ...] | list[Holding] = (), order: tuple[str, ...] | None = None) -> AccountMoney:
    """``order`` is the account's registration order; by default every bot in view, by name."""
    bots = sorted({item.strategy_instance_id for item in budget.deployments}
                  | {item.strategy_instance_id for item in holdings if item.strategy_instance_id is not None})
    return account_money(budget, unseen_fills=unseen_fills, unseen_sales=unseen_sales, holdings=holdings,
                         registration_order=tuple(bots) if order is None else order)


def _drawn(money: AccountMoney, **kwargs: int) -> MoneyBar:
    bar = money_bar(money, **kwargs)
    assert sum(segment.cents for segment in bar.segments) == bar.total_cents + bar.shortfall_cents
    assert sum(segment.bps for segment in bar.segments) == FULL_BAR_BPS
    assert all(segment.bps >= 1 for segment in bar.segments if segment.cents > 0)
    for segment in bar.segments:
        if segment.parts is not None:
            parts = segment.parts
            assert parts.in_shares_cents + parts.pending_cents + parts.free_cents == segment.cents
            assert parts.in_shares_bps + parts.pending_bps + parts.free_bps == (FULL_BAR_BPS if segment.cents else 0)
    return bar


def _by_kind(bar: MoneyBar) -> list[tuple[str, str | None, int]]:
    return [(segment.kind, segment.strategy_instance_id, segment.cents) for segment in bar.segments]


def test_one_running_bot_is_its_whole_balance_beside_free_to_deploy() -> None:
    # $100,000 account; a $1,000 bot bought 1 SPY at $764.71 with a $0.01 fee
    # Alpaca has not taken yet. Cash already shows the purchase.
    bot = _bot("spy-ema", cents=100_000, position="764.71", fees="0.01")
    budget = account_budget(cash="99235.29", deployments=[bot], order_claims=D(0), fee_claims=D("0.01"))
    money = _money(budget)

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
    money = _money(budget)

    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "a", 100_000), ("bot", "b", 200_000), ("free", None, 4_760_000)]
    assert bar.total_cents == 5_060_000
    b_parts = bar.segments[1].parts
    assert b_parts is not None and (b_parts.in_shares_cents, b_parts.pending_cents, b_parts.free_cents) == (60_000, 30_001, 109_999)


def test_stopped_bot_still_holding_keeps_its_shares_and_releases_its_free_budget() -> None:
    stopped = _bot("old", cents=100_000, position="764.71", fees="0.01", active=False)
    budget = account_budget(cash="99235.29", deployments=[stopped], order_claims=D(0), fee_claims=D(0))
    money = _money(budget)

    bar = _drawn(money)
    assert _by_kind(bar) == [("stopped", "old", 76_471), ("free", None, 9_923_529)]
    # Its Stop recorded no release, so the figure is estimated and says so.
    assert (bar.segments[0].released_cents, bar.segments[0].released_estimated) == (23_528, True)
    assert bar.segments[0].still_claimed_cents == 0
    assert bar.total_cents == 10_000_000


def test_a_stopped_bots_released_money_is_what_its_stop_recorded_whatever_its_money_does_after() -> None:
    # #2555: $1,000 bot stopped holding $600 of shares; its Stop released and
    # recorded $400. It has since sold half its shares for a $12.50 gain, which
    # comes back as cash -- the release stays the Stop's $400.00, not today's
    # $712.50 of free money.
    stopped = _bot("old", cents=100_000, position="300", realized="12.50", active=False,
                   release=ReleaseAtStop(released_cents=40_000, held_cents=60_000))
    budget = account_budget(cash="712.50", deployments=[stopped], order_claims=D(0), fee_claims=D(0))

    bar = _drawn(_money(budget))

    segment = bar.segments[0]
    assert (segment.kind, segment.cents, segment.still_claimed_cents) == ("stopped", 30_000, 0)
    assert (segment.released_cents, segment.released_estimated) == (40_000, False)


def test_release_at_stop_is_the_positive_free_budget_and_what_stays_held() -> None:
    # The fractional-fee remainder rule below, from the running deployment the Stop ends.
    assert release_at_stop(_bot("old", cents=100_000, position="600.005", fees="0.0049")) == ReleaseAtStop(40_000, 60_000)
    assert release_at_stop(_bot("pending", cents=20_000, pending="100.01")) == ReleaseAtStop(9_999, 10_001)
    # An overrun has no free budget to release; everything it holds stays claimed.
    assert release_at_stop(_bot("over", cents=10_000, position="101")) == ReleaseAtStop(0, 10_100)


def test_a_stopped_bots_released_money_is_the_remainder_of_its_balance() -> None:
    # A fractional fee: rounded on its own, released money (399.9901 floors to
    # $399.99) and shares ($600.005 half-evens to $600.00) would be a cent
    # short of the $1,000.00 balance (999.9951) the bot page states.
    stopped = _bot("old", cents=100_000, position="600.005", fees="0.0049", active=False)
    budget = account_budget(cash="399.9951", deployments=[stopped], order_claims=D(0), fee_claims=D(0))

    bar = _drawn(_money(budget))

    segment = bar.segments[0]
    assert (segment.cents, segment.released_cents, segment.still_claimed_cents) == (60_000, 40_000, 0)
    assert segment.cents + segment.released_cents == 100_000


def test_bot_segment_is_the_account_bars_own_slice_filling_a_bar_of_its_own() -> None:
    running = _bot("run", cents=100_000, position="764.71", fees="0.01")
    stopped = _bot("old", cents=100_000, position="600.005", fees="0.0049", active=False)
    finished = _bot("done", cents=50_000, realized="1", active=False)
    budget = account_budget(cash="100000", deployments=[running, stopped, finished], order_claims=D(0), fee_claims=D(0))
    money = _money(budget)

    bar = _drawn(money)

    by_bot = {segment.strategy_instance_id: segment for segment in bar.segments}
    assert bot_segment(money, "run") == replace(by_bot["run"], bps=FULL_BAR_BPS)
    assert bot_segment(money, "old") == replace(by_bot["old"], bps=FULL_BAR_BPS)
    # A flat stopped bot with nothing claimed is finished: it has no slice.
    assert bot_segment(money, "done") is None
    assert bot_segment(money, "never-deployed") is None


def test_pre_budget_stopped_bot_and_its_working_order_stay_held() -> None:
    # A bot deployed before budgets holds $500 of shares and a working $100.01
    # entry; neither is anyone's budget, so both stay claimed, not free.
    budget = account_budget(cash=9_500, deployments=[], order_claims=D("100.01"), fee_claims=D(0))
    money = _money(budget, holdings=[Holding("bot:legacy", "legacy", D(500), D("100.01"))])

    bar = _drawn(money)
    assert _by_kind(bar) == [("stopped", "legacy", 60_001), ("free", None, 939_999)]
    assert bar.segments[0].still_claimed_cents == 10_001 and bar.segments[0].released_cents == 0
    # It reserved nothing, so it released nothing: a fact, not an estimate.
    assert not bar.segments[0].released_estimated
    assert bar.total_cents == 1_000_000


def test_a_flat_stopped_bot_with_nothing_claimed_is_finished_and_not_drawn() -> None:
    finished = _bot("done", cents=100_000, realized="12.50", active=False)
    budget = account_budget(cash="1012.50", deployments=[finished], order_claims=D(0), fee_claims=D(0))
    bar = _drawn(_money(budget))
    assert _by_kind(bar) == [("free", None, 101_250)]


def test_manual_shares_are_money_held_outside_any_bot() -> None:
    budget = account_budget(cash=8_500, deployments=[], order_claims=D(0), fee_claims=D(0))
    money = _money(budget, holdings=[Holding("manual-operator:owner", None, D(1_500), D(0))])

    bar = _drawn(money)
    assert _by_kind(bar) == [("outside", None, 150_000), ("free", None, 850_000)]
    assert money.total == D(10_000)


def test_account_charges_cash_does_not_show_yet_are_their_own_slice() -> None:
    budget = account_budget(cash=1_000, deployments=[], order_claims=D(0), fee_claims=D("0.07"))
    bar = _drawn(_money(budget))
    assert _by_kind(bar) == [("charges", None, 7), ("free", None, 99_993)]
    assert bar.total_cents == 100_000


def test_overrun_shows_its_shortfall_and_never_a_negative_slice() -> None:
    # A $1,000 bot filled at $1,005 plus a $0.01 fee: $5.01 beyond its balance.
    bot = _bot("slipped", cents=100_000, position="1005", fees="0.01")
    budget = account_budget(cash=8_995, deployments=[bot], order_claims=D(0), fee_claims=D("0.01"))
    bar = _drawn(_money(budget))

    assert _by_kind(bar) == [("bot", "slipped", 100_500), ("charges", None, 1), ("free", None, 899_499)]
    assert bar.segments[0].shortfall_cents == 501
    assert bar.segments[0].parts is not None and bar.segments[0].parts.free_cents == 0
    assert bar.total_cents == 1_000_000


def test_a_fill_the_cash_has_not_seen_is_counted_once_as_shares() -> None:
    # The bot's $500 buy is recorded; Alpaca's cash still shows $10,000.
    bot = _bot("fresh", cents=100_000, position="500")
    budget = account_budget(cash=10_000, deployments=[bot], order_claims=D(500), fee_claims=D(0))
    money = _money(budget, unseen_fills=D(500))

    assert money.cash == D(9_500) and money.total == D(10_000)
    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "fresh", 100_000), ("free", None, 900_000)]


def test_subcent_costs_round_per_part_and_the_drawn_cents_still_add_up() -> None:
    # 0.125 shares at $10.01 cost $1.25125.
    bot = _bot("frac", cents=100_000, position="1.25125")
    budget = account_budget(cash="998.74875", deployments=[bot], order_claims=D(0), fee_claims=D(0))
    money = _money(budget)
    assert money.total == D(1_000)
    bar = _drawn(money)
    assert _by_kind(bar) == [("bot", "frac", 99_999), ("free", None, 0)]
    parts = bar.segments[0].parts
    assert parts is not None and (parts.in_shares_cents, parts.free_cents) == (125, 99_874)


def test_money_after_carves_the_new_slice_out_of_free_and_keeps_the_total() -> None:
    bot = _bot("a", cents=100_000)
    budget = account_budget(cash=10_000, deployments=[bot], order_claims=D(0), fee_claims=D(0))
    money = _money(budget)

    before = _drawn(money)
    after = _drawn(money, new_cents=80_000)
    assert _by_kind(after) == [("bot", "a", 100_000), ("new", None, 80_000), ("free", None, 820_000)]
    assert after.total_cents == before.total_cents == 1_000_000
    with pytest.raises(ValueError, match="within free to deploy"):
        money_bar(money, new_cents=900_001)


def test_overdrawn_account_draws_every_claim_and_reports_the_shortfall_as_data() -> None:
    # A $1,000 bot on an account with $900: the claims exceed the money by
    # $100. The bar still draws (review A6); free is $0 and the overrun is a
    # figure, never a hidden bar and never a negative slice.
    bot = _bot("a", cents=100_000)
    budget = account_budget(cash=900, deployments=[bot], order_claims=D(0), fee_claims=D(0))
    bar = _drawn(_money(budget))
    assert _by_kind(bar) == [("bot", "a", 100_000), ("free", None, 0)]
    assert bar.shortfall_cents == 10_000 and bar.total_cents == 90_000
    assert [segment.bps for segment in bar.segments] == [FULL_BAR_BPS, 0]


def test_an_exit_cash_has_not_seen_is_settling_never_a_drop_or_a_shortfall() -> None:
    # Review A2. A $1,000 bot bought 1 SPY at $764.71 (cash saw it) and has
    # just sold it at $770 with a $0.02 fee; Alpaca's cash still shows the
    # position's cost gone and no proceeds. FIFO already dropped the lot.
    before = _money(account_budget(
        cash="99235.29", deployments=[_bot("spy", cents=100_000, position="764.71")], order_claims=D(0), fee_claims=D(0),
    ))
    sold = _bot("spy", cents=100_000, realized="5.29", fees="0.02")
    after_budget = account_budget(cash="99235.29", deployments=[sold], order_claims=D(0), fee_claims=D("0.02"))
    after = _money(after_budget, unseen_sales=D(770))

    # The total moves only by the realized gain, never by the proceeds in flight.
    assert before.total == D("100000.00") and after.total == D("100005.29")
    bar = _drawn(after)
    assert _by_kind(bar) == [("bot", "spy", 100_527), ("charges", None, 2), ("settling", None, 77_000), ("free", None, 9_823_000)]
    # Free to deploy stays the Deploy preview's conservative figure until cash lands.
    assert bar.cents_of("free") == after_budget.unreserved_cents and bar.shortfall_cents == 0


def test_a_fully_deployed_accounts_exit_in_flight_is_not_a_shortfall() -> None:
    # Review A2's false advice: a $1,000 bot is the whole $1,000 account; it
    # bought at $990 and sold at $1,000, and cash has not seen the sale. The
    # budget's own available is -$1,000, but the proceeds in flight cover it.
    bot = _bot("all-in", cents=100_000, realized="10")
    budget = account_budget(cash=10, deployments=[bot], order_claims=D(0), fee_claims=D(0))
    assert budget.available == D(-1_000)
    bar = _drawn(_money(budget, unseen_sales=D(1_000)))
    assert _by_kind(bar) == [("bot", "all-in", 101_000), ("free", None, 0)]
    assert bar.shortfall_cents == 0 and bar.total_cents == 101_000


def test_a_flat_bot_that_overran_its_budget_draws_no_free_part() -> None:
    # A $1,000 bot lost $5.01 and is flat: nothing in its slice, its overrun shown.
    bot = _bot("lost", cents=100_000, realized="-1005.01")
    parts = bot_parts(bot)
    assert (parts.in_shares_cents, parts.pending_cents, parts.free_cents) == (0, 0, 0)
    assert (parts.in_shares_bps, parts.pending_bps, parts.free_bps) == (0, 0, 0)
    budget = account_budget(cash="994.99", deployments=[bot], order_claims=D(0), fee_claims=D(0))
    bar = _drawn(_money(budget))
    assert bar.segments[0].shortfall_cents == 501 and bar.segments[0].cents == 0


def test_every_bot_keeps_one_colour_slot_by_registration_order() -> None:
    # Palette slots follow the account's registration order, not the bar's
    # (name) order, so a bot's hue is the same on every surface and survives
    # other bots stopping or finishing.
    a, b = _bot("a", cents=100_000), _bot("b", cents=100_000)
    stopped = _bot("c", cents=100_000, position="10", active=False)
    budget = account_budget(cash=10_000, deployments=[a, b, stopped], order_claims=D(0), fee_claims=D(0))
    bar = _drawn(_money(budget, order=("finished", "b", "c", "a")))
    assert [(segment.strategy_instance_id, segment.palette_index) for segment in bar.segments] == [
        ("a", 3), ("b", 1), ("c", 2), (None, None),
    ]
    with pytest.raises(MoneyConservationError, match="not registered"):
        money_bar(_money(budget, order=("a", "b")))


def test_parts_that_do_not_add_up_refuse_to_draw() -> None:
    # An order claim with no pending entry or unseen fill behind it.
    budget = account_budget(cash=1_000, deployments=[], order_claims=D(1), fee_claims=D(0))
    money = _money(budget)
    with pytest.raises(MoneyConservationError):
        money_bar(money)


def test_empty_account_draws_all_free_and_still_sums_to_the_full_width() -> None:
    budget = AccountBudget(cash=D(0), active_free_claims=D(0), order_claims=D(0), fee_claims=D(0), available=D(0), deployments=())
    bar = _drawn(_money(budget))
    assert [(segment.kind, segment.bps) for segment in bar.segments] == [("free", FULL_BAR_BPS)]


def test_basis_points_follow_the_fee_rule_and_lift_a_tiny_slice_to_one() -> None:
    assert share_bps({"b": 1, "a": 1, "c": 1}, empty="c") == {"a": 3_334, "b": 3_333, "c": 3_333}
    widths = share_bps({"tiny": 1, "huge": 10_000_000}, empty="huge")
    assert widths == {"huge": 9_999, "tiny": 1}
    assert share_bps({"x": 0, "free": 0}, empty="free") == {"x": 0, "free": FULL_BAR_BPS}
    assert share_bps({"x": 0, "y": 0}, empty=None) == {"x": 0, "y": 0}
    with pytest.raises(ValueError):
        share_bps({"x": -1}, empty="x")

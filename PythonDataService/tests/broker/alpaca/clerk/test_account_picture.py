"""What the account owns, what is spoken for and where it is concentrated, through ``account_picture`` alone (#3101).

Every figure is worked by hand from the rules and stated in whole cents:

- the cash is the sum of its claims and what is left (``budgets.account_budget``'s identity), so the lines of
  "spoken for" add up to the cash on every case, a fractional claim included, with one visible rounding line;
- a $5-wide put spread holds a requirement of $500.00 and a $10-wide one of $1,000.00, and Alpaca takes the larger
  expiry of an underlying, so two spreads on two expiries are two rows and an account figure equal to the larger;
- the assignment obligation is each sold contract's |n| · strike · multiplier, gross: one put struck at 665 is
  1 · 665 · 100 = $66,500.00 whatever it can lose;
- the cost to close everything is the sum of the bots' net closes, and it is unknown, and counted, when any is.
"""

from __future__ import annotations

import random
from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.account_picture import (
    EXPIRY_NOTE,
    INFORMS_NOTE,
    OBLIGATION_NOTE,
    OWNED_NOTE,
    PARTITION_ADDS_UP,
    AccountPicture,
    AccountReadings,
    account_picture,
    assignment_obligation,
    collateral_by_underlying_and_expiry,
)
from app.broker.alpaca.clerk.budgets import AccountBudget, DeploymentBudget, account_budget, deployment_budget
from app.broker.alpaca.clerk.exit_estimate import BUY_BACK_ALONE_LINE, FEES_LINE, ExitEstimate, exit_estimate
from app.broker.alpaca.clerk.kept_option_quotes import HELD_MARK_MAX_AGE_MS
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.requirement import ContractAtMultiplier

NOW = 1_800_000_000_000
READ = NOW - 5_000

# A $5-wide put spread on the Oct 16 expiry (505 sold, 500 bought), a $10-wide one on Oct 23 (510 sold, 500 bought).
P505, P500 = "SPY261016P00505000", "SPY261016P00500000"
P510, P500_WEEK = "SPY261023P00510000", "SPY261023P00500000"
SPREAD: dict[ContractAtMultiplier, int] = {(P505, 100): -1, (P500, 100): 1}
WIDE_SPREAD: dict[ContractAtMultiplier, int] = {(P510, 100): -1, (P500_WEEK, 100): 1}
# A $3-wide QQQ spread: 440 sold, 437 bought.
Q440, Q437 = "QQQ261016P00440000", "QQQ261016P00437000"
QQQ_SPREAD: dict[ContractAtMultiplier, int] = {(Q440, 100): -1, (Q437, 100): 1}

OCT_16 = OptionContract.from_occ_symbol(P505).expiry_ms
OCT_23 = OptionContract.from_occ_symbol(P510).expiry_ms


def _bot(
    sid: str, *, committed_cents: int, active: bool = True, position: str = "0", pending: str = "0",
    realized: str = "0", fees: str = "0", collateral: str = "0",
) -> DeploymentBudget:
    return deployment_budget(
        strategy_instance_id=sid, committed_cents=committed_cents, active=active, realized_gross=realized,
        fees=D(fees), position_cost=D(position), pending_orders=D(pending), option_collateral=D(collateral),
    )


def _budget(
    cash: str, bots: list[DeploymentBudget], *, by_bot: dict[str, str] | None = None, outside: str = "0",
    fees: str = "0",
) -> AccountBudget:
    """The account as the projection builds it: the order claims are the outside purchases and the bots' claims."""
    claims = {sid: D(amount) for sid, amount in (by_bot or {}).items()}
    return account_budget(
        cash=cash, deployments=bots, order_claims=D(outside) + sum(claims.values(), D(0)), fee_claims=D(fees),
        outside_claims=D(outside), bot_order_claims=claims,
    )


def _readings(**changes: object) -> AccountReadings:
    arguments: dict[str, object] = {"observed_at_ms": READ, "now_ms": NOW, "equity": D("100012.40")}
    arguments.update(changes)
    return AccountReadings(**arguments)  # type: ignore[arg-type]


def _picture(
    budget: AccountBudget, *, holdings: dict[str, dict[ContractAtMultiplier, int]] | None = None,
    estimates: dict[str, ExitEstimate | None] | None = None, readings: AccountReadings | None = None, why: str = "",
) -> AccountPicture:
    return account_picture(
        budget, readings or _readings(), {} if holdings is None and not why else holdings, estimates or {},
        holdings_unread=why,
    )


def _lines(picture: AccountPicture) -> dict[str, tuple[int | None, str, list[tuple[str, int]]]]:
    """Each line of "spoken for" by kind: its cents, its state and its terms."""
    return {
        line.kind: (line.figure.amount_cents, line.figure.state, [(t.label, t.amount_cents) for t in line.figure.terms])
        for line in picture.spoken_for.lines
    }


def _adds_up(picture: AccountPicture) -> None:
    """The parts of "spoken for" add up to the cash, to the cent, and each line to what it is made of."""
    cash = picture.owned.broker_cash.amount_cents
    lines = picture.spoken_for.lines
    assert cash is not None
    assert sum(line.figure.amount_cents or 0 for line in lines) == cash
    assert picture.spoken_for.partition.amount_cents == cash
    assert sum(term.amount_cents for term in picture.spoken_for.partition.terms) == cash
    for line in lines:
        assert sum(term.amount_cents for term in line.figure.terms) == line.figure.amount_cents


# ---- owned ----------------------------------------------------------------------------------------------


def test_cash_and_marked_equity_are_two_figures_each_with_its_source_and_age() -> None:
    picture = _picture(_budget("98564.86", [_bot("a", committed_cents=100_000)]))

    cash, equity = picture.owned.broker_cash, picture.owned.marked_equity
    assert (cash.amount_cents, cash.state, cash.source, cash.as_of_ms) == (9_856_486, "known", "Alpaca's account answer", READ)
    assert (equity.amount_cents, equity.state, equity.source, equity.as_of_ms) == (
        10_001_240, "known", "Alpaca's account answer", READ,
    )
    assert cash.amount_cents != equity.amount_cents
    assert picture.owned.note == OWNED_NOTE


@pytest.mark.parametrize("world", ["Dry Run", "Live Shadow"])
def test_a_simulated_accounts_cash_and_equity_say_they_are_simulated(world: str) -> None:
    picture = _picture(_budget("1000", []), readings=_readings(equity=D("1010.50"), simulated=world))

    for figure in (picture.owned.broker_cash, picture.owned.marked_equity):
        assert world in figure.source and "simulated" in figure.source and figure.source != "Alpaca's account answer"
    assert picture.owned.marked_equity.amount_cents == 101_050


def test_an_equity_the_reading_did_not_carry_is_unknown_and_never_zero_beside_a_known_cash() -> None:
    picture = _picture(_budget("1000", []), readings=_readings(equity=None))

    equity = picture.owned.marked_equity
    assert (equity.state, equity.amount_cents, equity.terms) == ("unknown", None, ())
    assert equity.as_of_ms == READ
    assert picture.owned.broker_cash.state == "known" and picture.owned.broker_cash.amount_cents == 100_000


def test_a_reading_past_its_age_limit_is_stale_with_the_amount_it_last_had() -> None:
    old = NOW - OBSERVATION_MAX_AGE_MS - 1
    picture = _picture(_budget("1000", [_bot("a", committed_cents=50_000)]), readings=_readings(observed_at_ms=old))

    assert (picture.owned.broker_cash.state, picture.owned.broker_cash.amount_cents) == ("stale", 100_000)
    assert picture.owned.marked_equity.state == "stale" and picture.owned.marked_equity.as_of_ms == old
    lines = _lines(picture)
    # The free claim is the books' own and does not wait on the reading; what stands on the reading is stale.
    assert lines["bot_free"][1] == "known"
    assert lines["unreserved"][1] == lines["fees"][1] == "stale"
    assert picture.spoken_for.partition.state == "stale"
    _adds_up(picture)


# ---- spoken for -----------------------------------------------------------------------------------------


def test_what_is_spoken_for_adds_up_to_the_cash_and_opens_by_bot() -> None:
    """$10,000.00 of cash. Bot a: $2,000.00 committed, $300.50 of it in a working entry order, so $1,699.50 free.
    Bot b: $1,000.00 free. $0.25 of fees not posted. Unreserved: 10,000 - 1,699.50 - 1,000 - 300.50 - 0.25."""
    a = _bot("a", committed_cents=200_000, pending="300.50")
    b = _bot("b", committed_cents=100_000)
    picture = _picture(_budget("10000", [a, b], by_bot={"a": "300.50"}, fees="0.25"))

    assert _lines(picture) == {
        "bot_free": (269_950, "known", [("Bot a", 169_950), ("Bot b", 100_000)]),
        "collateral": (0, "known", []),
        "orders": (30_050, "known", [("Bot a", 30_050)]),
        "outside": (0, "known", []),
        "fees": (25, "known", [("Fees not posted yet", 25)]),
        "unreserved": (699_975, "known", [
            ("Cash as the account reports it", 1_000_000), ("Set aside for running bots", -269_950),
            ("Held in entry orders", -30_050), ("Fees not posted yet", -25),
        ]),
    }
    assert picture.spoken_for.partition.state == "known"
    assert picture.spoken_for.sentence == PARTITION_ADDS_UP
    _adds_up(picture)


def test_a_stopped_bots_collateral_and_an_outside_purchase_appear_by_name_and_still_add_up() -> None:
    """$5,000.00 of cash. Bot run has $1,000.00 free. Stopped bot old holds a credit spread whose collateral is
    $500.00 (its free budget was released at its Stop). A manual buy of $200.00 is not in the cash yet.
    Unreserved: 5,000 - 1,000 - 500 - 200 = 3,300."""
    run = _bot("run", committed_cents=100_000)
    old = _bot("old", committed_cents=80_000, active=False, position="350", collateral="500")
    picture = _picture(_budget("5000", [run, old], outside="200"))

    assert _lines(picture) == {
        "bot_free": (100_000, "known", [("Bot run", 100_000)]),
        "collateral": (50_000, "known", [("Stopped bot old", 50_000)]),
        "orders": (0, "known", []),
        "outside": (20_000, "known", [("Manual trades and outside orders", 20_000)]),
        "fees": (0, "known", []),
        "unreserved": (330_000, "known", [
            ("Cash as the account reports it", 500_000), ("Set aside for running bots", -100_000),
            ("Collateral held against sold options", -50_000), ("Outside the system", -20_000),
        ]),
    }
    _adds_up(picture)


def test_a_stopped_bots_working_order_is_its_own_row_by_name() -> None:
    gone = _bot("gone", committed_cents=50_000, active=False, pending="80")
    picture = _picture(_budget("1000", [gone], by_bot={"gone": "80"}))

    assert _lines(picture)["orders"] == (8_000, "known", [("Stopped bot gone", 8_000)])
    _adds_up(picture)


def test_claims_beyond_the_cash_are_a_negative_unreserved_line_that_still_adds_up() -> None:
    """$1,000.00 of cash against a bot's $1,500.00 claim: unreserved is -$500.00, said as claims beyond the cash."""
    picture = _picture(_budget("1000", [_bot("a", committed_cents=150_000)]))

    unreserved = next(line for line in picture.spoken_for.lines if line.kind == "unreserved")
    assert (unreserved.label, unreserved.figure.amount_cents) == ("Claims beyond the cash", -50_000)
    assert picture.spoken_for.sentence.endswith("The claims on the account's cash are more than the cash.")
    _adds_up(picture)


def test_a_fractional_claim_adds_up_with_one_visible_rounding_line() -> None:
    """Bot a's free budget is $100.004 ($100.00 and a $0.004 gain): $100.00 as a claim. The account's unreserved
    cash is 1,000 - 100.004 = 899.996, which floors to $899.99 as the Deploy preview's does. The cent between them
    is stated, not hidden."""
    a = _bot("a", committed_cents=10_000, realized="0.004")
    picture = _picture(_budget("1000", [a]))

    lines = _lines(picture)
    assert lines["bot_free"][0:2] == (10_000, "known")
    assert lines["unreserved"][0] == 89_999
    assert lines["rounding"] == (1, "known", [("Rounding to whole cents", 1)])
    _adds_up(picture)


def test_the_unreserved_line_is_the_deploy_previews_own_unreserved_cash() -> None:
    budget = _budget("1000", [_bot("a", committed_cents=10_000, realized="0.004")])

    picture = _picture(budget)

    unreserved = next(line for line in picture.spoken_for.lines if line.kind == "unreserved")
    assert unreserved.figure.amount_cents == budget.unreserved_cents


def test_a_line_that_cannot_be_worked_out_makes_the_partition_unknown_and_is_never_left_out() -> None:
    """A budget with order claims that nothing says the bots or the outside own: its orders and outside lines are
    unknown, so the partition is, and the lines that are known are still shown."""
    unsplit = account_budget(
        cash="1000", deployments=[_bot("a", committed_cents=50_000)], order_claims=D("100"), fee_claims=D(0),
    )
    picture = _picture(unsplit)

    lines = _lines(picture)
    assert lines["orders"][0:2] == (None, "unknown") and lines["outside"][0:2] == (None, "unknown")
    assert lines["bot_free"][0] == 50_000 and lines["unreserved"][0] == 40_000
    assert (picture.spoken_for.partition.state, picture.spoken_for.partition.amount_cents) == ("unknown", None)
    assert picture.spoken_for.sentence.startswith("The lines cannot be added up to the account's cash:")
    assert picture.owned.broker_cash.amount_cents == 100_000


def test_the_lines_add_up_to_the_cash_on_every_constructed_case() -> None:
    """Seeded cases with fractional claims, collateral, orders, outside purchases, stopped bots and fees."""
    rng = random.Random(3101)
    for _ in range(300):
        bots: list[DeploymentBudget] = []
        by_bot: dict[str, str] = {}
        for index in range(rng.randint(0, 4)):
            sid = f"bot{index}"
            active = rng.random() < 0.7
            pending = rng.choice(["0", f"{rng.randint(0, 5_000_000) / 10_000:.4f}"])
            collateral = rng.choice(["0", f"{rng.randint(0, 100_000) / 100:.2f}"])
            bots.append(_bot(
                sid, committed_cents=rng.randint(0, 500_000), active=active, pending=pending,
                realized=f"{rng.randint(-5_000, 5_000) / 1000:.3f}", fees=f"{rng.randint(0, 400) / 10_000:.4f}",
                collateral=collateral, position=f"{rng.randint(0, 20_000) / 100:.2f}",
            ))
            if D(pending):
                by_bot[sid] = pending
        budget = _budget(
            f"{rng.randint(0, 2_000_000) / 100:.2f}", bots, by_bot=by_bot,
            outside=rng.choice(["0", f"{rng.randint(0, 100_000) / 100:.2f}", f"{rng.randint(0, 9_999) / 10_000:.4f}"]),
            fees=rng.choice(["0", f"{rng.randint(0, 9_999) / 10_000:.4f}"]),
        )
        _adds_up(_picture(budget))


# ---- concentration --------------------------------------------------------------------------------------


def test_two_spreads_on_two_expiries_are_two_rows_and_an_account_figure_equal_to_the_larger() -> None:
    """One bot holds a $5-wide spread expiring Oct 16 ($500.00) and a $10-wide one expiring Oct 23 ($1,000.00).
    Alpaca takes the larger expiry of the underlying: the rows add up to $1,500.00, the account's figure is $1,000.00."""
    held = {**SPREAD, **WIDE_SPREAD}
    picture = _picture(_budget("10000", []), holdings={"a": held})

    concentration = picture.concentration
    assert [(g.underlying, g.expiry_ms, g.collateral_cents) for g in concentration.groups] == [
        ("SPY", OCT_16, 50_000), ("SPY", OCT_23, 100_000),
    ]
    assert (concentration.account_collateral.state, concentration.account_collateral.amount_cents) == ("known", 100_000)
    assert [(t.label, t.amount_cents) for t in concentration.account_collateral.terms] == [("SPY", 100_000)]
    assert sum(g.collateral_cents for g in concentration.groups) == 150_000 > 100_000
    assert concentration.expiry_note == EXPIRY_NOTE


def test_independent_underlyings_add_into_the_account_figure() -> None:
    picture = _picture(_budget("10000", []), holdings={"a": SPREAD, "b": QQQ_SPREAD})

    account = picture.concentration.account_collateral
    assert account.amount_cents == 80_000
    assert [(t.label, t.amount_cents) for t in account.terms] == [("QQQ", 30_000), ("SPY", 50_000)]
    assert [(g.underlying, g.collateral_cents) for g in picture.concentration.groups] == [("QQQ", 30_000), ("SPY", 50_000)]


def test_holdings_are_netted_across_bots_before_they_are_valued() -> None:
    """Bot a holds the spread and bot b holds its opposite: the account holds nothing, whatever bot a set aside."""
    opposite = {key: -count for key, count in SPREAD.items()}
    a = _bot("a", committed_cents=100_000, position="500", collateral="500")
    b = _bot("b", committed_cents=100_000)
    picture = _picture(_budget("10000", [a, b]), holdings={"a": SPREAD, "b": opposite})

    assert picture.concentration.groups == ()
    assert (picture.concentration.account_collateral.amount_cents, picture.concentration.expiry_note) == (0, None)
    # The bots set aside their own collateral one by one, which is what the cash is spoken for by.
    assert _lines(picture)["collateral"][0] == 50_000
    assert picture.obligations.assignment.amount_cents == 0


def test_a_contract_held_by_two_bots_on_one_side_adds() -> None:
    picture = _picture(_budget("10000", []), holdings={"a": SPREAD, "b": SPREAD})

    assert picture.concentration.account_collateral.amount_cents == 100_000
    assert [g.collateral_cents for g in picture.concentration.groups] == [100_000]


def test_contracts_of_another_multiplier_are_valued_apart_and_still_add_up_by_underlying() -> None:
    """An adjusted SPY spread of 10 shares a contract: $50.00, beside the standard one's $500.00, on one expiry."""
    odd: dict[ContractAtMultiplier, int] = {("SPY261016P00495000", 10): -1, ("SPY261016P00490000", 10): 1}
    picture = _picture(_budget("10000", []), holdings={"a": {**SPREAD, **odd}})

    assert picture.concentration.account_collateral.amount_cents == 55_000
    assert [(t.label, t.amount_cents) for t in picture.concentration.account_collateral.terms] == [("SPY", 55_000)]


def test_the_groups_alone_are_the_canonical_requirement_of_each() -> None:
    assert collateral_by_underlying_and_expiry({**SPREAD, **WIDE_SPREAD, **QQQ_SPREAD}) == {
        ("QQQ", OCT_16): 30_000, ("SPY", OCT_16): 50_000, ("SPY", OCT_23): 100_000,
    }
    assert collateral_by_underlying_and_expiry({}) == {}
    # A group of bought contracts alone needs nothing, and a contract netting to zero is no group.
    assert collateral_by_underlying_and_expiry({(P500, 100): 3, (P505, 100): 0}) == {("SPY", OCT_16): 0}


def test_holdings_whose_loss_has_no_limit_make_the_concentration_unknown_with_the_reason() -> None:
    naked: dict[ContractAtMultiplier, int] = {("SPY261016C00585000", 100): -1}
    picture = _picture(_budget("10000", []), holdings={"a": naked})

    concentration = picture.concentration
    assert (concentration.account_collateral.state, concentration.groups) == ("unknown", ())
    assert "loss grows without limit" in concentration.account_collateral.source


# ---- obligations ----------------------------------------------------------------------------------------


def test_one_sold_put_struck_at_665_is_an_obligation_of_66500_dollars() -> None:
    assert assignment_obligation({("SPY261016P00665000", 100): -1}) == {"SPY": 6_650_000}


def test_a_sold_put_at_665_shows_66500_dollars_beside_its_own_loss_bound_of_500() -> None:
    """The 665 put sold and the 660 put bought: the loss is bounded at $5 · 100 = $500.00 and the account holds
    $500.00 of collateral, while the sold put can oblige 100 shares at 665: $66,500.00."""
    held: dict[ContractAtMultiplier, int] = {("SPY261016P00665000", 100): -1, ("SPY261016P00660000", 100): 1}

    picture = _picture(_budget("10000", []), holdings={"a": held})

    assert picture.obligations.assignment.amount_cents == 6_650_000
    assert picture.concentration.account_collateral.amount_cents == 50_000
    assert picture.obligations.assignment_note == OBLIGATION_NOTE


def test_the_obligation_is_gross_by_underlying_and_apart_from_the_loss_it_can_make() -> None:
    """The $5-wide spread loses at most $500.00 and holds $500.00 of collateral, yet the sold 505 put can oblige the
    account to buy 100 shares at 505: $50,500.00. The bought 500 put offsets none of it here."""
    picture = _picture(_budget("10000", []), holdings={"a": SPREAD, "b": QQQ_SPREAD})

    assignment = picture.obligations.assignment
    assert (assignment.state, assignment.amount_cents) == ("known", 9_450_000)
    assert [(t.label, t.amount_cents) for t in assignment.terms] == [("QQQ", 4_400_000), ("SPY", 5_050_000)]
    assert picture.concentration.account_collateral.amount_cents == 80_000
    assert picture.obligations.assignment_note == OBLIGATION_NOTE
    assert "a bounded loss is not a bounded obligation" in OBLIGATION_NOTE


def test_obligation_counts_contracts_and_each_contracts_own_multiplier() -> None:
    held: dict[ContractAtMultiplier, int] = {
        ("SPY261016P00665000", 100): -3, ("SPY261016C00700000", 100): -2, ("SPY261016P00012500", 10): -2,
        ("SPY261016P00600000", 100): 5,
    }
    # 3 · 665 · 100 + 2 · 700 · 100 + 2 · 12.5 · 10 = 199,500 + 140,000 + 250.
    assert assignment_obligation(held) == {"SPY": 19_950_000 + 14_000_000 + 25_000}


def test_an_obligation_is_zero_and_known_when_nothing_is_sold() -> None:
    picture = _picture(_budget("10000", []), holdings={"a": {(P500, 100): 2}})

    assert (picture.obligations.assignment.state, picture.obligations.assignment.amount_cents) == ("known", 0)
    assert assignment_obligation({}) == {}


def test_an_obligation_that_is_not_whole_cents_is_refused_never_rounded() -> None:
    with pytest.raises(ValueError, match="whole cents"):
        assignment_obligation({("SPY261016P00100005", 1): -1})


@pytest.mark.parametrize(
    "held",
    [{(P505, 100): True}, {(P505, 100): 1.5}, {(P505, 0): -1}, {("not-a-contract", 100): -1}],
)
def test_holdings_that_are_not_whole_contracts_at_a_multiplier_are_refused(held: dict) -> None:
    with pytest.raises(ValueError):
        assignment_obligation(held)


# ---- the cost to close everything -------------------------------------------------------------------------


def _quote(bid: str | None, ask: str | None, *, age_ms: int = 1_000) -> HeldQuote:
    return HeldQuote(
        bid=None if bid is None else QuoteSide(D(bid), NOW - age_ms),
        ask=None if ask is None else QuoteSide(D(ask), NOW - age_ms),
        data_type=QuoteDataType.LIVE, generation=1,
    )


def _estimate(legs: dict[ContractAtMultiplier, int], quotes: dict[str, HeldQuote | None]) -> ExitEstimate:
    return exit_estimate(legs, quotes, now_ms=NOW, quotes_from="Quotes kept for this Dry Run", none_held="none kept")


# Deep in the money: the sold 505 put is bought back at 100.00 ($10,000.00) and the bought 500 put sold at 95.00
# ($9,500.00): the whole close pays $500.00.
DEEP = {P505: _quote("99.90", "100.00"), P500: _quote("95.00", "95.10")}


def test_the_cost_to_close_everything_is_the_sum_of_the_bots_net_closes() -> None:
    """Bot a's spread closes for +$500.00. Bot b holds one bought 500 put sold at its 2.20 bid: -$220.00 comes back."""
    estimates = {
        "a": _estimate(SPREAD, DEEP),
        "b": _estimate({(P500, 100): 1}, {P500: _quote("2.20", "2.24")}),
    }
    picture = _picture(_budget("10000", []), holdings={"a": SPREAD, "b": {(P500, 100): 1}}, estimates=estimates)

    close = picture.obligations.close_everything
    assert (close.state, close.amount_cents) == ("known", 50_000 - 22_000)
    assert [(t.label, t.amount_cents) for t in close.terms] == [("Stopped bot a", 50_000), ("Stopped bot b", -22_000)]
    assert (picture.obligations.bots_holding, picture.obligations.bots_unpriced) == (2, 0)
    assert picture.obligations.conditions == (FEES_LINE, BUY_BACK_ALONE_LINE)


def test_a_running_bot_is_named_as_running_in_the_total() -> None:
    picture = _picture(
        _budget("10000", [_bot("a", committed_cents=100_000)]), holdings={"a": SPREAD},
        estimates={"a": _estimate(SPREAD, DEEP)},
    )

    assert [t.label for t in picture.obligations.close_everything.terms] == ["Bot a"]


def test_one_unknown_exit_estimate_makes_the_total_unknown_and_counted() -> None:
    estimates = {
        "a": _estimate(SPREAD, DEEP),
        "b": _estimate({(P500, 100): 1}, {P500: _quote(None, "2.24")}),
    }
    picture = _picture(_budget("10000", []), holdings={"a": SPREAD, "b": {(P500, 100): 1}}, estimates=estimates)

    close = picture.obligations.close_everything
    assert (close.state, close.amount_cents, close.terms) == ("unknown", None, ())
    assert close.source.startswith("1 of 2 bots' exit estimates cannot be priced, so the cost to close everything is not known: ")
    assert "Stopped bot b" in close.source and "no bid has arrived" in close.source
    assert (picture.obligations.bots_holding, picture.obligations.bots_unpriced) == (2, 1)


def test_a_bot_whose_estimate_could_not_be_worked_out_is_one_of_the_unknown() -> None:
    picture = _picture(
        _budget("10000", []), holdings={"a": SPREAD, "b": QQQ_SPREAD}, estimates={"a": _estimate(SPREAD, DEEP), "b": None},
    )

    close = picture.obligations.close_everything
    assert close.state == "unknown" and "its exit estimate could not be worked out" in close.source
    assert (picture.obligations.bots_holding, picture.obligations.bots_unpriced) == (2, 1)


def test_a_bot_with_no_estimate_at_all_is_unknown_never_left_out_of_the_total() -> None:
    picture = _picture(_budget("10000", []), holdings={"a": SPREAD, "b": QQQ_SPREAD}, estimates={"a": _estimate(SPREAD, DEEP)})

    assert picture.obligations.close_everything.state == "unknown"
    assert picture.obligations.bots_unpriced == 1


def test_a_stale_estimate_makes_the_total_stale_with_the_amount_it_last_had() -> None:
    quotes = {P505: _quote("99.90", "100.00", age_ms=HELD_MARK_MAX_AGE_MS + 1), P500: DEEP[P500]}
    picture = _picture(
        _budget("10000", []), holdings={"a": SPREAD}, estimates={"a": _estimate(SPREAD, quotes)},
    )

    close = picture.obligations.close_everything
    assert (close.state, close.amount_cents) == ("stale", 50_000)
    assert close.as_of_ms == NOW - HELD_MARK_MAX_AGE_MS - 1


def test_the_total_is_zero_and_known_when_no_bot_holds_an_option() -> None:
    picture = _picture(_budget("10000", []), holdings={})

    close = picture.obligations.close_everything
    assert (close.state, close.amount_cents, close.terms) == ("known", 0, ())
    assert (picture.obligations.bots_holding, picture.obligations.bots_unpriced) == (0, 0)


def test_a_bought_position_alone_says_nothing_of_buying_a_sold_leg_back() -> None:
    held = {(P500, 100): 1}
    picture = _picture(
        _budget("10000", []), holdings={"a": held}, estimates={"a": _estimate(held, {P500: _quote("2.20", "2.24")})},
    )

    assert picture.obligations.conditions == (FEES_LINE,)


# ---- holdings the books could not be read for -----------------------------------------------------------


def test_holdings_that_could_not_be_read_make_every_figure_standing_on_them_unknown_and_nothing_else() -> None:
    a = _bot("a", committed_cents=100_000)
    picture = _picture(_budget("10000", [a]), holdings=None, why="SPY261016P00505000 is held with no recorded terms")

    for figure in (
        picture.concentration.account_collateral, picture.obligations.assignment, picture.obligations.close_everything,
    ):
        assert (figure.state, figure.amount_cents, figure.terms) == ("unknown", None, ())
        assert "held with no recorded terms" in figure.source
    assert picture.concentration.groups == ()
    # What stands on the cash is still told.
    assert picture.owned.broker_cash.state == "known" and picture.spoken_for.partition.state == "known"
    _adds_up(picture)


def test_the_picture_informs_and_blocks_nothing() -> None:
    picture = _picture(_budget("10000", []))

    assert picture.note == INFORMS_NOTE

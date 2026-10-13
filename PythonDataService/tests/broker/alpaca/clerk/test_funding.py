"""Whether a bot can fund an entry, which limit binds and the shortfall, through ``funding_answer`` alone (#3097).

Every figure is worked by hand from the rule: an entry is admitted when it fits
the bot's free budget and the account's headroom for it, in whole cents; the
known limit with the least room binds, ties in the order bot budget, account
headroom, broker buying power.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.budgets import (
    AccountBudget,
    DeploymentBudget,
    account_budget,
    deployment_budget,
)
from app.broker.alpaca.clerk.funding import EntryNeed, Figure, FundingAnswer, Term, funding_answer
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS
from tests.broker.alpaca.clerk.sqlite.conftest import NOON


def _bot(
    strategy_instance_id: str, *, committed: int = 1000, active: bool = True, realized: object = 0,
    fees: object = 0, position_cost: int = 0, pending: int = 0, collateral: int = 0,
) -> DeploymentBudget:
    """A bot's budget in whole dollars; ``position_cost`` is the signed basis plus the collateral held."""
    return deployment_budget(
        strategy_instance_id=strategy_instance_id, committed_cents=committed * 100, active=active,
        realized_gross=realized, fees=D(fees), position_cost=D(position_cost), pending_orders=D(pending),
        option_collateral=D(collateral),
    )


def _account(*bots: DeploymentBudget, cash: object) -> AccountBudget:
    return account_budget(cash=cash, deployments=list(bots), order_claims=D(0), fee_claims=D(0))


def _need(cents: int, *, fee: int = 0) -> EntryNeed:
    return EntryNeed.for_options(commitment_cents=cents, fee_provision_cents=fee)


def _answer(
    account: AccountBudget | None, *, sid: str = "b", need: EntryNeed | None = None,
    broker: Figure | None = None, observed_at_ms: int = NOON, now_ms: int = NOON,
) -> FundingAnswer:
    answer = funding_answer(
        account, strategy_instance_id=sid, need=need, broker_buying_power=broker,
        observed_at_ms=observed_at_ms, now_ms=now_ms,
    )
    for figure in (answer.free_budget, answer.account_headroom, answer.broker_buying_power):
        if figure is not None and figure.amount_cents is not None:
            assert sum(term.amount_cents for term in figure.terms) == figure.amount_cents
    if answer.need is not None:
        assert sum(term.amount_cents for term in answer.need.terms) == answer.need.commitment_cents
    return answer


def _broker(cents: int, *, as_of_ms: int = NOON) -> Figure:
    return Figure(cents, "known", "Alpaca option buying power", as_of_ms, (Term("Option buying power", cents),))


def _reproduced_account(cash: object = 1500) -> AccountBudget:
    """The account #3096 reproduced: A lost its whole $1,000 budget and holds $1,000 of collateral.

    Cash $1,500. A: free -$500, claim $0. B: free $1,000, claim $1,000. The account's unreserved cash is
    1,500 - 1,000 - 1,000 = -$500, so it can fund B -500 + 1,000 = $500.
    """
    a = _bot("a", realized=-1000, position_cost=500, collateral=1000)
    return _account(a, _bot("b"), cash=cash)


# ---- each limit binding in turn -------------------------------------------------------------


def test_the_accounts_headroom_binds_and_the_sentence_says_what_the_account_can_fund() -> None:
    """The PRD's own case: $1,000 free, the account can fund $500, the entry needs $800."""
    answer = _answer(_reproduced_account(), need=_need(80_000, fee=25))

    assert (answer.free_budget.amount_cents, answer.account_headroom.amount_cents) == (100_000, 50_000)
    assert [(term.label, term.amount_cents) for term in answer.account_headroom.terms] == [
        ("Unreserved account cash", -50_000), ("This bot's own claim", 100_000),
    ]
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "account_headroom", 30_000, 0)
    assert answer.sentence == (
        "This bot has $1,000.00 free, but the account can fund only $500.00 after protecting other positions. "
        "The entry needs $800.00 including its fee provision. Shortfall: $300.00."
    )
    assert answer.action == "Free another bot's budget, or add cash to the account."


def test_the_bots_own_budget_binds_when_it_is_the_least() -> None:
    """A $300 budget on an account with $5,000: the budget is what falls short of $800."""
    answer = _answer(_account(_bot("b", committed=300), cash=5000), need=_need(80_000))

    assert (answer.free_budget.amount_cents, answer.account_headroom.amount_cents) == (30_000, 500_000)
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "bot_budget", 50_000, 0)
    assert answer.sentence == (
        "This bot has only $300.00 free. The entry needs $800.00 including its fee provision. Shortfall: $500.00."
    )
    assert answer.action == "Stop the bot and deploy it again with a larger budget, or reduce its size."


def test_both_own_limits_short_name_the_budget_and_the_account_s_figure_too() -> None:
    """$300 free and cash of $400 (so $400 fundable): both fall short of $800, the budget by more."""
    answer = _answer(_account(_bot("b", committed=300), cash=400), need=_need(80_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", 50_000)
    assert answer.sentence == (
        "This bot has only $300.00 free. The account can fund only $400.00 for it after protecting other positions. "
        "The entry needs $800.00 including its fee provision. Shortfall: $500.00."
    )


def test_broker_buying_power_can_bind_but_never_changes_what_the_clerk_admits() -> None:
    """$1,000 free and fundable; Alpaca's option buying power $500 against an $800 entry.

    Our own rule passes, so ``admits`` is true; the broker binds, $300 short, and the sentence says Alpaca would
    likely refuse and that passing our limits promises nothing.
    """
    answer = _answer(_account(_bot("b"), cash=1000), need=_need(80_000), broker=_broker(50_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (True, "broker_buying_power", 30_000, 0)
    assert answer.sentence == (
        "This bot has $1,000.00 free and the account can fund $1,000.00 after protecting other positions, "
        "enough for the entry's $800.00 including its fee provision. "
        "Alpaca's option buying power is only $500.00, so Alpaca would likely refuse the order. Shortfall: $300.00. "
        "Passing our own limits does not guarantee that Alpaca accepts the order."
    )
    assert answer.action is None


def test_a_refusal_the_bots_budget_makes_is_bound_by_the_budget_though_buying_power_has_less_room() -> None:
    """The $300 budget refuses $800; Alpaca's $100 is the smallest figure but never refuses (#3122).

    The answer is what it is without the broker's figure: the budget binds, $500 short, with the budget's action.
    """
    answer = _answer(_account(_bot("b", committed=300), cash=5000), need=_need(80_000), broker=_broker(10_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "bot_budget", 50_000, 0)
    assert answer.sentence == (
        "This bot has only $300.00 free. The entry needs $800.00 including its fee provision. Shortfall: $500.00."
    )
    assert answer.action == "Stop the bot and deploy it again with a larger budget, or reduce its size."
    assert answer.fits_sentence == (
        "No whole entry fits right now under the bot's budget, the limit that refuses this entry. "
        "Every bot competes for the same account headroom, so these counts are never added up across bots."
    )


def test_a_refusal_the_accounts_headroom_makes_is_bound_by_the_headroom_though_buying_power_has_less_room() -> None:
    answer = _answer(_reproduced_account(), need=_need(80_000), broker=_broker(10_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "account_headroom", 30_000, 0)
    assert answer.sentence == (
        "This bot has $1,000.00 free, but the account can fund only $500.00 after protecting other positions. "
        "The entry needs $800.00 including its fee provision. Shortfall: $300.00."
    )
    assert answer.action == "Free another bot's budget, or add cash to the account."


def test_an_entry_every_limit_covers_passes_with_the_note_and_counts_how_many_fit() -> None:
    answer = _answer(_account(_bot("b"), cash=1000), need=_need(40_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (True, "bot_budget", 0, 2)
    assert answer.sentence == (
        "This bot has $1,000.00 free and the account can fund $1,000.00 after protecting other positions, "
        "enough for the entry's $400.00 including its fee provision. "
        "Passing our own limits does not guarantee that Alpaca accepts the order."
    )
    assert answer.action is None


@pytest.mark.parametrize(
    ("limit_cents", "need_cents", "admits", "shortfall", "fits"),
    [(100_000, 99_999, True, 0, 1), (100_000, 100_000, True, 0, 1), (100_000, 100_001, False, 1, 0)],
)
def test_the_comparison_is_exact_to_the_cent(
    limit_cents: int, need_cents: int, admits: bool, shortfall: int, fits: int,
) -> None:
    answer = _answer(_account(_bot("b", committed=limit_cents // 100), cash=limit_cents // 100), need=_need(need_cents))

    assert (answer.admits, answer.shortfall_cents, answer.fits) == (admits, shortfall, fits)


def test_ties_go_to_the_bot_budget_then_the_account_headroom_then_the_broker() -> None:
    reproduced = _reproduced_account()
    three_way = _answer(_account(_bot("b"), cash=1000), need=_need(1), broker=_broker(100_000))
    headroom_and_broker = _answer(reproduced, need=_need(1), broker=_broker(50_000))
    broker_below = _answer(reproduced, need=_need(1), broker=_broker(49_999))

    assert three_way.binding == "bot_budget"
    assert headroom_and_broker.binding == "account_headroom"
    assert broker_below.binding == "broker_buying_power"


def test_a_stale_or_unknown_limit_is_never_the_one_that_binds() -> None:
    unknown_broker = Figure.unknown("Alpaca's option buying power is read at Deploy")
    answer = _answer(_account(_bot("b"), cash=1000), need=_need(1), broker=unknown_broker)

    assert answer.binding == "bot_budget"
    assert answer.broker_buying_power == unknown_broker


# ---- no entry priced yet ------------------------------------------------------------------


def test_with_no_entry_priced_the_limits_are_shown_and_nothing_is_judged() -> None:
    answer = _answer(_account(_bot("b"), cash=1000), need=None)

    assert (answer.free_budget.amount_cents, answer.account_headroom.amount_cents) == (100_000, 100_000)
    assert (answer.need, answer.admits, answer.shortfall_cents, answer.fits, answer.action) == (None, None, None, None, None)
    assert answer.binding == "bot_budget"
    assert answer.sentence == "This bot has $1,000.00 free and the account can fund $1,000.00 after protecting other positions."


def test_with_no_entry_priced_a_budget_with_nothing_free_admits_nothing_since_an_entry_costs_something() -> None:
    answer = _answer(_account(_bot("b", realized=-1000), cash=1000), need=None)

    assert answer.free_budget.amount_cents == 0
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", None)
    assert answer.sentence == "Nothing of its budget is free to trade, and an entry always costs something."
    assert answer.action == "Stop the bot and deploy it again with a larger budget, or reduce its size."


def test_with_no_entry_priced_an_account_that_can_fund_nothing_admits_nothing_and_says_so() -> None:
    """$100 free of a $1,000 budget, and the account holds no cash: unreserved -$100, fundable $0."""
    answer = _answer(_account(_bot("b", realized=-900), cash=0), need=None)

    assert (answer.free_budget.amount_cents, answer.account_headroom.amount_cents) == (10_000, 0)
    assert (answer.admits, answer.binding) == (False, "account_headroom")
    assert answer.sentence == (
        "The account can fund nothing for this bot after protecting other positions, and an entry always costs something."
    )
    assert answer.action == "Free another bot's budget, or add cash to the account."


# ---- stale and unknown ---------------------------------------------------------------------


def test_a_reading_older_than_the_envelopes_limit_makes_the_headroom_stale_and_the_verdict_unsaid() -> None:
    account = _account(_bot("b"), cash=1000)

    at_the_limit = _answer(account, need=_need(80_000), observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS)
    past_it = _answer(account, need=_need(80_000), observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS - 1)
    dated_ahead = _answer(account, need=_need(80_000), observed_at_ms=NOON + 1)

    assert (at_the_limit.account_headroom.state, at_the_limit.admits) == ("known", True)
    for stale in (past_it, dated_ahead):
        assert stale.account_headroom.state == "stale"
        assert stale.account_headroom.amount_cents == 100_000, "a stale figure is the last one worked out"
        assert (stale.admits, stale.shortfall_cents, stale.fits, stale.action) == (None, None, None, None)
        assert stale.binding == "bot_budget"
        assert stale.sentence == (
            "This bot has $1,000.00 free. The account's headroom for it was last read more than 45 seconds ago, "
            "so it is not counted here. Whether the entry fits is not said. "
            "The entry needs $800.00 including its fee provision."
        )
    assert past_it.account_headroom.as_of_ms == NOON - OBSERVATION_MAX_AGE_MS - 1


def test_a_refusal_a_known_limit_makes_stands_though_the_other_limit_is_stale() -> None:
    answer = _answer(
        _account(_bot("b", committed=300), cash=5000), need=_need(80_000), observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS - 1,
    )

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", 50_000)
    assert answer.sentence == (
        "This bot has only $300.00 free. The entry needs $800.00 including its fee provision. Shortfall: $500.00. "
        "The account's headroom for it was last read more than 45 seconds ago, so it is not counted here."
    )


def test_a_broker_figure_older_than_the_limit_is_stale_and_does_not_bind() -> None:
    answer = _answer(
        _account(_bot("b"), cash=1000), need=_need(40_000), broker=_broker(10_000, as_of_ms=NOON - OBSERVATION_MAX_AGE_MS - 1),
    )

    assert answer.broker_buying_power is not None and answer.broker_buying_power.state == "stale"
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (True, "bot_budget", 0)


def test_an_unavailable_budget_leaves_every_figure_unknown_never_zero_and_admits_says_nothing() -> None:
    answer = _answer(None, need=_need(80_000))

    for figure in (answer.free_budget, answer.account_headroom):
        assert (figure.state, figure.amount_cents, figure.terms, figure.as_of_ms) == ("unknown", None, (), None)
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits, answer.action) == (None, None, None, None, None)
    assert answer.sentence == "This bot's budget cannot be read right now, so what it can fund is unknown."


def test_a_bot_the_books_hold_no_budget_for_is_unknown_and_the_clerk_refuses_it() -> None:
    answer = _answer(_account(_bot("b"), cash=1000), sid="ghost", need=_need(100))

    assert (answer.free_budget.state, answer.account_headroom.state) == ("unknown", "unknown")
    assert answer.admits is None
    assert answer.sentence.startswith("This deployment has no budget on this account's books.")


def test_a_stopped_bot_is_refused_whatever_its_figures_are() -> None:
    answer = _answer(_account(_bot("b", active=False), cash=1000), need=_need(100))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits, answer.action) == (False, None, None, None, None)
    assert answer.sentence == "This deployment is stopped. Review a fresh Deploy before creating new exposure."


# ---- over-committed and over budget ------------------------------------------------------------


def test_an_account_over_committed_by_collateral_names_what_it_is_over_by() -> None:
    """Cash $900 on the reproduced account: unreserved -$1,100, fundable for B -$100, so $800 is $900 short."""
    answer = _answer(_reproduced_account(cash=900), need=_need(80_000))

    assert answer.account_headroom.amount_cents == -10_000
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "account_headroom", 90_000)
    assert answer.sentence == (
        "This bot has $1,000.00 free, but the account can fund nothing for it after protecting other positions, "
        "because it is $100.00 over-committed. The entry needs $800.00 including its fee provision. Shortfall: $900.00."
    )


def test_a_bot_over_its_budget_is_short_by_what_it_would_take_to_fund_the_entry() -> None:
    """Lost $1,005 of a $1,000 budget: $5.00 over. A $1,000 entry needs $1,005.00 more budget."""
    answer = _answer(_account(_bot("b", realized=-1005), cash=5000), need=_need(100_000))

    assert answer.free_budget.amount_cents == -500
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", 100_500)
    assert answer.sentence == (
        "This bot is $5.00 over its budget. The entry needs $1,000.00 including its fee provision. Shortfall: $1,005.00."
    )


# ---- terms and the need ----------------------------------------------------------------------------


def test_the_free_budget_shows_what_it_is_made_of_including_collateral() -> None:
    """$1,000 budget, $12.40 realized, $0.03 of fees; $60.00 of shares at cost beside $40.00 of collateral
    ($100.00 of position cost in all), and a $25.00 entry order working."""
    bot = _bot("b", realized=D("12.40"), fees="0.03", position_cost=100, collateral=40, pending=25)
    answer = _answer(_account(bot, cash=2000), need=None)

    assert [(term.label, term.amount_cents) for term in answer.free_budget.terms] == [
        ("Budget set aside at deploy", 100_000),
        ("Realized gains and losses", 1_240),
        ("Fees", -3),
        ("Held positions at cost", -6_000),
        ("Collateral held against sold options", -4_000),
        ("Waiting in entry orders", -2_500),
    ]
    assert answer.free_budget.amount_cents == 100_000 + 1_240 - 3 - 10_000 - 2_500


def test_a_fractional_cent_adds_one_visible_rounding_term_so_the_terms_still_add_up() -> None:
    """Realized $0.006 less $0.001 of fees leaves $1,000.005 free; the figure floors to $1,000.00, and the
    terms, each rounded to the cent, would make $1,000.01 without the rounding term."""
    answer = _answer(_account(_bot("b", realized=D("0.006"), fees="0.001"), cash=2000), need=None)

    assert answer.free_budget.amount_cents == 100_000
    assert answer.free_budget.terms[-1] == Term("Rounding to whole cents", -1)


def test_a_share_entry_rounds_up_to_the_cent_once_and_its_parts_add_up() -> None:
    need = EntryNeed.for_shares(requirement=D("100.011"), fee_provision_cents=1)

    assert need.commitment_cents == 10_002
    assert [(term.label, term.amount_cents) for term in need.terms] == [("Shares at the ask", 10_001), ("Fee provision", 1)]


@pytest.mark.parametrize(
    ("realized", "free_cents", "admits", "shortfall", "fits"),
    [("0.0115", 10_001, False, 1, 0), ("0.0199", 10_001, False, 1, 0), ("0.02", 10_002, True, 0, 1)],
)
def test_an_entry_that_fits_a_limit_only_within_the_same_cent_is_refused_one_cent_short(
    realized: str, free_cents: int, admits: bool, shortfall: int, fits: int,
) -> None:
    """A share at $100.001 with a one-cent fee provision needs $100.011, $100.02 rounded up. $100.0115 or
    $100.0199 free is $100.01 rounded down: the exact comparison admitted both, the cent comparison refuses
    both, $0.01 short. At $100.02 free it admits."""
    need = EntryNeed.for_shares(requirement=D("100.011"), fee_provision_cents=1)
    answer = _answer(_account(_bot("b", committed=100, realized=D(realized)), cash=1000), need=need)

    assert need.commitment_cents == 10_002
    assert answer.free_budget.amount_cents == free_cents
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (admits, "bot_budget", shortfall, fits)
    if not admits:
        assert answer.sentence == (
            "This bot has only $100.01 free. The entry needs $100.02 including its fee provision. Shortfall: $0.01."
        )


def test_an_option_entry_as_admission_is_handed_it_splits_into_its_commitment_and_its_fee() -> None:
    need = EntryNeed.for_options(commitment_cents=19_300, fee_provision_cents=6)

    assert [(term.label, term.amount_cents) for term in need.terms] == [
        ("Broker requirement and net premium", 19_294), ("Fee provision", 6),
    ]


def test_a_figure_whose_terms_do_not_add_up_is_refused_rather_than_drawn() -> None:
    with pytest.raises(ValueError, match="terms add up"):
        Figure(100, "known", "Clerk books", NOON, (Term("a", 60), Term("b", 30)))
    with pytest.raises(ValueError, match="never zero"):
        Figure(0, "unknown", "Clerk books")
    with pytest.raises(ValueError, match="parts add up"):
        EntryNeed(100, (Term("a", 99),))

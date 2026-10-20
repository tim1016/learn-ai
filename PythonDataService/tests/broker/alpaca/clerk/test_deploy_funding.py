"""Whether a budget typed at Deploy can be set aside and fund one entry, through ``deploy_funding_answer`` alone (#3099).

Every figure is worked by hand from the rule: the entry is paid out of the
budget typed, the budget is set aside out of the account's unreserved cash,
and Alpaca's option buying power can bind but never changes what is admitted.
The known limit with the least room binds; a refusal is short by what the
limit that refused it lacks (the account must fund the whole budget).
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.budgets import AccountBudget, account_budget, deployment_budget
from app.broker.alpaca.clerk.funding import (
    EntryNeed,
    Figure,
    FundingAnswer,
    Term,
    deploy_funding_answer,
    option_buying_power_figure,
    requirement_effect,
)
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS
from tests.broker.alpaca.clerk.sqlite.conftest import NOON

_PASS_NOTE = "Passing our own limits does not guarantee that Alpaca accepts the order."


def _account(*, cash: object, claimed: int = 0, collateral: int = 0) -> AccountBudget:
    """An account, in whole dollars: ``claimed`` is another running bot's free budget, ``collateral`` is held
    against sold options by a stopped bot."""
    bots = []
    if claimed:
        bots.append(deployment_budget(
            strategy_instance_id="other", committed_cents=claimed * 100, active=True, realized_gross=0, fees=D(0),
            position_cost=D(0), pending_orders=D(0),
        ))
    if collateral:
        bots.append(deployment_budget(
            strategy_instance_id="holder", committed_cents=collateral * 100, active=False, realized_gross=0, fees=D(0),
            position_cost=D(collateral), pending_orders=D(0), option_collateral=D(collateral),
        ))
    return account_budget(cash=cash, deployments=bots, order_claims=D(0), fee_claims=D(0))


def _need(cents: int, *, fee: int = 0) -> EntryNeed:
    return EntryNeed.for_options(commitment_cents=cents, fee_provision_cents=fee)


def _broker(cents: int, *, as_of_ms: int = NOON) -> Figure:
    return Figure(cents, "known", "Alpaca's account answer", as_of_ms, (Term("Option buying power", cents),))


def _answer(
    account: AccountBudget | None, *, amount: int | None, need: EntryNeed | None = None, broker: Figure | None = None,
    observed_at_ms: int = NOON, now_ms: int = NOON, simulated: bool = False,
) -> FundingAnswer:
    answer = deploy_funding_answer(
        account, amount_cents=amount, need=need, broker_buying_power=broker,
        observed_at_ms=observed_at_ms, now_ms=now_ms, simulated=simulated,
    )
    for figure in (answer.free_budget, answer.account_headroom, answer.broker_buying_power, answer.budget_left, answer.headroom_left):
        if figure is not None and figure.amount_cents is not None:
            assert sum(term.amount_cents for term in figure.terms) == figure.amount_cents
    return answer


# ---- each limit refusing, and passing ----------------------------------------------------------


def test_a_budget_that_covers_one_entry_and_fits_the_account_is_admitted_and_counts_how_many_fit() -> None:
    """$1,000 typed on an account with $5,000: one $800 entry fits the budget, and the budget is the least room."""
    answer = _answer(_account(cash=5000), amount=100_000, need=_need(80_000, fee=25))

    assert (answer.free_budget.amount_cents, answer.account_headroom.amount_cents) == (100_000, 500_000)
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (True, "bot_budget", 0, 1)
    assert answer.sentence == (
        "This budget is $1,000.00 and the account has $5,000.00 unreserved, "
        f"enough for the entry's $800.00 including its fee provision. {_PASS_NOTE}"
    )
    assert answer.action is None
    assert answer.fits_sentence == (
        "One whole entry fits right now under the bot's budget, the limit with the least room. "
        "Every bot competes for the same account headroom, so these counts are never added up across bots."
    )


def test_a_budget_under_one_entry_is_refused_by_the_budget_and_says_what_to_choose() -> None:
    answer = _answer(_account(cash=5000), amount=50_000, need=_need(80_000, fee=25))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "bot_budget", 30_000, 0)
    assert answer.sentence == (
        "This budget is only $500.00. The entry needs $800.00 including its fee provision. Shortfall: $300.00."
    )
    assert answer.action == "Choose a budget of at least $800.00."


def test_a_budget_over_what_the_account_has_unreserved_is_refused_by_the_account_and_short_by_the_budget() -> None:
    """Cash $1,000 with another bot's $300 set aside: $700 unreserved. A $900 budget needs $200 more cash."""
    answer = _answer(_account(cash=1000, claimed=300), amount=90_000, need=_need(80_000))

    assert answer.account_headroom.amount_cents == 70_000
    assert [(term.label, term.amount_cents) for term in answer.account_headroom.terms] == [
        ("Cash in the account", 100_000), ("Set aside for running bots", -30_000),
        ("Held in entry orders", 0), ("Fees not posted yet", 0),
    ]
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "account_headroom", 20_000, 0)
    assert answer.sentence == "This budget is $900.00, but the account has only $700.00 unreserved. Shortfall: $200.00."
    assert answer.action == "Choose a smaller budget, free another bot's budget, or add cash to the account."


def test_both_limits_short_name_the_budget_and_the_account_s_figure() -> None:
    """A $500 budget and $100 unreserved, against an $800 entry: the account is the least room, short by $700."""
    answer = _answer(_account(cash=1000, claimed=900), amount=50_000, need=_need(80_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "account_headroom", 70_000)
    assert answer.sentence == (
        "This budget is $500.00. The account has only $100.00 unreserved. "
        "The entry needs $800.00 including its fee provision. Shortfall: $700.00."
    )


def test_collateral_held_against_sold_options_is_off_what_the_account_can_fund() -> None:
    """Cash $1,500 with $1,000 of collateral held by a stopped bot: $500 unreserved, and the terms say why."""
    answer = _answer(_account(cash=1500, collateral=1000), amount=80_000, need=_need(80_000))

    assert answer.account_headroom.amount_cents == 50_000
    assert ("Collateral held against sold options", -100_000) in [
        (term.label, term.amount_cents) for term in answer.account_headroom.terms
    ]
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "account_headroom", 30_000)


@pytest.mark.parametrize(
    ("amount", "unreserved", "need", "admits", "shortfall", "fits"),
    [
        (80_000, 80_000, 80_000, True, 0, 1),
        (80_001, 80_000, 80_000, False, 1, 1),
        (79_999, 90_000, 80_000, False, 1, 0),
        (80_000, 90_000, 80_000, True, 0, 1),
        (100_000, 100_000, 40_000, True, 0, 2),
        (79_999, 79_999, 40_000, True, 0, 1),
    ],
)
def test_every_comparison_is_exact_to_the_cent_and_one_cent_under_a_whole_entry_fits_one_fewer(
    amount: int, unreserved: int, need: int, admits: bool, shortfall: int, fits: int,
) -> None:
    answer = _answer(_account(cash=D(unreserved) / 100), amount=amount, need=_need(need))

    assert (answer.admits, answer.shortfall_cents, answer.fits) == (admits, shortfall, fits)


def test_a_fractional_cent_of_unreserved_cash_rounds_down_because_it_is_a_limit() -> None:
    """$800.009 of cash is $800.00 of room: a budget of $800.01 is a cent over it."""
    answer = _answer(_account(cash=D("800.009")), amount=80_001, need=_need(80_000))

    assert answer.account_headroom.amount_cents == 80_000
    assert [(term.label, term.amount_cents) for term in answer.account_headroom.terms][-1] == ("Rounding to whole cents", -1)
    assert (answer.admits, answer.shortfall_cents) == (False, 1)


# ---- Alpaca's option buying power ---------------------------------------------------------------------


def test_alpacas_buying_power_can_bind_and_never_changes_what_is_admitted() -> None:
    """$1,000 typed, $5,000 unreserved; Alpaca's option buying power $500 against an $800 entry.

    Our own limits pass, so ``admits`` is true; Alpaca's figure is the least room and falls $300 short. That is
    the case the Deploy stops on (the buying-power blocker is this verdict), and it has no action of ours."""
    answer = _answer(_account(cash=5000), amount=100_000, need=_need(80_000), broker=_broker(50_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (True, "broker_buying_power", 30_000, 0), (
        "buying power binds, $300 short, and the entry fits none, yet the entry is admitted"
    )
    assert answer.sentence == (
        "This budget is $1,000.00 and the account has $5,000.00 unreserved, "
        "enough for the entry's $800.00 including its fee provision. "
        "Alpaca's option buying power is only $500.00, so Alpaca would likely refuse the order. Shortfall: $300.00. "
        f"{_PASS_NOTE}"
    ), "the sentence says Alpaca would likely refuse and that passing our limits promises nothing"
    assert answer.fits_sentence is not None and "Alpaca's option buying power" in answer.fits_sentence, (
        "the fit count names Alpaca's option buying power"
    )
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.action) == (
        True, "broker_buying_power", 30_000, None,
    ), "where buying power is short and nothing else is, the answer admits, buying power binds, and we have no action"
    assert answer.fits_sentence is not None and "the limit with the least room" in answer.fits_sentence, (
        "the fit count says buying power is the limit with the least room"
    )


def test_one_cent_of_buying_power_under_the_entry_binds_and_the_entry_fits_none() -> None:
    short = _answer(_account(cash=5000), amount=100_000, need=_need(82_409), broker=_broker(82_408))
    enough = _answer(_account(cash=5000), amount=100_000, need=_need(82_409), broker=_broker(82_409))

    assert (short.binding, short.shortfall_cents, short.fits) == ("broker_buying_power", 1, 0)
    assert (enough.binding, enough.shortfall_cents, enough.fits) == ("broker_buying_power", 0, 1)
    assert short.admits is True and enough.admits is True


def test_buying_power_does_not_rescue_a_refusal_our_own_limits_make() -> None:
    answer = _answer(_account(cash=5000), amount=50_000, need=_need(80_000), broker=_broker(1_000_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", 30_000)


def test_a_refusal_is_bound_by_the_limit_that_makes_it_and_not_by_a_smaller_figure_that_covers_the_entry() -> None:
    """A $100 entry, a $1,000 budget, $900 unreserved and $500 of buying power.

    Buying power is the smallest figure and covers the entry. The refusal is the account's: it cannot fund the
    whole $1,000 budget it would set aside, and is $100 short of it. Naming buying power would list figures that
    all cover the entry and offer no remedy."""
    answer = _answer(_account(cash=900), amount=100_000, need=_need(10_000), broker=_broker(50_000))

    assert (answer.free_budget.amount_cents, answer.account_headroom.amount_cents) == (100_000, 90_000)
    assert answer.broker_buying_power is not None and answer.broker_buying_power.amount_cents == 50_000
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "account_headroom", 10_000)
    assert answer.sentence == "This budget is $1,000.00, but the account has only $900.00 unreserved. Shortfall: $100.00."
    assert answer.action == "Choose a smaller budget, free another bot's budget, or add cash to the account."
    # How many fit is counted under the limit that refuses, and says so.
    assert answer.fits == 9
    assert answer.fits_sentence is not None
    assert answer.fits_sentence.startswith(
        "9 whole entries fit right now under the account's headroom, the limit that refuses this budget."
    )


def test_the_refusal_reads_the_same_with_no_buying_power_figure_as_the_clerks_recheck_gives_it() -> None:
    """The Clerk's re-check is handed no buying power. Where Alpaca's figure covers the entry it changes nothing
    of the sentence, so the review and the commit say one thing."""
    with_buying_power = _answer(_account(cash=900), amount=100_000, need=_need(10_000), broker=_broker(50_000))
    without = _answer(_account(cash=900), amount=100_000, need=_need(10_000), broker=None)

    assert with_buying_power.sentence == without.sentence
    assert (without.binding, without.shortfall_cents, without.action) == (
        "account_headroom", 10_000, with_buying_power.action,
    )


def test_a_budget_that_refuses_is_bound_by_the_budget_though_buying_power_is_smaller() -> None:
    """A $500 budget against an $800 entry, $5,000 unreserved and $100 of buying power.

    The budget is what refuses, short by $300. Buying power is the least room and is short too, and is said to be,
    but it never refuses and is not what the refusal is bound by."""
    answer = _answer(_account(cash=5000), amount=50_000, need=_need(80_000), broker=_broker(10_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", 30_000)
    assert answer.sentence == (
        "This budget is only $500.00. The entry needs $800.00 including its fee provision. Shortfall: $300.00. "
        "Alpaca's option buying power is only $100.00, which would not cover the entry either."
    )
    assert answer.action == "Choose a budget of at least $800.00."


@pytest.mark.parametrize(
    ("cash", "broker_cents", "binding", "shortfall", "action"),
    [
        # $500 budget, $800 entry, $100 unreserved: the budget is $300 short and the account $700 short.
        pytest.param(
            100, 1_000_000, "account_headroom", 70_000,
            "Choose a smaller budget, free another bot's budget, or add cash to the account.",
            id="both-refuse-the-one-short-by-more-is-named",
        ),
        # $500 budget, $800 entry, $500 unreserved: $300 short each. The budget is named first, as it always was.
        pytest.param(
            500, 10_000, "bot_budget", 30_000, "Choose a budget of at least $800.00.",
            id="short-by-the-same-the-budget-is-named",
        ),
    ],
)
def test_where_the_budget_and_the_account_both_refuse_the_one_short_by_more_is_named(
    cash: int, broker_cents: int, binding: str, shortfall: int, action: str,
) -> None:
    answer = _answer(_account(cash=cash), amount=50_000, need=_need(80_000), broker=_broker(broker_cents))

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, binding, shortfall), (
        "the refusal is bound by the limit that is short by more, a tie going to the budget"
    )
    assert answer.action == action, "the action is the remedy of the limit that binds"


def test_a_budget_over_the_account_is_still_bound_by_the_account_when_the_account_is_not_the_smallest_figure() -> None:
    """The mirror of the first: the budget covers its entry and is the smaller raw figure beside buying power."""
    answer = _answer(_account(cash=900), amount=100_000, need=_need(10_000), broker=_broker(2_000_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "account_headroom", 10_000, 9)


def test_a_buying_power_reading_older_than_the_limit_is_stale_with_its_time_and_never_a_pass() -> None:
    """Read 46 seconds ago: shown as stale at that time, counted nowhere, and the entry is not called covered."""
    read_at = NOON - OBSERVATION_MAX_AGE_MS - 1
    answer = _answer(
        _account(cash=5000), amount=100_000, need=_need(80_000),
        broker=option_buying_power_figure(D("1"), read_at_ms=read_at),
    )

    reading = answer.broker_buying_power
    assert reading is not None
    assert (reading.state, reading.as_of_ms, reading.amount_cents) == ("stale", read_at, 100)
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (True, "bot_budget", 0)
    assert "option buying power" not in answer.sentence


def test_a_buying_power_reading_at_the_limit_is_still_current() -> None:
    at_the_limit = option_buying_power_figure(D("1"), read_at_ms=NOON - OBSERVATION_MAX_AGE_MS)
    answer = _answer(_account(cash=5000), amount=100_000, need=_need(80_000), broker=at_the_limit)

    assert answer.broker_buying_power is not None and answer.broker_buying_power.state == "known"
    assert answer.binding == "broker_buying_power"


def test_buying_power_alpaca_did_not_carry_is_unknown_with_the_time_it_answered_never_zero() -> None:
    figure = option_buying_power_figure(None, read_at_ms=NOON)
    answer = _answer(_account(cash=5000), amount=100_000, need=_need(80_000), broker=figure)

    assert (figure.state, figure.amount_cents, figure.as_of_ms, figure.terms) == ("unknown", None, NOON, ())
    assert "did not carry option buying power" in figure.source
    assert (answer.admits, answer.binding) == (True, "bot_budget")


def test_buying_power_rounds_down_to_the_cent_and_keeps_its_sign() -> None:
    assert option_buying_power_figure(D("824.089"), read_at_ms=NOON).amount_cents == 82_408
    assert option_buying_power_figure(D("-5"), read_at_ms=NOON).amount_cents == -500


def test_with_no_account_read_only_alpacas_own_limit_is_judged() -> None:
    """What the buying-power check of the option review asks: the figure and the entry, nothing of the account."""
    short = _answer(None, amount=None, need=_need(82_409), broker=_broker(82_408))
    enough = _answer(None, amount=None, need=_need(82_409), broker=_broker(82_409))
    negative = _answer(None, amount=None, need=_need(82_409), broker=_broker(-500))
    unknown = _answer(None, amount=None, need=_need(82_409), broker=option_buying_power_figure(None, read_at_ms=NOON))

    assert (short.binding, short.shortfall_cents, short.admits) == ("broker_buying_power", 1, None)
    assert short.sentence == (
        "Alpaca's option buying power is $824.08, but the entry needs $824.09 including its fee provision. "
        "Shortfall: $0.01."
    )
    assert (enough.binding, enough.shortfall_cents) == ("broker_buying_power", 0)
    assert negative.shortfall_cents == 82_909 and "is -$5.00" in negative.sentence
    assert (unknown.binding, unknown.shortfall_cents, unknown.fits) == (None, None, None)
    assert unknown.sentence == "This account's money cannot be read right now, so what it can fund is unknown."
    # How many fit is the account's to say: Alpaca's figure alone does not count it.
    assert (short.fits, enough.fits, short.fits_sentence, enough.fits_sentence) == (None, None, None, None)
    assert "did not read" in short.account_headroom.source


# ---- no amount typed yet ---------------------------------------------------------------------------


def test_with_no_amount_typed_the_account_says_how_many_entries_it_could_fund() -> None:
    answer = _answer(_account(cash=5000), amount=None, need=_need(80_000))

    assert answer.free_budget.state == "unknown" and answer.free_budget.amount_cents is None
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (None, "account_headroom", 0, 6)
    assert answer.budget_left is None and answer.headroom_left is not None
    assert answer.headroom_left.amount_cents == 420_000
    assert answer.sentence == (
        "The account has $5,000.00 unreserved. The entry needs $800.00 including its fee provision."
    )


def test_with_no_amount_typed_an_account_that_cannot_fund_one_entry_says_so() -> None:
    answer = _answer(_account(cash=700), amount=None, need=_need(80_000))

    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (False, "account_headroom", 10_000, 0)
    assert answer.sentence == (
        "The account has only $700.00 unreserved. The entry needs $800.00 including its fee provision. "
        "Shortfall: $100.00."
    )


def test_with_no_entry_priced_a_budget_over_the_account_is_still_refused() -> None:
    """The Clerk re-checks at the commit with no price for an option bot: an amount over the cash is refused."""
    over = _answer(_account(cash=1000), amount=100_001)
    at = _answer(_account(cash=1000), amount=100_000)

    assert (over.admits, over.binding, over.shortfall_cents) == (False, "account_headroom", None)
    assert over.sentence == "This budget is $1,000.01, but the account has only $1,000.00 unreserved."
    assert (at.admits, at.need) == (None, None)
    assert at.sentence == "This budget is $1,000.00. The account has $1,000.00 unreserved."


# ---- the account stale, over-committed or unread -------------------------------------------------------


def test_an_account_reading_past_its_limit_is_stale_and_the_verdict_is_unsaid() -> None:
    account = _account(cash=5000)

    at_the_limit = _answer(account, amount=100_000, need=_need(80_000), observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS)
    past_it = _answer(account, amount=100_000, need=_need(80_000), observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS - 1)

    assert (at_the_limit.account_headroom.state, at_the_limit.admits) == ("known", True)
    assert past_it.account_headroom.state == "stale" and past_it.account_headroom.amount_cents == 500_000
    assert (past_it.admits, past_it.shortfall_cents, past_it.fits) == (None, None, None)
    assert past_it.headroom_left is not None and past_it.headroom_left.state == "stale"
    assert past_it.headroom_left.as_of_ms == NOON - OBSERVATION_MAX_AGE_MS - 1
    assert past_it.sentence == (
        "This budget is $1,000.00. The account's unreserved cash was last read more than 45 seconds ago, "
        "so it is not counted here. Whether the entry fits is not said. "
        "The entry needs $800.00 including its fee provision."
    )


def test_a_budget_under_one_entry_is_refused_even_while_the_account_reading_is_stale() -> None:
    answer = _answer(
        _account(cash=5000), amount=50_000, need=_need(80_000), observed_at_ms=NOON - OBSERVATION_MAX_AGE_MS - 1,
    )

    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "bot_budget", 30_000)
    assert answer.sentence.endswith("so it is not counted here.")


def test_an_account_over_committed_by_other_bots_names_what_it_is_over_by() -> None:
    """Cash $100 with $300 set aside for other bots: unreserved -$200."""
    answer = _answer(_account(cash=100, claimed=300), amount=10_000, need=_need(8_000))

    assert answer.account_headroom.amount_cents == -20_000
    assert (answer.admits, answer.binding, answer.shortfall_cents) == (False, "account_headroom", 30_000)
    assert answer.sentence == (
        "This budget is $100.00, but the account is $200.00 over-committed, so nothing is unreserved. "
        "Shortfall: $300.00."
    )


def test_an_unread_account_leaves_its_figure_unknown_never_zero() -> None:
    answer = _answer(None, amount=100_000, need=_need(80_000))

    assert (answer.account_headroom.state, answer.account_headroom.amount_cents) == ("unknown", None)
    assert (answer.admits, answer.binding, answer.shortfall_cents, answer.fits) == (None, "bot_budget", None, None)
    assert answer.sentence == "This account's money cannot be read right now, so what it can fund is unknown."


# ---- what is left after one entry ---------------------------------------------------------------------


def test_what_each_limit_leaves_after_one_entry_is_its_figure_less_the_entry_with_terms() -> None:
    answer = _answer(_account(cash=5000), amount=100_000, need=_need(82_409, fee=9))

    assert answer.budget_left is not None and answer.headroom_left is not None
    assert (answer.budget_left.amount_cents, answer.budget_left.state) == (17_591, "known")
    assert [(term.label, term.amount_cents) for term in answer.budget_left.terms] == [
        ("Budget typed in Deploy", 100_000), ("One entry", -82_409),
    ]
    assert (answer.headroom_left.amount_cents, answer.headroom_left.state) == (417_591, "known")


def test_a_limit_that_leaves_less_than_nothing_says_so_in_a_negative_figure() -> None:
    answer = _answer(_account(cash=5000), amount=50_000, need=_need(80_000))

    assert answer.budget_left is not None and answer.budget_left.amount_cents == -30_000


# ---- a Dry Run ---------------------------------------------------------------------------------------------


def test_a_dry_run_has_no_account_limit_its_cash_is_the_amount_typed_and_nothing_is_said_of_an_account() -> None:
    passes = _answer(None, amount=100_000, need=_need(82_409), simulated=True)
    short = _answer(None, amount=82_408, need=_need(82_409), simulated=True)

    assert passes.account_headroom.amount_cents == 100_000, "a dry run's account headroom is the amount typed"
    assert "simulated cash" in passes.account_headroom.source, "the headroom says it is simulated cash"
    assert (passes.admits, passes.binding, passes.fits) == (True, "bot_budget", 1), (
        "a budget that covers one entry is admitted and the budget binds"
    )
    assert passes.sentence == (
        f"This budget is $1,000.00, enough for the entry's $824.09 including its fee provision. {_PASS_NOTE}"
    ), "the passing sentence names the budget alone"
    assert (short.admits, short.binding, short.shortfall_cents) == (False, "bot_budget", 1), (
        "a budget one cent under the entry is refused by the budget"
    )
    assert short.sentence == (
        "This budget is only $824.08. The entry needs $824.09 including its fee provision. Shortfall: $0.01."
    ), "the refusing sentence names the budget alone"
    assert "account" not in short.sentence, "the refusing sentence says nothing of an account"
    # A dry run's cash is its own, so nothing is said of an account or of other bots.
    assert passes.simulated is True and _answer(_account(cash=5000), amount=100_000).simulated is False, (
        "only a dry run is flagged simulated"
    )
    assert passes.fits_sentence == "One whole entry fits right now with this budget as its starting cash.", (
        "the fit count is said with the budget as starting cash"
    )
    assert "compete" not in (passes.fits_sentence or "") and "account" not in (passes.fits_sentence or ""), (
        "the fit count says nothing of other bots or an account"
    )
    assert passes.headroom_left is not None, "a dry run states what its cash leaves after one entry"
    assert [(term.label, term.amount_cents) for term in passes.headroom_left.terms] == [
        ("Simulated starting cash", 100_000), ("One entry", -82_409),
    ], "what is left is the simulated starting cash less one entry"


# ---- what one more entry does to the account's requirement -----------------------------------------------------


def test_on_a_flat_account_the_requirement_change_is_the_entrys_own_and_nothing_nets() -> None:
    """A $5-wide bear call spread: $500.00 of requirement alone and $500.00 added to a flat account."""
    effect = requirement_effect(
        before_after_cents=(0, 50_000), why_not="", entry_requirement_cents=50_000, as_of_ms=NOON,
    )

    assert (effect.change.amount_cents, effect.change.state, effect.change.as_of_ms) == (50_000, "known", NOON)
    assert [(term.label, term.amount_cents) for term in effect.change.terms] == [
        ("Account requirement once this structure is held", 50_000), ("Account requirement now", 0),
    ]
    assert effect.netting is None


def test_an_offsetting_position_makes_the_change_smaller_and_the_bot_does_not_spend_the_saving() -> None:
    """The account holds the opposite spread: held together the two need nothing, so this adds nothing."""
    effect = requirement_effect(
        before_after_cents=(50_000, 0), why_not="", entry_requirement_cents=50_000, as_of_ms=NOON,
    )

    assert effect.change.amount_cents == -50_000
    assert effect.netting == (
        "Alpaca values the account's positions together, so the account's requirement would fall by $500.00, "
        "though this structure requires $500.00 alone. The bot still sets aside $500.00: "
        "it does not spend the $1,000.00 that netting saves."
    )


def test_a_change_between_nothing_and_the_entrys_own_says_it_would_rise_by_only_that() -> None:
    effect = requirement_effect(
        before_after_cents=(20_000, 50_000), why_not="", entry_requirement_cents=50_000, as_of_ms=NOON,
    )

    assert effect.change.amount_cents == 30_000
    assert effect.netting is not None and "would rise by only $300.00, though this structure requires $500.00 alone" in effect.netting
    assert "does not spend the $200.00 that netting saves" in effect.netting


def test_a_requirement_that_cannot_be_worked_out_is_unknown_and_says_why() -> None:
    effect = requirement_effect(
        before_after_cents=None, why_not="The structure sells more calls than it buys.", entry_requirement_cents=0,
        as_of_ms=NOON,
    )

    assert (effect.change.state, effect.change.amount_cents, effect.change.terms) == ("unknown", None, ())
    assert "The structure sells more calls than it buys." in effect.change.source
    assert effect.netting is None


# ---- inputs --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [True, 1.5, "100"])
def test_a_budget_that_is_not_whole_cents_is_refused_not_rounded(amount: object) -> None:
    with pytest.raises(ValueError, match="whole number of cents"):
        deploy_funding_answer(
            _account(cash=1000), amount_cents=amount, need=None,  # type: ignore[arg-type]
            broker_buying_power=None, observed_at_ms=NOON, now_ms=NOON,
        )

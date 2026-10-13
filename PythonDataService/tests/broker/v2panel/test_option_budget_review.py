"""The budget of a bot that trades options is reviewed against what the structure preview prices one entry at (#2913).

The real money review on a real Paper Clerk's books. An option contract has
no top of book, so the caller hands over the price one entry sets aside; a
bot in shares is priced from its asset's ask exactly as before.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.schemas.deployment_budget import DeploymentBudgetInput
from app.services.broker_v2_panel import budget_deploy
from app.services.broker_v2_panel.budget_deploy import OptionEntryPrice
from tests.broker.v2panel.test_budget_deploy import (
    NOON,
    _request,
    authority,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.v2panel.test_deploy_option_structure import _ONE_CALL, _TERMS

_ACCOUNT = "BUDGET-PAPER"
# One SPY call offered at $1.94, with a ten-cent fee estimate.
_PRICED = OptionEntryPrice(commitment_cents=19_410, observed_at_ms=NOON - 400, waiting=False, detail="")


def _options(**updates):
    return _request(trades=_ONE_CALL, **updates)


def _review(request, entry: OptionEntryPrice | None = _PRICED):
    return budget_deploy.preview_budget(_ACCOUNT, request, resolved_parameters={}, option_entry=entry)


def test_the_minimum_budget_of_an_option_bot_is_what_one_entry_sets_aside_right_now(authority) -> None:  # noqa: F811
    preview = _review(_options())

    assert (preview.state, preview.minimum_budget_usd, preview.estimated_price_usd) == ("ready", "194.10", None)
    assert preview.observed_at_ms == NOON, "the review of a real account is as old as its account reading"
    headroom = next(shortcut for shortcut in preview.shortcuts if shortcut.key == "position_headroom")
    assert headroom.amount_usd == "232.92"
    assert "one entry sets aside" in headroom.explanation and "position" not in headroom.explanation
    assert preview.review_token is None, "no amount was reviewed yet"


def test_a_budget_under_one_entry_is_refused_in_an_entrys_words_and_one_that_covers_it_can_be_consented_to(
    authority,  # noqa: F811
) -> None:
    too_little = _review(_options(budget=DeploymentBudgetInput(amount_usd="194.09", risk_revision=1)))
    covers = _options(budget=DeploymentBudgetInput(amount_usd="194.10", risk_revision=1))
    reviewed = _review(covers)

    assert too_little.state == "unavailable" and too_little.review_token is None
    assert too_little.detail == (
        "This budget is only $194.09. The entry needs $194.10 including its fee provision. Shortfall: $0.01."
    )
    assert too_little.funding is not None and too_little.funding.action == "Choose a budget of at least $194.10."
    assert reviewed.state == "ready" and reviewed.review_token is not None
    accepted = covers.model_copy(update={"budget": covers.budget.model_copy(update={"review_token": reviewed.review_token})})
    consent = budget_deploy.resolve_consent(_ACCOUNT, accepted, resolved_parameters={}, option_entry=_PRICED)
    assert (consent.committed_cents, consent.world) == (19_410, "real_paper")


def test_a_review_of_one_set_of_order_terms_is_no_review_of_another(authority) -> None:  # noqa: F811
    budget = DeploymentBudgetInput(amount_usd="600.00", risk_revision=1)
    reviewed = _review(_options(budget=budget)).review_token
    longer_wait = _request(
        trades={**_ONE_CALL, "order_terms": {**_TERMS, "entry_wait_seconds": 25}},
        budget=budget.model_copy(update={"review_token": reviewed}),
    )

    with pytest.raises(BudgetUnavailable, match="Review this exact budget"):
        budget_deploy.resolve_consent(_ACCOUNT, longer_wait, resolved_parameters={}, option_entry=_PRICED)


@pytest.mark.parametrize(
    ("entry", "state", "said"),
    [
        (OptionEntryPrice(None, NOON, True, "Waiting to price this structure. Quotes are arriving."), "awaiting_price", "arriving"),
        (OptionEntryPrice(None, NOON, False, "This account is approved for covered calls only."), "unavailable", "approved"),
        (None, "unavailable", "has not been priced"),
    ],
    ids=["still arriving", "something stops it", "nobody priced it"],
)
def test_an_option_entry_with_no_price_has_no_review_to_consent_to(
    authority, entry: OptionEntryPrice | None, state: str, said: str  # noqa: F811
) -> None:
    request = _options(budget=DeploymentBudgetInput(amount_usd="600.00", risk_revision=1))

    preview = _review(request, entry)

    assert (preview.state, preview.review_token, preview.minimum_budget_usd) == (state, None, None)
    assert said in preview.detail
    with pytest.raises(BudgetUnavailable):
        budget_deploy.resolve_consent(_ACCOUNT, request, resolved_parameters={}, option_entry=entry)


def test_a_dry_run_of_options_is_priced_the_same_way_from_private_simulated_cash(authority) -> None:  # noqa: F811
    preview = _review(_options(execution_mode="dry_run", budget=DeploymentBudgetInput(amount_usd="500.00", risk_revision=0)))

    assert (preview.state, preview.world, preview.minimum_budget_usd) == ("ready", "synthetic", "194.10")
    assert preview.observed_at_ms == _PRICED.observed_at_ms, "a Dry Run's review is as old as the quotes it was priced from"
    assert preview.money_after is None and preview.review_token is not None


def test_a_bot_in_shares_is_priced_from_its_assets_ask_whatever_option_price_is_handed_over(authority) -> None:  # noqa: F811
    """The option price is read for a bot that trades options, and for no other."""
    plain = budget_deploy.preview_budget(_ACCOUNT, _request(), resolved_parameters={})
    handed_a_price = _review(_request())

    assert plain == handed_a_price
    assert plain.estimated_price_usd is not None and plain.minimum_budget_usd != "194.10"

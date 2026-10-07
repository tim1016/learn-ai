"""A Paper account goes on trading after its first option fill, and what its fees then read as (#2913).

Alpaca reports no fee at the fill, and none of its own fee activities for an
option has been seen. So a real account's option fill is charged by the
option fee model, as an estimate, until a fee row of its day is seen. That
row is then shared among the day's fills, and it stops no bot.
"""

from __future__ import annotations

from decimal import Decimal

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.contract.models import BrokerActivity
from app.services.alpaca_fee_reconciliation import read_fee_view
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)

_DAY_MS = 86_400_000


async def _a_holds_one_call(paper: PaperOptions) -> str:  # noqa: F811
    order_ref = await paper.resting("a")
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    assert paper.outcome(order_ref).standing is EntryStanding.FILLED
    return order_ref


async def _the_next_day_alpaca_has_posted(paper: PaperOptions, dollars: str) -> None:  # noqa: F811
    """A day passes and the Clerk reads Alpaca's activities: one fee row, dated the day bot A's call filled."""
    filled_on = et_date_at_ms(paper.clock())
    paper.wait(_DAY_MS)
    publish_quotes(paper.clock(), SPY=585.0)
    paper.account.account_observed_at_ms = paper.clock()
    posted = BrokerActivity(
        broker="alpaca", activity_id="fee-1", activity_type="FEE", category="non_trade_activity", symbol=None,
        side=None, quantity=None, price=None, net_amount=-float(dollars),
        occurred_at_ms=et_midnight_ms(filled_on), observed_at_ms=paper.clock(),
    )
    record_fee_evidence(paper.repo, [posted], checked_at_ms=paper.clock(), history_complete=True)


async def test_a_paper_account_still_has_a_budget_after_its_first_option_fill_and_another_bot_enters(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _a_holds_one_call(paper)

    fees = paper.repo.fee_attribution(now_ms=paper.clock())
    # The account is read again after the fill, as its own cadence reads it; then bot B decides.
    assert await paper.sync.tick() == "observed"
    entered = await paper.enter("b")

    assert fees.known
    # One contract bought: CAT, ORF and OCC, each rounded up to the cent, 1 + 2 + 3.
    assert fees.total_for(bot_subject_id("a")) == Decimal("0.06")
    assert {share.state for share in fees.shares} == {"estimated"}
    assert entered.state is EffectOperationState.SUBMITTED, entered.explanation
    assert paper.held("a") == {SYMBOL: 1.0}


async def test_the_fees_card_shows_an_option_fills_fees_as_an_estimate_while_no_fee_row_of_its_day_is_seen(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _a_holds_one_call(paper)

    view, _projection = read_fee_view(paper.clerk, period=None)

    [row] = [row for row in view.rows if row.strategy_instance_id == "a"]
    assert (row.estimated_usd, row.modelled_settled_usd, row.observed_usd, row.total_usd) == (
        "0.06", "0", "0", "0.06",
    )
    assert view.known and view.messages == []


async def test_a_fee_row_for_a_day_with_an_option_fill_is_charged_to_its_bot_and_stops_no_bots_entry(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _a_holds_one_call(paper)
    await _the_next_day_alpaca_has_posted(paper, "0.05")

    fees = paper.repo.fee_attribution(now_ms=paper.clock())
    assert await paper.sync.tick() == "observed"
    entered = await paper.enter("b")

    # Bot A's was the day's only fill, so the row is all its own and takes its estimate's place.
    assert fees.known and fees.unattributed == 0
    assert [(share.subject_id, share.amount, share.state) for share in fees.shares] == [
        (bot_subject_id("a"), Decimal("0.05"), "observed"),
    ]
    assert entered.state is EffectOperationState.SUBMITTED, entered.explanation
    # The Fees card says the same: nothing is the account's alone, and no message asks for anything.
    view, _projection = read_fee_view(paper.clerk, period=None)
    [row] = [row for row in view.rows if row.strategy_instance_id == "a"]
    assert (row.estimated_usd, row.observed_usd, row.total_usd) == ("0", "0.05", "0.05")
    assert (view.known, view.account_unattributed_usd, view.messages) == (True, "0", [])

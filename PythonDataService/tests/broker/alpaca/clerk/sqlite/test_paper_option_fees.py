"""A Paper account goes on trading after its first option fill, and what its fees then read as (#2913).

Alpaca reports no fee at the fill, and none of its own fee activities for an
option has been seen. So a real account's option fills are charged by the
option fee model, as an estimate that stays an estimate, and no fee row of a
day with an option fill is shared out among the bots.
"""

from __future__ import annotations

from decimal import Decimal

from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.fee_evidence import OPTION_DAY_FEE_NOT_ATTRIBUTED, record_fee_evidence
from app.broker.contract.models import BrokerActivity
from app.services.alpaca_fee_reconciliation import read_fee_view
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)


async def _a_holds_one_call(paper: PaperOptions) -> str:  # noqa: F811
    order_ref = await paper.resting("a")
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    assert paper.outcome(order_ref).standing is EntryStanding.FILLED
    return order_ref


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


async def test_the_fees_card_shows_an_option_fills_fees_as_an_estimate_and_never_as_observed(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _a_holds_one_call(paper)

    view, _projection = read_fee_view(paper.clerk, period=None)

    [row] = [row for row in view.rows if row.strategy_instance_id == "a"]
    assert (row.estimated_usd, row.modelled_settled_usd, row.observed_usd, row.total_usd) == (
        "0.06", "0", "0", "0.06",
    )
    assert view.known and view.messages == []


async def test_a_fee_row_for_a_day_with_an_option_fill_stops_new_entries_on_the_account_by_name(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _a_holds_one_call(paper)
    posted = BrokerActivity(
        broker="alpaca", activity_id="fee-1", activity_type="FEE", category="non_trade_activity", symbol=None,
        side=None, quantity=None, price=None, net_amount=-0.05,
        occurred_at_ms=et_midnight_ms(et_date_at_ms(paper.clock())), observed_at_ms=paper.clock(),
    )
    record_fee_evidence(paper.repo, [posted], checked_at_ms=paper.clock(), history_complete=True)

    fees = paper.repo.fee_attribution(now_ms=paper.clock())
    await paper.sync.tick()
    refused = await paper.enter("b")

    assert fees.unresolved == (f"Fee fee-1 {OPTION_DAY_FEE_NOT_ATTRIBUTED}",)
    assert fees.unattributed == Decimal("0.05")
    # Bot A's fee is still the model's estimate: the row was charged to no bot.
    assert fees.total_for(bot_subject_id("a")) == Decimal("0.06")
    # No bot on the account is given a new entry while a charge is nobody's, and the Fees card says which.
    assert refused.state is EffectOperationState.REJECTED
    assert paper.venue.option_submit_calls == [next(iter(paper.venue.option_orders))]
    view, _projection = read_fee_view(paper.clerk, period=None)
    assert (view.known, view.account_unattributed_usd) == (False, "0.05")
    assert view.messages == [f"Fee fee-1 {OPTION_DAY_FEE_NOT_ATTRIBUTED}"]

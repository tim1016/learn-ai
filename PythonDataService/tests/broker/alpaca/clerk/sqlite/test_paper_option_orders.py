"""A Paper account's bot that trades one bought option: what is sent to Alpaca, and what the Clerk makes of its answers (#2913).

The venue here is a double that keeps an option order in the shape Alpaca's
documentation shows and answers through the adapter's own reader. No order
was sent to Alpaca, on Paper or anywhere, and no answer was recorded from it:
what these tests show is what the Clerk does with the documented shapes.
"""

from __future__ import annotations

import asyncio
import shutil
from decimal import Decimal

import pytest

from app.broker.alpaca import adapter as alpaca_adapter
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntrySettlement, EntryStanding, SettledEntry, entry_outcome
from app.broker.alpaca.clerk.sqlite.facts import EntrySettledFacts
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order, order_contract
from app.broker.alpaca.clerk.sqlite.option_order_evidence import (
    AskedOptionOrder,
    asked_option_order,
    evidence_of_the_order_asked,
    option_order_evidence,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerEvidenceUnavailable, BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.bot_binding_repository import BrokerBotBinding
from tests.broker.alpaca.clerk.sqlite.conftest import documented_option_listing
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    BUYS_ONE_CALL,
    CALL,
    SYMBOL,
    WAIT_MS,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
    patiently,
)
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    _DryRun,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.documented_examples import documented

ONE_CONTRACT_SET_ASIDE = 19_406


def _refused(receipt: EffectOperationReceipt, code: str) -> None:
    assert receipt.state is EffectOperationState.REJECTED, receipt.explanation
    assert receipt.explanation.startswith(f"{code}: "), receipt.explanation


# ── Paper takes one bought option ────────────────────────────────────────


async def test_a_paper_account_sends_one_bought_option_as_a_limit_order_that_rests(paper: PaperOptions) -> None:  # noqa: F811
    sent = await paper.enter()

    assert sent.state is EffectOperationState.SUBMITTED, sent.explanation
    [order_ref] = sent.child_order_refs
    # One order, to the option port, as the contract's own symbol: a limit at the ask, to open.
    assert paper.venue.submitted_option_orders == [
        SingleOptionOrderRequest(
            contract=CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, quantity=1,
            order_type=OrderType.LIMIT, limit_price=Decimal("1.94"),
        )
    ]
    assert paper.venue.option_submit_calls == [order_ref] and paper.venue.submitted_legs == []
    # It rests: accepted, working, nothing held, and the whole commitment set aside.
    assert paper.order_state(order_ref) == "new"
    assert paper.repo.order(order_ref).broker_order_id == paper.venue.option_orders[order_ref]["id"]
    assert order_contract(paper.repo, order_ref) == ContractTerms(symbol=SYMBOL, multiplier=100)
    assert paper.held() == {} and paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE
    assert paper.outcome(order_ref).standing is EntryStanding.WORKING
    # Alpaca's own listing of the contract was read once, before the entry was taken.
    assert paper.account.listing_reads == [SYMBOL]


async def test_a_paper_account_registers_a_bot_that_trades_one_bought_option_under_its_underlying(
    paper: PaperOptions,  # noqa: F811
) -> None:
    binding = BrokerBotBinding(
        strategy_instance_id="options-on-paper", strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="trade", quantity=1, action_plan=BUYS_ONE_CALL, run_id="run-1", created_at_ms=0,
        sealed_account_id=paper.repo.account_id, exit_terms=TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=30_000, risk_revision=1, actor="owner", request_fingerprint="reviewed", world="real_paper",
        ),
    )

    await paper.clerk.register_strategy_run(binding)

    registered = paper.repo.strategy_instance("options-on-paper")
    assert registered is not None and registered["symbol"] == "SPY"
    assert paper.repo.active_run("options-on-paper") is not None


# ── the contract is asked of Alpaca's own reference first ────────────────


@pytest.mark.parametrize(
    ("listing", "code"),
    [
        (None, "OPTION_CONTRACT_NOT_LISTED"),
        ({"status": "inactive"}, "OPTION_CONTRACT_NOT_ACTIVE"),
        ({"tradable": False}, "OPTION_CONTRACT_NOT_TRADABLE"),
        ({"style": "european"}, "OPTION_CONTRACT_NOT_AMERICAN"),
        # The entry was handed a multiplier of 100; Alpaca lists this contract with another.
        ({"multiplier": "10"}, "OPTION_CONTRACT_NOT_STANDARD"),
        ({"size": "50"}, "OPTION_CONTRACT_NOT_STANDARD"),
        ({"strike_price": "590"}, "OPTION_CONTRACT_NOT_THE_CONTRACT_ASKED_FOR"),
    ],
)
async def test_an_entry_in_a_contract_alpaca_would_not_let_a_bot_trade_is_refused_by_name(
    paper: PaperOptions, listing: dict[str, object] | None, code: str  # noqa: F811
) -> None:
    paper.account.listed = {} if listing is None else {SYMBOL: documented_option_listing(CALL, **listing)}

    refused = await paper.enter()

    _refused(refused, code)
    assert paper.venue.option_submit_calls == [] and refused.child_order_refs == ()
    assert paper.repo.get_command("cmd:a:enter-1") is None and paper.set_aside_cents() == 0


async def test_an_entry_whose_contract_alpaca_could_not_confirm_is_refused(paper: PaperOptions) -> None:  # noqa: F811
    paper.account.listing_error = BrokerUnavailable("contract reference timed out", broker="alpaca")

    refused = await paper.enter()

    _refused(refused, "OPTION_CONTRACT_REFERENCE_UNREAD")
    assert paper.venue.option_submit_calls == []


async def test_a_decision_sent_again_is_answered_from_its_own_entry_without_asking_alpaca_again(
    paper: PaperOptions,  # noqa: F811
) -> None:
    first = await paper.enter(decision="d-1")
    paper.account.listed = {}  # delisted since: an honest retry must not be told "refused" for an order that rests

    again = await paper.enter(decision="d-1")

    assert again.child_order_refs == first.child_order_refs
    assert again.state is not EffectOperationState.REJECTED
    assert paper.account.listing_reads == [SYMBOL] and len(paper.venue.option_submit_calls) == 1


# ── what Alpaca answers to the send ──────────────────────────────────────


async def test_a_lost_reply_is_found_by_looking_the_order_up_and_nothing_is_sent_twice(paper: PaperOptions) -> None:  # noqa: F811
    paper.venue.option_answer_lost = True

    sent = await paper.enter()

    assert sent.state is not EffectOperationState.REJECTED, sent.explanation
    [order_ref] = sent.child_order_refs
    assert paper.venue.option_submit_calls == [order_ref]
    assert paper.venue.option_lookup_calls == [order_ref] and paper.venue.lookup_calls == []
    assert (paper.order_state(order_ref), paper.enter_state(order_ref)) == ("new", "in_progress")
    assert paper.outcome(order_ref).standing is EntryStanding.WORKING


async def test_a_lookup_that_fails_leaves_the_outcome_unknown_and_the_whole_commitment_set_aside(
    paper: PaperOptions,  # noqa: F811
) -> None:
    paper.venue.option_answer_lost = True
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")

    sent = await paper.enter()

    [order_ref] = sent.child_order_refs
    assert paper.enter_state(order_ref) == "unknown"
    assert paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE
    # No second entry is sent for the bot while the first one's outcome is unknown.
    second = await paper.enter()
    assert second.state is EffectOperationState.REJECTED
    assert len(paper.venue.option_submit_calls) == 1


@pytest.mark.parametrize("changed", [{"symbol": "SPY260918C00590000"}, {"qty": "5"}, {"side": "sell"}])
async def test_an_answer_about_another_order_states_no_outcome(paper: PaperOptions, changed: dict[str, str]) -> None:  # noqa: F811
    order_ref = await paper.resting()
    # Alpaca answers the lookup with an order that is not the one the books asked for.
    paper.venue.option_orders[order_ref].update(changed, filled_qty="1", filled_avg_price="1.94", status="filled")
    paper.wait(WAIT_MS)

    await paper.round()

    # Nothing of it is counted, and the entry is not called ended.
    assert paper.held() == {} and paper.order_state(order_ref) == "new"
    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    assert paper.set_aside_cents() == ONE_CONTRACT_SET_ASIDE


def test_one_option_orders_answer_becomes_the_order_evidence_the_books_take() -> None:
    payload = dict(documented("option_orders", "documented_responses.json")["OptionOrderResponse"])
    payload.update(filled_qty="1", filled_avg_price="9.85", status="partially_filled")
    answer = alpaca_adapter.from_alpaca_option_order(payload)

    (evidence,) = option_order_evidence(answer)

    # The contract's own symbol, contracts, and premium a share; the broker's status and times as they are.
    assert (evidence.symbol, evidence.side, evidence.asset_class) == ("AAPL250620C00100000", "buy", "us_option")
    assert (evidence.quantity, evidence.filled_quantity, evidence.limit_price, evidence.filled_avg_price) == (
        2.0, 1.0, 10.0, 9.85,
    )
    assert (evidence.order_id, evidence.client_order_id, evidence.status) == (
        answer.order_id, answer.client_order_id, "partially_filled",
    )
    assert (evidence.submitted_at_ms, evidence.updated_at_ms) == (answer.submitted_at_ms, answer.updated_at_ms)
    assert evidence.events == []
    assert evidence_of_the_order_asked(answer, asked=AskedOptionOrder("AAPL250620C00100000", "buy", 2)) == evidence
    with pytest.raises(BrokerEvidenceUnavailable):
        evidence_of_the_order_asked(answer, asked=AskedOptionOrder("AAPL250620C00100000", "buy", 3))


def test_a_multi_leg_answer_is_not_yet_evidence_of_anything() -> None:
    answer = alpaca_adapter.from_alpaca_option_order(
        documented("option_orders", "documented_responses.json")["MultilegOptionsOrderResponse"]
    )

    with pytest.raises(BrokerEvidenceUnavailable):
        option_order_evidence(answer)


async def test_what_the_books_asked_for_is_read_from_the_order_and_a_stock_order_has_none(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)

    assert asked_option_order(paper.repo, order_ref) == AskedOptionOrder(SYMBOL, "buy", 2)
    assert asked_option_order(paper.repo, "no-such-order") is None


# ── the exit ─────────────────────────────────────────────────────────────


async def test_a_paper_exit_is_a_market_order_sent_with_no_quote_and_it_closes_what_filled(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")

    sold = await paper.exit()

    assert sold.state is not EffectOperationState.REJECTED, sold.explanation
    closing_ref = next(ref for ref in sold.child_order_refs if ref != order_ref)
    assert paper.venue.submitted_option_orders[-1] == SingleOptionOrderRequest(
        contract=CALL, side=OrderSide.SELL, intent=OptionPositionIntent.CLOSE, quantity=1, order_type=OrderType.MARKET,
    )
    assert asked_option_order(paper.repo, closing_ref) == AskedOptionOrder(SYMBOL, "sell", 1)

    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=1, price="1.90", execution_id="x-2")
    await paper.pass_()

    assert paper.held() == {}
    assert paper.repo.active_exit_for_strategy("a") is None


async def test_a_closing_order_alpaca_shows_only_by_its_own_id_is_still_read_as_an_option_order(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    sold = await paper.exit()
    closing_ref = next(ref for ref in sold.child_order_refs if ref != order_ref)
    # Alpaca fills the close, then answers "no such order" to its client order id and still has it by its own id.
    paper.venue.option_order_fills(closing_ref, contracts=1, average_price="1.90")
    paper.venue.option_orders["shown-by-its-own-id-only"] = paper.venue.option_orders.pop(closing_ref)

    await paper.pass_()

    assert paper.order_state(closing_ref) == "filled"
    assert paper.held() == {} and paper.venue.lookup_calls == []


async def test_an_exit_while_the_entry_still_rests_cancels_it_first_and_closes_only_what_filled(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")

    await paper.exit()

    assert [why for why, _at in paper.cancel_requests(order_ref)] == ["EXIT_ENTRY_TERMINAL_PROOF"]
    assert paper.order_state(order_ref) == "canceled"
    closing = paper.venue.submitted_option_orders[-1]
    assert (closing.side, closing.intent, closing.quantity) == (OrderSide.SELL, OptionPositionIntent.CLOSE, 1)


# ── the outcome the runner asks for ──────────────────────────────────────


async def test_a_dry_runs_entry_is_settled_as_entered_at_once(dry_run: _DryRun) -> None:  # noqa: F811
    bought = await dry_run.enter(dry_run.evidence(), decision="d-1")
    before = dry_run.clock()

    settled = await dry_run.clerk.await_entry_outcome(
        strategy_instance_id="dry-run-buys-a-call", decision_id="d-1", until_ms=before + 60_000
    )

    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.FILLED)
    assert (settled.units_asked, settled.units_filled, settled.recorded) == (1, 1, True)
    assert settled.order_ref == bought.child_order_refs[0]
    assert (settled.settled_at_ms, dry_run.clock()) == (before, before)


async def test_an_entry_that_ends_unfilled_is_settled_as_discarded_once_its_cancel_is_proven(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(decision="d-1")
    deadline = paper.outcome(order_ref).cancel_at_ms

    async def one_poll_of_the_wait(seconds: float) -> None:
        paper.wait(round(seconds * 1000))
        await paper.round()

    paper.clerk._outcome_pause = one_poll_of_the_wait

    settled = await paper.clerk.await_entry_outcome(
        strategy_instance_id="a", decision_id="d-1", until_ms=paper.clock() + 60_000
    )

    assert (settled.settlement, settled.standing, settled.units_filled) == (
        EntrySettlement.DISCARDED, EntryStanding.UNFILLED, 0,
    )
    # Answered as soon as the cancel was proven, well before the caller's limit.
    assert settled.settled_at_ms == deadline and settled.recorded


async def test_an_entry_that_fills_in_part_is_settled_as_entered(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2, decision="d-1")
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.wait(WAIT_MS)
    await paper.round()

    settled = await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-1", until_ms=paper.clock())

    assert (settled.settlement, settled.standing) == (EntrySettlement.ENTERED, EntryStanding.PART_FILLED)
    assert (settled.units_asked, settled.units_filled) == (2, 1)


async def test_an_entry_still_unproven_at_the_callers_limit_is_settled_as_entered_and_that_answer_stands(
    paper: PaperOptions,  # noqa: F811
) -> None:
    order_ref = await paper.resting(decision="d-1")
    paper.venue.cancel_is_slow = True
    paper.clerk._outcome_pause = patiently(paper)
    until_ms = paper.clock() + 5_000

    settled = await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-1", until_ms=until_ms)

    assert (settled.settlement, settled.standing, settled.recorded) == (
        EntrySettlement.ENTERED, EntryStanding.WORKING, True,
    )
    assert (settled.until_ms, settled.settled_at_ms) == (until_ms, until_ms)

    # The order then ends with nothing filled. What was answered is what the strategy acted on.
    paper.venue.cancel_is_slow = False
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED

    again = await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-1", until_ms=until_ms + 99)

    assert again == settled
    assert len([kind for kind, _summary, _facts in paper.log() if kind == "ENTRY_SETTLED"]) == 1


async def test_a_wait_that_ends_because_the_run_stopped_is_answered_and_not_recorded(paper: PaperOptions) -> None:  # noqa: F811
    await paper.resting(decision="d-1")
    pauses = 0

    async def the_owner_stops_the_bot(_seconds: float) -> None:
        nonlocal pauses
        pauses += 1
        assert pauses == 1, "the wait went on after the run had stopped"
        submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")

    paper.clerk._outcome_pause = the_owner_stops_the_bot

    settled = await paper.clerk.await_entry_outcome(
        strategy_instance_id="a", decision_id="d-1", until_ms=paper.clock() + 60_000
    )

    assert (settled.settlement, settled.standing, settled.recorded) == (
        EntrySettlement.ENTERED, EntryStanding.WORKING, False,
    )
    assert "ENTRY_SETTLED" not in [kind for kind, _summary, _facts in paper.log()]


async def test_a_proven_outcome_is_recorded_even_after_the_run_stopped(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(decision="d-1")
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")

    settled = await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-1", until_ms=paper.clock())

    assert (settled.settlement, settled.standing, settled.recorded) == (
        EntrySettlement.ENTERED, EntryStanding.FILLED, True,
    )


async def test_books_rebuilt_from_their_mirror_hold_the_entrys_deadline_and_its_settlement(
    paper: PaperOptions, tmp_path_factory: pytest.TempPathFactory  # noqa: F811
) -> None:
    order_ref = await paper.resting(decision="d-1")
    paper.wait(WAIT_MS)
    await paper.round()
    settled = await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-1", until_ms=paper.clock())
    kept = option_entry_of_order(paper.repo, order_ref)
    replay_root = tmp_path_factory.mktemp("replay")
    shutil.copytree(paper.artifacts_root, replay_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("clerk.db*"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=paper.repo.account_id, artifacts_root=replay_root, clock=paper.clock
    )

    try:
        assert kept is not None and kept.cancel_at_ms is not None
        assert option_entry_of_order(rebuilt, order_ref) == kept
        assert entry_outcome(rebuilt, order_ref).standing is EntryStanding.UNFILLED
        order = rebuilt.order(order_ref)
        recorded = rebuilt.first_effect_transition(
            effect_operation_id=order.effect_operation_id, transition_kind="ENTRY_SETTLED"
        )
        assert recorded is not None
        assert SettledEntry.of(
            EntrySettledFacts.from_facts_json(recorded["facts_json"]), order_ref=order_ref, recorded=True
        ) == settled
        verify_database(rebuilt.db_path, expected_account_id=paper.repo.account_id)
    finally:
        rebuilt.close()


async def test_a_refused_decision_has_no_entry_to_wait_for(paper: PaperOptions) -> None:  # noqa: F811
    paper.account.listed = {}
    await paper.enter(decision="d-refused")

    with pytest.raises(LookupError):
        await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-refused", until_ms=paper.clock())
    with pytest.raises(ValueError, match="until_ms"):
        await paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-refused", until_ms=-1)


async def test_another_bot_enters_while_one_waits_for_its_outcome(paper: PaperOptions) -> None:  # noqa: F811
    await paper.resting("a", decision="d-1")
    polls = 0

    async def yielding_pause(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        await asyncio.sleep(0)
        if polls >= 3:
            paper.wait(60_000)

    paper.clerk._outcome_pause = yielding_pause

    waited, entered = await asyncio.gather(
        paper.clerk.await_entry_outcome(strategy_instance_id="a", decision_id="d-1", until_ms=paper.clock() + 30_000),
        paper.enter("b"),
    )

    # The wait held no lock: B's entry was taken while A's was still unproven.
    assert entered.state is EffectOperationState.SUBMITTED, entered.explanation
    assert waited.standing is EntryStanding.WORKING and polls >= 3

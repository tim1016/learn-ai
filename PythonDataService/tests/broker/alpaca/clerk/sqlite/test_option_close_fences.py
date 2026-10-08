"""What stops a Dry Run's option position being closed, and what gets it closed again (#2913).

Two things can stand between a bot and the close of the contract it holds.

Nothing kept can price it: the newest quote handed over has nothing bid. The
position stays open, and the first quote with a bid lets it be sold: by the
Clerk's own retry while it still retries, and by the owner's Flatten always.

The contract has expired: its last session is over. Then nothing is sold, by
anyone, at any price from before. The position stays on the books under a
notice that says so, and the Clerk sends nothing more for it.

The scene is the one of ``test_option_entry_and_exit``: one SPY call expiring
Friday 2026-09-18, bought at noon on Tuesday 2026-09-08 at $1.94 a share.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotesUnavailable
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState
from app.broker.alpaca.clerk.option_effects import OptionExitEvidence
from app.broker.alpaca.clerk.program_leg import LegRefusal, LegShape
from app.broker.alpaca.clerk.recovery_reduction import (
    ConfirmedRecoveryLimit,
    ConfirmedRecoveryShape,
    RegularSessionReduction,
)
from app.broker.alpaca.clerk.sqlite import enter as enter_module
from app.broker.alpaca.clerk.sqlite import exit_resolution
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter, submit_accepted_enter
from app.broker.alpaca.clerk.sqlite.exit_recovery import latest_exit_recovery
from app.broker.alpaca.clerk.sqlite.option_admission import work_out_option_entry
from app.broker.alpaca.clerk.sqlite.option_close import recorded_option_close
from app.broker.alpaca.clerk.sqlite.option_entry import OptionExitFacts
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ProjectedUncertainty,
    RecoveryCapability,
    SafeFlattenPlan,
)
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    RecoveryExecutionResult,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    RecoveryPolicyContext,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.runtime import _OptionQuoteBoundTradePort
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    execute_safe_flatten_plan,
)
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulationEvidenceUnavailable
from app.broker.alpaca.clerk.sqlite.uncertainty import open_exit_stuck
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.simulated_fill import SimulatedFillModel
from app.schemas.market_liveness import TopOfBookQuote
from app.services import market_liveness
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.test_claimed_broker_io import _Repo
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    BUYS_ONE_CALL,
    GENERATION,
    LEG,
    RUN,
    SID,
    SYMBOL,
    _DryRun,
    _quote,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)

EXPIRY = date(2026, 9, 18)
NOON_OF_THE_ENTRY = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
THURSDAY_NOON = et_minute_of_day_ms(date(2026, 9, 17), 12 * 60)
EXPIRY_NOON = et_minute_of_day_ms(EXPIRY, 12 * 60)
EXPIRY_CLOSE = et_minute_of_day_ms(EXPIRY, 16 * 60)
SATURDAY_NOON = et_minute_of_day_ms(date(2026, 9, 19), 12 * 60)
MONDAY_NOON = et_minute_of_day_ms(date(2026, 9, 21), 12 * 60)
MONDAY_AFTER_THE_OPEN = et_minute_of_day_ms(date(2026, 9, 21), 9 * 60 + 31)
PASS_MS = 15_000


def _wait_until(account: _DryRun, at_ms: int) -> None:
    """Days pass. The Clerk's hold on its account is taken up again where it left it, as after a long freeze."""
    assert at_ms >= account.clock()
    account.clock.value = at_ms
    account.repo.revive_execution_lease()


def _hand_over(account: _DryRun, bid: str, ask: str, *, age_ms: int = 200) -> None:
    """The newest quote held for the contract is stated as its mark, as the Clerk's keeping states it."""
    account.hand_over({SYMBOL: _quote(bid, ask, received_ms=account.clock() - age_ms)})


def _an_ask_alone(account: _DryRun, ask: str, *, age_ms: int = 200) -> HeldQuote:
    """A quote with an ask and no bid: since the last reconnect the bid has not arrived."""
    return HeldQuote(
        bid=None, ask=QuoteSide(Decimal(ask), account.clock() - age_ms), data_type=QuoteDataType.LIVE,
        generation=GENERATION,
    )


async def _stop(account: _DryRun) -> None:
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN)


async def _passes(account: _DryRun, count: int) -> None:
    """The Clerk's own pass over its account, every fifteen seconds."""
    for _ in range(count):
        account.wait(PASS_MS)
        await account.clerk.reconcile_once()


def _notice(account: _DryRun, reason_code: str = "EXIT_NOT_FLAT") -> dict | None:
    return account.repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=reason_code, strategy_instance_id=SID)


def _notice_words(account: _DryRun) -> str:
    import json

    notice = _notice(account)
    assert notice is not None, "no notice says the position is still open"
    facts = json.loads(notice["facts_json"])
    return f"{facts['headline']} {facts['explanation']} {facts['next_step']}"


def _recovery_says(account: _DryRun) -> tuple[str, str]:
    """What the Clerk last recorded of its own retries for the open notice: hold or failure, and why."""
    outcome, reason_code, _words = _recovery_recorded(account)
    return outcome, reason_code


def _recovery_recorded(account: _DryRun) -> tuple[str, str, str]:
    notice = _notice(account)
    assert notice is not None
    recorded = latest_exit_recovery(account.repo, strategy_instance_id=SID, uncertainty_id=notice["uncertainty_id"])
    assert recorded is not None
    return recorded.outcome, recorded.reason_code, recorded.explanation or ""


def _sales(account: _DryRun) -> list[tuple[str, str, float, float, int | None]]:
    return [fill for fill in account.fills() if fill[1] == "sell"]


def _exits_made(account: _DryRun) -> int:
    """Every EXIT on the books, whoever made it and however it ended: one is made for each try to close."""
    with account.repo._write_lock:
        return account.repo._conn.execute("SELECT COUNT(*) FROM effect_operations WHERE kind = 'EXIT'").fetchone()[0]


async def _context(account: _DryRun) -> RecoveryPolicyContext:
    context = await read_recovery_context(account.clerk, strategy_instance_id=SID)
    assert context is not None
    return context


async def _offered(account: _DryRun, action_id: str) -> RecoveryCapability:
    """One action of the bot's recovery catalog, read the way the bot's page reads it."""
    return next(item for item in build_recovery_catalog(await _context(account)) if item.action_id == action_id)


async def _flatten(account: _DryRun, *, limit: ConfirmedRecoveryLimit | None = None) -> RecoveryExecutionResult:
    """The owner presses Reconcile now and then Flatten, as the bot's page does."""
    await account.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    presented = await _offered(account, "execute_safe_flatten")
    assert presented.available, (presented.unavailable_reason_code, presented.unavailable_reason)

    async def current_context() -> RecoveryPolicyContext:
        return await _context(account)

    return await execute_recovery_action(
        account.clerk,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten", concurrency_token=presented.concurrency_token,
            execution_ref=presented.execution_ref, reason="owner-flatten", confirmed_limit=limit,
        ),
        current_context=current_context,
    )


async def _flatten_refused(account: _DryRun, *, limit: ConfirmedRecoveryLimit | None = None) -> RecoveryExecutionError:
    orders = account.orders()
    with pytest.raises(RecoveryExecutionError) as refused:
        await _flatten(account, limit=limit)
    assert refused.value.refusal is not None, "the Flatten was refused with no reason code"
    assert account.orders() == orders, "a refused Flatten left an order on the books"
    return refused.value


async def _flatten_not_offered(account: _DryRun, code: str) -> str:
    """The owner presses Reconcile now and finds no Flatten to press; returns what the page says instead."""
    await account.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    said = []
    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        offered = await _offered(account, action_id)
        assert not offered.available, f"{action_id} is offered"
        assert offered.unavailable_reason_code == code, (offered.unavailable_reason_code, offered.unavailable_reason)
        assert offered.reduction_plan is None
        said.append(f"{offered.unavailable_reason} {offered.next_step}")
    return said[-1]


async def _a_plan_made_while_a_flatten_could_be_sent(account: _DryRun) -> SafeFlattenPlan:
    """A Flatten the owner was shown when it could go out, to be pressed after it no longer can."""
    await account.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    plan = (await _offered(account, "execute_safe_flatten")).reduction_plan
    assert plan is not None, "no Flatten was offered"
    return plan


def _pressed_anyway(account: _DryRun, plan: SafeFlattenPlan, *, limit: ConfirmedRecoveryLimit | None = None) -> LegRefusal:
    """Beneath the catalog: what the Clerk shows for an old plan, and what pressing it comes to. The same refusal."""
    orders = account.orders()
    shown = account.clerk.price_safe_flatten(plan)
    with pytest.raises(SafeFlattenExecutionError) as refused:
        account.clerk._safe_flatten_shape(plan, limit)
    assert isinstance(shown, LegRefusal), "the old plan is still shown as one that can go out"
    assert refused.value.refusal is not None and account.orders() == orders
    if limit is None:
        assert refused.value.refusal.reason_code == shown.reason_code
    return refused.value.refusal


def _symbols_the_stock_feed_was_asked_about() -> set[str]:
    """Every symbol put to the IBKR stock quote source, which is what adds one to the shares it watches."""
    return set(market_liveness.get_market_liveness_store()._requested_symbols)


# ── nothing bid: the position waits for a quote that can price it ─────────


async def test_a_stopped_bot_whose_last_quote_had_nothing_bid_is_sold_by_the_owners_flatten_once_a_bid_is_kept(
    dry_run: _DryRun,  # noqa: F811 -- the imported fixture
) -> None:
    """The whole way round. A bid of $2.00 is kept, then one of nothing, and the owner presses Stop.

    The simulation's close of what the run left is refused: nothing is bid.
    The Clerk tries three more times, two minutes apart, reads the same last
    quote each time, and stops trying. Then a quote with $0.80 bid is kept,
    and the owner's Flatten sells the one contract at $0.80.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "2.00", "2.06")
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []
    assert _notice(dry_run) is not None, "the refused close left no notice"

    await _passes(dry_run, 50)  # twelve and a half minutes of the regular session

    assert open_exit_stuck(dry_run.repo, SID) is not None, "the Clerk never gave up retrying"
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []
    orders_when_it_gave_up = dry_run.orders()
    await _passes(dry_run, 4)
    assert dry_run.orders() == orders_when_it_gave_up, "the Clerk went on retrying after it gave up"

    # Nothing can price a sale, so no Flatten is offered that would only be refused.
    await _flatten_not_offered(dry_run, "OPTION_EXIT_UNPRICED")

    _hand_over(dry_run, "0.80", "0.86")
    flattened = await _flatten(dry_run)

    assert flattened.applied
    assert dry_run.held() == {}
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 0.80, 100)]
    # Bought at $1.94, sold at $0.80, a hundred shares: $114.00 lost.
    assert dry_run.realized_pnl() == Decimal("-114")
    [sale] = [fill for fill in dry_run.clerk.synthesized_option_fills().values() if fill.request.side is OrderSide.SELL and fill.fill_price is not None]
    assert (sale.fill_model, sale.fill_price) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("0.80"))
    assert sale.quote_age_ms is not None and sale.quote_age_ms >= 200
    # The flat proof clears the notice at once; the Clerk's next pass clears "gave up retrying" with it.
    assert _notice(dry_run) is None
    await dry_run.clerk.reconcile_once()
    assert open_exit_stuck(dry_run.repo, SID) is None


async def test_an_older_bid_sells_nothing_once_a_newer_quote_has_no_bid_and_the_next_quote_with_a_bid_does(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A bid of $2.00 is kept. Then the newest quote kept has an ask and no bid, and the owner presses Stop.

    The $2.00 is what was bid before that quote, and nothing kept says it
    still is. So the simulation's close of what the run left sells nothing,
    and no Flatten is offered that would. Then a quote with $0.80 bid is
    kept, and the owner's Flatten sells the one contract at $0.80.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "2.00", "2.06")
    dry_run.wait(20_000)
    dry_run.hand_over({SYMBOL: _an_ask_alone(dry_run, "2.10")})
    await _stop(dry_run)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []
    assert _notice(dry_run) is not None, "the refused close left no notice"
    dry_run.wait(30_000)
    await _flatten_not_offered(dry_run, "OPTION_EXIT_UNPRICED")

    _hand_over(dry_run, "0.80", "0.86")
    flattened = await _flatten(dry_run)

    assert flattened.applied
    assert dry_run.held() == {}
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 0.80, 100)]
    # Bought at $1.94, sold at $0.80, a hundred shares: $114.00 lost.
    assert dry_run.realized_pnl() == Decimal("-114")


async def test_an_exit_whose_own_quote_has_no_bid_is_not_sold_at_the_bid_its_entry_saw(dry_run: _DryRun) -> None:  # noqa: F811
    """The quote read at the exit decision is the newest there is, and it has no bid. The entry's quote, twenty
    seconds older, had $1.90 bid: that prices nothing now, so nothing is sold and the position stays open."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    with_no_bid = OptionExitEvidence(
        quotes={SYMBOL: _an_ask_alone(dry_run, "2.10", age_ms=300)}, generation=GENERATION,
        read_at_ms=dry_run.clock() - 100,
    )

    refused = await dry_run.exit(with_no_bid)

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []
    assert _notice(dry_run) is not None, "the refused close left no notice"


async def test_the_owners_flatten_sells_exactly_what_is_held_once_and_a_second_press_sells_nothing(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence(), units=3)
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16", age_ms=1_000)

    await _flatten(dry_run)

    assert dry_run.held() == {}
    assert _sales(dry_run) == [(SYMBOL, "sell", 3.0, 2.10, 100)]
    # Sixteen cents a share on three contracts of a hundred shares.
    assert dry_run.realized_pnl() == Decimal("48")
    await dry_run.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    again = await _offered(dry_run, "execute_safe_flatten")
    assert not again.available and again.unavailable_reason_code == "NO_ATTRIBUTED_EXPOSURE"
    assert len(_sales(dry_run)) == 1


async def test_a_flatten_with_nothing_bid_is_not_offered_and_says_so_in_words_about_a_contract(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)

    words = (await _flatten_not_offered(dry_run, "OPTION_EXIT_UNPRICED")).lower()

    assert SYMBOL.lower() in words
    # Not the words of a share position waiting on the stock feed.
    assert "shares" not in words and "ibkr" not in words


async def test_a_flatten_shown_while_there_was_a_bid_and_pressed_after_it_went_is_refused_and_records_no_exit(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The plan is thirty seconds old. Since it was shown, the newest quote says nothing is bid."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    dry_run.wait(10_000)
    _hand_over(dry_run, "0", "0.05")

    refusal = _pressed_anyway(dry_run, plan)

    assert refusal.reason_code == "OPTION_EXIT_UNPRICED"
    assert SYMBOL in refusal.explanation and "shares" not in f"{refusal.explanation} {refusal.next_step}".lower()
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []


async def test_no_question_about_a_contract_is_ever_put_to_the_stock_quote_source(dry_run: _DryRun) -> None:  # noqa: F811
    """Asking that source about a symbol adds it to the shares the IBKR supervisor watches.

    Every step that looks for a price to close the contract at is taken: the
    simulation's close of the ended run, the Clerk's retries, a Flatten that
    is not offered, one pressed anyway and refused, the price shown to the
    owner, and a Flatten that sells.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    _hand_over(dry_run, "0", "0.05", age_ms=100)
    _pressed_anyway(dry_run, plan)
    await _flatten_not_offered(dry_run, "OPTION_EXIT_UNPRICED")
    dry_run.clerk._trade.bind_latest_recovery_bar("no-such-order", symbol=SYMBOL, side="sell")
    _hand_over(dry_run, "0.80", "0.86", age_ms=50)

    await _flatten(dry_run)

    assert dry_run.held() == {} and _sales(dry_run) == [(SYMBOL, "sell", 1.0, 0.80, 100)]
    assert SYMBOL not in _symbols_the_stock_feed_was_asked_about()
    # The same reading does see a question that is put to it.
    market_liveness.prepared_top_of_book("SPY", dry_run.clock())
    assert "SPY" in _symbols_the_stock_feed_was_asked_about()


async def test_a_quote_stamped_after_the_clerks_clock_gets_no_flatten_offered(dry_run: _DryRun) -> None:  # noqa: F811
    """The newest quote received by now has nothing bid. One stamped a minute ahead has $2.10 bid.

    That one is not a price a close made now could meet, so the page offers
    no Flatten on it, as the simulation would sell nothing at it.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    dry_run.hand_over({SYMBOL: _quote("2.10", "2.16", received_ms=dry_run.clock() + 60_000)})

    await _flatten_not_offered(dry_run, "OPTION_EXIT_UNPRICED")

    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []


async def test_a_contract_spelled_in_lower_case_is_never_taken_for_a_share(dry_run: _DryRun) -> None:  # noqa: F811
    """Whether a symbol is a contract is read from its spelling. Spelled in lower case it is one all the same.

    Its Flatten is shown and shaped as a contract's is, a limit for it is
    refused as a contract's is, the simulation answers from the quotes kept
    for it, and the stock quote source is asked nothing about it.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    in_lower_case = replace(plan, legs=(replace(plan.legs[0], symbol=SYMBOL.lower()),))
    limit = ConfirmedRecoveryLimit(limit_price=Decimal("2.05"), quote_observed_at_ms=dry_run.clock() - 1_000)
    simulation = dry_run.clerk._trade

    shown = dry_run.clerk.price_safe_flatten(in_lower_case)
    shaped = dry_run.clerk._safe_flatten_shape(in_lower_case, None)
    with pytest.raises(SafeFlattenExecutionError) as with_a_limit:
        dry_run.clerk._safe_flatten_shape(in_lower_case, limit)
    priced = simulation.recovery_price_available(SYMBOL.lower(), side="sell")
    bound_to_a_stock_quote = simulation.bind_latest_recovery_bar("no-such-order", symbol=SYMBOL.lower(), side="sell")

    # The market order in regular hours that closes an option position, with no price of its own.
    assert isinstance(shown, RegularSessionReduction) and shaped is None
    assert with_a_limit.value.refusal is not None
    assert with_a_limit.value.refusal.reason_code == "OPTION_CLOSE_TAKES_NO_LIMIT"
    # From the $2.10 bid kept for the contract, not from a stock quote nobody has.
    assert priced and not bound_to_a_stock_quote
    assert SYMBOL not in _symbols_the_stock_feed_was_asked_about()


async def test_the_clerks_own_retry_sells_once_a_quote_with_a_bid_is_kept(dry_run: _DryRun) -> None:  # noqa: F811
    """Before it has given up, the Clerk's next retry reads the newer quote and sells, once."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    assert dry_run.held() == {SYMBOL: 1.0}

    _hand_over(dry_run, "1.10", "1.16")
    await _passes(dry_run, 10)  # past the two minutes a refused close is left to settle

    assert dry_run.held() == {}
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 1.10, 100)]
    assert _notice(dry_run) is None


async def test_an_owners_flatten_of_an_option_position_after_hours_is_not_offered_until_the_open(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """With a limit price or without one: an option position is closed by a market order in regular hours.

    A plan shown at half past three and pressed at five is refused the same
    way, and says when the next session opens.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(3 * 3_600_000 + 29 * 60_000)  # 15:29
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    _wait_until(dry_run, NOON_OF_THE_ENTRY + 5 * 3_600_000)  # 17:00
    _hand_over(dry_run, "2.10", "2.16")
    limit = ConfirmedRecoveryLimit(limit_price=Decimal("2.05"), quote_observed_at_ms=dry_run.clock() - 1_000)

    await _flatten_not_offered(dry_run, "OPTION_CLOSE_WAITS_FOR_OPEN")
    without, with_a_limit = _pressed_anyway(dry_run, plan), _pressed_anyway(dry_run, plan, limit=limit)

    assert without.reason_code == with_a_limit.reason_code == "OPTION_CLOSE_WAITS_FOR_OPEN"
    assert without.available_at_ms == et_minute_of_day_ms(date(2026, 9, 9), 9 * 60 + 30)
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []


async def test_a_limit_price_confirmed_for_an_option_flatten_in_regular_hours_is_refused_by_name(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    limit = ConfirmedRecoveryLimit(limit_price=Decimal("2.05"), quote_observed_at_ms=dry_run.clock() - 1_000)

    refused = await _flatten_refused(dry_run, limit=limit)

    assert refused.refusal.reason_code == "OPTION_CLOSE_TAKES_NO_LIMIT"
    assert dry_run.held() == {SYMBOL: 1.0}


async def test_a_priced_leg_that_reaches_the_flattens_own_executor_is_a_refusal_and_never_a_crash(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Beneath the gate above: the executor handed an after-hours limit for an option position names the refusal."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    await dry_run.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    plan = (await _offered(dry_run, "execute_safe_flatten")).reduction_plan
    assert plan is not None
    priced = ConfirmedRecoveryShape(
        shape=LegShape(
            order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=2.05, extended_hours=True,
            side=OrderSide.SELL,
        ),
        valid_until_ms=dry_run.clock() + 3_600_000,
        reference_quote=TopOfBookQuote(
            symbol=SYMBOL, bid=2.10, ask=2.16, source="test", observed_at_ms=dry_run.clock() - 1_000
        ),
        quantity=1.0,
    )
    orders = dry_run.orders()

    with pytest.raises(SafeFlattenExecutionError) as refused:
        await execute_safe_flatten_plan(
            dry_run.repo, plan=plan, read=dry_run.clerk._read, trade=dry_run.clerk._trade,
            intake=dry_run.clerk._intake, account_id=dry_run.repo.account_id,
            pricing=dry_run.clerk.recovery_pricing, confirmed_shape=priced,
        )

    assert refused.value.refusal is not None and refused.value.refusal.reason_code == "OPTION_CLOSE_TAKES_NO_LIMIT"
    assert dry_run.orders() == orders and dry_run.held() == {SYMBOL: 1.0}


async def _pressed_past_the_pages_own_check(account: _DryRun, plan: SafeFlattenPlan) -> SafeFlattenExecutionError:
    """The Flatten goes to its executor, as it does once the page's own check has passed, and is not taken."""
    with pytest.raises(SafeFlattenExecutionError) as refused:
        await execute_safe_flatten_plan(
            account.repo, plan=plan, read=account.clerk._read, trade=account.clerk._trade,
            intake=account.clerk._intake, account_id=account.repo.account_id, pricing=account.clerk.recovery_pricing,
        )
    assert refused.value.refusal is not None, "the Flatten was refused with no reason code"
    return refused.value


async def test_a_flatten_the_simulation_would_not_take_is_told_in_the_simulations_own_words_under_a_code(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The bid went between the page's check and the sale. The simulation sells nothing for nothing.

    No broker was asked, so the owner is not told that one rejected an
    order, and is not told to retry.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "2.10", "2.16")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    dry_run.wait(5_000)
    _hand_over(dry_run, "0", "0.05")

    refused = await _pressed_past_the_pages_own_check(dry_run, plan)

    assert refused.refusal.reason_code == "ORDER_SUBMIT_FAILED"
    said = str(refused).lower()
    assert SYMBOL.lower() in said and "simulation" in said
    assert "broker" not in said and "retry" not in said
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []


async def test_a_flatten_pressed_as_its_contract_expires_is_told_it_expired_and_is_sent_to_no_retry(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Shown twenty seconds before Friday's close, pressed four seconds before it.

    An order sent then would arrive after the contract's last trade. The
    Flatten is refused under the expiry's own code, and nothing in what the
    owner is told says to flatten again.

    The bot is stopped forty seconds before the close with nothing bid, so
    its closes fail. The bid comes twenty seconds later, sooner than the
    thirty the Clerk leaves a failed scheduled close before sending it
    again, so it is the owner's Flatten that meets the expiry.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_CLOSE - 40_000)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    dry_run.wait(20_000)
    _hand_over(dry_run, "0.80", "0.86")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    dry_run.wait(16_000)

    refused = await _pressed_past_the_pages_own_check(dry_run, plan)

    assert refused.refusal.reason_code == "OPTION_CONTRACT_EXPIRED"
    _says_only_what_is_true_of_an_expired_contract(str(refused))
    assert "broker" not in str(refused).lower()
    await _expired_and_unsold(dry_run)


# ── the contract has expired: nothing is sold at a price from before ──────


async def _expired_and_unsold(account: _DryRun, *, fills: int = 1) -> None:
    """Nothing was sold: the one buy is the only fill, no P&L was booked, and the contract is still held."""
    assert account.held() == {SYMBOL: 1.0}
    assert len(account.fills()) == fills and _sales(account) == []
    assert account.realized_pnl() == Decimal("0")
    assert all(fill.request.side is OrderSide.BUY for fill in account.clerk.synthesized_option_fills().values())


def _refused_as_expired(receipt: EffectOperationReceipt) -> None:
    assert receipt.state is EffectOperationState.REJECTED, receipt.explanation
    assert receipt.explanation.startswith("OPTION_CONTRACT_EXPIRED: "), receipt.explanation
    assert SYMBOL in receipt.explanation and "2026-09-18" in receipt.explanation


async def test_an_exit_sent_after_the_contract_expired_is_refused_by_name_and_sells_nothing(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Bought on the 8th, the call expired on Friday the 18th. The exit arrives on Monday the 21st at noon."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, MONDAY_NOON)
    orders = dry_run.orders()

    refused = await dry_run.exit(None)

    _refused_as_expired(refused)
    assert refused.child_order_refs == () and (dry_run.orders(), _exits_made(dry_run)) == (orders, 0)
    await _expired_and_unsold(dry_run)
    # The bot's page says so: the contract by name, that it expired, and that nothing settles it yet.
    words = _notice_words(dry_run)
    assert SYMBOL in words and "2026-09-18" in words and "expired" in words and "settle" in words

    again = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    _refused_as_expired(again)
    assert (dry_run.orders(), _exits_made(dry_run)) == (orders, 0), "no exit is made of a close that cannot be sent"
    await _expired_and_unsold(dry_run)
    # No runner holds this run, so the Clerk's passes end it, and the simulation's one close of what an
    # ended run left is made and refused. After that nothing more is made of the notice, pass after pass.
    await _passes(dry_run, 12)
    assert (dry_run.orders(), _exits_made(dry_run)) == (orders, 1)
    await _passes(dry_run, 36)
    assert (dry_run.orders(), _exits_made(dry_run)) == (orders, 1)
    assert _recovery_says(dry_run) == ("hold", "OPTION_CONTRACT_EXPIRED")
    await _expired_and_unsold(dry_run)


async def test_an_exit_with_a_fresh_quote_of_its_own_is_refused_just_the_same_once_the_contract_has_expired(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_CLOSE)

    _refused_as_expired(await dry_run.exit(dry_run.exit_quotes("2.40", "2.46")))
    await _expired_and_unsold(dry_run)


async def test_a_close_on_the_expiry_day_while_its_session_is_open_still_sells(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_NOON)

    closed = await dry_run.exit(dry_run.exit_quotes("0.40", "0.46"))

    assert closed.state is EffectOperationState.FLAT, closed.explanation
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 0.40, 100)]
    assert dry_run.realized_pnl() == Decimal("-154")


async def test_an_exit_decided_after_hours_on_expiry_friday_is_refused_then_and_nothing_is_sent_on_monday(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Five past four on the Friday the call expires. Any other day this exit would wait for the next open.

    This contract has no next open. The exit is refused at once, by name,
    and Monday's pass finds no exit waiting to be sent at Friday's bid.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_CLOSE + 5 * 60_000)

    refused = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    _refused_as_expired(refused)
    assert dry_run.repo.active_exit_for_strategy(SID) is None, "an exit that can never be sent is waiting"
    _wait_until(dry_run, MONDAY_AFTER_THE_OPEN)
    await dry_run.clerk.reconcile_once()
    await _expired_and_unsold(dry_run)


async def test_a_close_that_could_not_arrive_before_the_last_session_ends_is_refused_and_one_that_can_sells(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """An order is judged where it would arrive, five seconds after it is sent.

    Sent 5.001 seconds before Friday's close it arrives inside the session
    and sells. Sent one millisecond later it would arrive at the close: it
    is not held for an open that will never come, it is refused as expired.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_CLOSE - 5_000)

    too_late = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    _refused_as_expired(too_late)
    await _expired_and_unsold(dry_run)


async def test_a_close_sent_just_in_time_on_the_expiry_day_sells(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_CLOSE - 5_001)

    in_time = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    assert in_time.state is EffectOperationState.FLAT, in_time.explanation
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 2.40, 100)]


async def test_a_close_held_since_before_the_expiry_and_sent_on_monday_ends_with_nothing_sold(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Decided on Thursday evening and held for the open, with the Clerk down all of Friday.

    Its turn comes on Monday, after the contract expired. It is ended with
    nothing sent, under its own code, and the bid of $2.40 it was decided on
    is never booked as a sale.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, THURSDAY_NOON + 5 * 3_600_000)  # Thursday 17:00
    held_back = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))
    assert held_back.state is EffectOperationState.ACCEPTED, held_back.explanation
    assert dry_run.repo.active_exit_for_strategy(SID) is not None
    _wait_until(dry_run, MONDAY_AFTER_THE_OPEN)

    await dry_run.clerk.reconcile_once()

    await _expired_and_unsold(dry_run)
    assert dry_run.repo.active_exit_for_strategy(SID) is None
    failed = dry_run.repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_NOT_FLAT")
    assert failed is not None and failed["summary_code"] == "OPTION_CONTRACT_EXPIRED"


async def test_a_closing_order_made_on_friday_and_reached_for_sending_on_monday_is_ended_as_expired(
    dry_run: _DryRun, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The exit is decided ten seconds before Friday's close and its order is made. Then the Clerk freezes.

    It comes back on Monday with the order made and not sent. The expiry is
    asked again before a made order goes out: it is ended under the expiry's
    own code, and the $2.40 bid it was decided on is never booked.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_CLOSE - 10_000)
    made = exit_resolution._reducing_order_facts
    frozen: list[int] = []

    def made_and_then_the_clerk_freezes_until_monday(repo: object, order_ref: str) -> object:
        if not frozen:
            frozen.append(dry_run.clock())
            _wait_until(dry_run, MONDAY_AFTER_THE_OPEN)
        return made(repo, order_ref)

    monkeypatch.setattr(exit_resolution, "_reducing_order_facts", made_and_then_the_clerk_freezes_until_monday)

    await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    assert frozen == [EXPIRY_CLOSE - 10_000], "the closing order was never made"
    await _expired_and_unsold(dry_run)
    assert dry_run.repo.active_exit_for_strategy(SID) is None
    failed = dry_run.repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_NOT_FLAT")
    assert failed is not None and failed["summary_code"] == "OPTION_CONTRACT_EXPIRED"


async def test_the_account_reading_of_a_dry_run_holding_an_expired_contract_says_it_expired_and_not_to_wait(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Before the expiry a reading with no fresh quote waits for the feed. After it no feed will quote the
    contract again: the reading is still unavailable, and says why."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, THURSDAY_NOON)
    with pytest.raises(SimulationEvidenceUnavailable) as waiting:
        dry_run.account_reading()
    _wait_until(dry_run, MONDAY_NOON)

    with pytest.raises(SimulationEvidenceUnavailable) as unsettled:
        dry_run.account_reading()

    assert "expired" not in str(waiting.value).lower()
    said = str(unsettled.value).lower()
    assert SYMBOL.lower() in said and "expired" in said and "settle" in said
    assert "wait" not in said and "feed" not in said


EXPIRES_ON_THE_ENTRY_DAY = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 8), right="call", strike=585)
ENTRY_DAY_CLOSE = et_minute_of_day_ms(date(2026, 9, 8), 16 * 60)


async def test_a_bid_handed_over_just_before_the_last_close_marks_nothing_from_the_close_on(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Bought at noon on its expiry day. At 15:59:30 the bid handed over is $2.10, and the contract is marked at it.

    At the close that hand-over is thirty seconds old, well inside what a
    mark may be. The contract has stopped trading all the same, and what it
    is worth is no longer a bid: from the close on the reading is the
    expired one. And the reading taken while it traded says its marks are
    good no longer than that.
    """
    today = EXPIRES_ON_THE_ENTRY_DAY.occ_symbol
    await dry_run.enter(dry_run.evidence(picked={LEG: EXPIRES_ON_THE_ENTRY_DAY}))
    dry_run.wait(ENTRY_DAY_CLOSE - 30_000 - dry_run.clock())
    dry_run.hand_over({today: _quote("2.10", "2.16", received_ms=dry_run.clock() - 200)})

    while_it_traded = dry_run.account_reading()
    dry_run.wait(29_999)
    a_millisecond_before_the_close = dry_run.account_reading()
    dry_run.wait(1)
    with pytest.raises(SimulationEvidenceUnavailable) as at_the_close:
        dry_run.account_reading()
    dry_run.wait(1_000)
    with pytest.raises(SimulationEvidenceUnavailable) as a_second_after:
        dry_run.account_reading()

    # Sixteen cents a share over the $1.94 paid, a hundred shares.
    assert while_it_traded.unrealized_pl_usd == a_millisecond_before_the_close.unrealized_pl_usd == Decimal("16")
    assert while_it_traded.simulation_marks_valid_until_ms == ENTRY_DAY_CLOSE - 1
    for unsettled in (at_the_close, a_second_after):
        said = str(unsettled.value).lower()
        assert today.lower() in said and "expired" in said and "settle" in said
        assert "wait" not in said and "feed" not in said


async def test_a_run_that_ends_on_the_weekend_after_expiry_has_nothing_sold_by_its_simulation(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A stopped Dry Run's simulation closes what the run left, at any hour. Not an expired contract."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_NOON)
    _hand_over(dry_run, "2.10", "2.16")
    _wait_until(dry_run, SATURDAY_NOON)
    await _stop(dry_run)

    await dry_run.clerk.reconcile_once()

    await _expired_and_unsold(dry_run)
    failed = dry_run.repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_NOT_FLAT")
    assert failed is not None and failed["summary_code"] == "OPTION_CONTRACT_EXPIRED"
    assert SYMBOL in _notice_words(dry_run)


async def test_the_clerk_spends_no_retry_on_an_expired_contract_and_never_gives_up_on_it_either(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Twelve minutes of Monday's session: no order is made, no attempt is counted, and nothing escalates."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    _wait_until(dry_run, MONDAY_AFTER_THE_OPEN)
    await dry_run.clerk.reconcile_once()
    orders, exits = dry_run.orders(), _exits_made(dry_run)
    assert exits == 1, "the one close the simulation tried when the run ended"

    await _passes(dry_run, 48)

    assert (dry_run.orders(), _exits_made(dry_run)) == (orders, exits), "the Clerk tried again to close it"
    assert open_exit_stuck(dry_run.repo, SID) is None
    assert _recovery_says(dry_run) == ("hold", "OPTION_CONTRACT_EXPIRED")
    await _expired_and_unsold(dry_run)


async def test_a_position_that_failed_to_close_before_expiry_is_not_retried_after_it(dry_run: _DryRun) -> None:  # noqa: F811
    """Nothing was bid on Thursday, so the close failed and the Clerk was retrying. Then the contract expired."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, THURSDAY_NOON)
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    _wait_until(dry_run, MONDAY_AFTER_THE_OPEN)
    _hand_over(dry_run, "0.80", "0.86")
    orders, exits = dry_run.orders(), _exits_made(dry_run)

    await _passes(dry_run, 12)

    assert (dry_run.orders(), _exits_made(dry_run)) == (orders, exits)
    assert _recovery_says(dry_run) == ("hold", "OPTION_CONTRACT_EXPIRED")
    await _expired_and_unsold(dry_run)


async def test_the_owners_flatten_of_an_expired_contract_is_not_offered_and_cannot_be_forced(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, THURSDAY_NOON)
    await _stop(dry_run)
    _hand_over(dry_run, "0", "0.05")
    await dry_run.clerk.reconcile_once()
    dry_run.wait(30_000)
    _hand_over(dry_run, "0.80", "0.86")
    plan = await _a_plan_made_while_a_flatten_could_be_sent(dry_run)
    _wait_until(dry_run, MONDAY_NOON)
    _hand_over(dry_run, "0.80", "0.86")

    await _flatten_not_offered(dry_run, "OPTION_CONTRACT_EXPIRED")
    # A plan made on Thursday, while the contract still traded, pressed on Monday.
    assert _pressed_anyway(dry_run, plan).reason_code == "OPTION_CONTRACT_EXPIRED"
    await _expired_and_unsold(dry_run)


async def test_the_simulation_itself_will_not_price_an_expired_contract_when_the_clerks_own_fence_is_gone(
    dry_run: _DryRun, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Two fences. With the Clerk's taken away, the close reaches the simulation, which refuses it."""
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, EXPIRY_NOON)
    _hand_over(dry_run, "2.10", "2.16")
    _wait_until(dry_run, SATURDAY_NOON)
    monkeypatch.setattr(exit_resolution, "expired_option_close", lambda _symbol, _now_ms: None)
    await _stop(dry_run)

    await dry_run.clerk.reconcile_once()

    await _expired_and_unsold(dry_run)
    failed = dry_run.repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_NOT_FLAT")
    assert failed is not None and "OPTION_EXIT_UNPRICED" in failed["facts_json"]


async def test_what_the_owner_can_still_do_about_an_expired_contract_is_said_as_it_is(dry_run: _DryRun) -> None:  # noqa: F811
    """An expired contract is never written off: that would leave what it cost on the books for good.

    So the notice does not send the owner to a write-off, or to a flatten.
    What is offered, once the simulation has said it has no price to settle
    the contract from, is to end it at the owner's word.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    _wait_until(dry_run, MONDAY_NOON)
    await dry_run.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")

    write_off = await _offered(dry_run, "discharge_attributed_residue")

    assert not write_off.available
    assert write_off.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_IS_NOT_WRITTEN_OFF"
    # Neither the notice nor the refused write-off sends the owner to a flatten, which is refused too.
    for words in (_notice_words(dry_run).lower(), write_off.next_step.lower()):
        assert "flatten" not in words and "discharg" not in words and "write" not in words
    assert (await _offered(dry_run, "end_expired_option")).available


# ── a stopped bot whose close failed before the contract expired ──────────


async def _stopped_on_thursday_with_nothing_bid(account: _DryRun) -> None:
    """Bought on the 8th. On Thursday the 17th nothing is bid and the owner presses Stop.

    The simulation's close of what the run left is refused: it sells nothing
    for nothing. The contract expires the next day.
    """
    await account.enter(account.evidence())
    _wait_until(account, THURSDAY_NOON)
    _hand_over(account, "0", "0.05")
    await _stop(account)
    await account.clerk.reconcile_once()
    assert account.held() == {SYMBOL: 1.0} and _notice(account) is not None


async def _page_notices(account: _DryRun) -> dict[str, ProjectedUncertainty]:
    """The bot's open notices as its page reads them, by reason code. Nothing is pressed to read them."""
    return {notice.reason_code: notice for notice in (await _context(account)).uncertainties}


def _sends_the_owner_to_nothing_that_cannot_be_done(*words: str) -> str:
    """No flatten can go out, the Clerk tries nothing again, and no reduction remains available."""
    said = " ".join(words).lower()
    for no_longer_true in ("flatten", "again", "reduction"):
        assert no_longer_true not in said, (no_longer_true, said)
    return said


def _says_only_what_is_true_of_an_expired_contract(*words: str) -> None:
    """The contract by name, that it expired, and no instruction that can no longer be carried out."""
    said = _sends_the_owner_to_nothing_that_cannot_be_done(*words)
    assert SYMBOL.lower() in said and "expired" in said, said


def _transitions(account: _DryRun, kind: str | None = None) -> int:
    """How many records the Clerk has written to this account's books, of one kind or of any."""
    with account.repo._write_lock:
        if kind is None:
            return account.repo._conn.execute("SELECT COUNT(*) FROM custody_transitions").fetchone()[0]
        return account.repo._conn.execute(
            "SELECT COUNT(*) FROM custody_transitions WHERE transition_kind = ?", (kind,)
        ).fetchone()[0]


async def _no_order_can_be_sent_and_nothing_offered_says_otherwise(account: _DryRun) -> None:
    """The Flatten and the write-off are both not offered, each under its own true reason, with nothing pressed."""
    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        flatten = await _offered(account, action_id)
        assert not flatten.available and flatten.reduction_plan is None
        assert flatten.unavailable_reason_code == "OPTION_CONTRACT_EXPIRED"
    write_off = await _offered(account, "discharge_attributed_residue")
    assert not write_off.available and not write_off.needed
    assert write_off.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_IS_NOT_WRITTEN_OFF"
    _says_only_what_is_true_of_an_expired_contract(write_off.unavailable_reason or "", write_off.next_step)


async def _press(account: _DryRun, action_id: str) -> RecoveryExecutionResult:
    """The owner presses one action as the bot's page shows it now."""
    shown = await _offered(account, action_id)

    async def current_context() -> RecoveryPolicyContext:
        return await _context(account)

    return await execute_recovery_action(
        account.clerk,
        request=RecoveryExecutionRequest(
            action_id=action_id, concurrency_token=shown.concurrency_token, execution_ref=shown.execution_ref,
            reason="owner",
        ),
        current_context=current_context,
    )


async def test_a_stopped_bot_the_clerk_gave_up_closing_is_told_its_contract_expired_and_offered_nothing_that_fails(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The common road to an expired position: a worthless contract has no bid.

    Thursday: nothing is bid, the owner stops the bot, the close is refused,
    three retries fail and the Clerk stops trying. Friday the contract
    expires. On Monday the owner opens the bot's page, having pressed
    nothing. Both notices say the contract expired. Neither sends the owner
    to a flatten or says the Clerk will try again. No Flatten and no
    write-off is offered, and what is offered does not fail when pressed:
    a check against the simulation, and the owner's ending of the contract.
    """
    await _stopped_on_thursday_with_nothing_bid(dry_run)
    await _passes(dry_run, 50)  # twelve and a half minutes of Thursday's session
    assert open_exit_stuck(dry_run.repo, SID) is not None, "the Clerk never gave up retrying"
    # Before the expiry the page does send the owner to a flatten: what is asserted absent below was there.
    assert all("flatten" in notice.next_step.lower() for notice in (await _page_notices(dry_run)).values())
    _wait_until(dry_run, MONDAY_NOON)

    await _passes(dry_run, 2)

    notices = await _page_notices(dry_run)
    # The simulation kept no price of the underlying from the last session, so it settled nothing and says so.
    # The pass after the expiry also records the close the contract's last session ended on.
    assert set(notices) == {"EXIT_NOT_FLAT", "EXIT_STUCK", "OPTION_EXPIRED_NOT_SETTLED", "SCHEDULED_CLOSE_MISSED"}
    for notice in notices.values():
        _says_only_what_is_true_of_an_expired_contract(
            notice.headline, notice.explanation, notice.operator_impact, notice.next_step
        )
    status = notices["EXIT_NOT_FLAT"].recovery_status
    assert status is not None and (status.kind, status.reason_code) == ("on_hold", "OPTION_CONTRACT_EXPIRED")
    await _no_order_can_be_sent_and_nothing_offered_says_otherwise(dry_run)
    # Two things the page offers write anything. Reconcile now: pressed, it works, and the page reads
    # the same after it. And the owner's ending of the contract, which the simulation has no price for.
    catalog = build_recovery_catalog(await _context(dry_run))
    assert {item.action_id for item in catalog if item.available and item.mutation} == {
        "reconcile_now", "end_expired_option"
    }
    assert (await _press(dry_run, "reconcile_now")).applied
    await _no_order_can_be_sent_and_nothing_offered_says_otherwise(dry_run)
    # A write-off pressed from a page shown before the expiry is answered "not available", by
    # name and before anything is done. It is never taken up and then failed.
    with pytest.raises(RecoveryActionUnavailableError) as not_available:
        await _press(dry_run, "discharge_attributed_residue")
    assert not_available.value.capability.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_IS_NOT_WRITTEN_OFF"
    await _expired_and_unsold(dry_run, fills=1)


async def test_the_notices_of_an_expired_contract_are_rewritten_once_and_later_passes_write_nothing(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The first pass after the expiry rewrites both notices. Ten more minutes of passes add no record."""
    await _stopped_on_thursday_with_nothing_bid(dry_run)
    await _passes(dry_run, 50)
    assert open_exit_stuck(dry_run.repo, SID) is not None
    rewritten_before = _transitions(dry_run, "UNCERTAINTY_REFRESHED")
    _wait_until(dry_run, MONDAY_NOON)

    await _passes(dry_run, 1)

    assert _transitions(dry_run, "UNCERTAINTY_REFRESHED") == rewritten_before + 2
    # The books' own rows say it, not only the page's reading of them.
    words = _notice_words(dry_run)
    _says_only_what_is_true_of_an_expired_contract(words)
    stuck = open_exit_stuck(dry_run.repo, SID)
    assert stuck is not None
    _says_only_what_is_true_of_an_expired_contract(stuck["headline"], stuck["explanation"], stuck["next_step"])
    written = _transitions(dry_run)
    await _passes(dry_run, 40)
    assert _transitions(dry_run) == written, "a later pass wrote to the books of an expired contract again"
    assert _recovery_says(dry_run) == ("hold", "OPTION_CONTRACT_EXPIRED")


async def test_a_close_still_being_retried_when_its_contract_expired_is_told_as_expired_and_never_as_retried(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The lesser road: the close failed on Thursday and the Clerk had retried it once, not three times.

    Its notice said the Clerk tries the close again, and to flatten once a
    quote with a bid is kept. After the expiry it says neither.
    """
    await _stopped_on_thursday_with_nothing_bid(dry_run)
    await _passes(dry_run, 10)  # past the two minutes a refused close is left to settle: one retry
    assert open_exit_stuck(dry_run.repo, SID) is None, "the Clerk gave up after fewer than three retries"
    # Before the expiry the notice does promise a retry: what is asserted absent below was there.
    assert "again" in _notice_words(dry_run).lower()
    _wait_until(dry_run, MONDAY_NOON)

    await _passes(dry_run, 2)

    notices = await _page_notices(dry_run)
    # The simulation kept no price of the underlying from the last session, so it settled nothing and says so.
    # The pass after the expiry also records the close the contract's last session ended on.
    assert set(notices) == {"EXIT_NOT_FLAT", "OPTION_EXPIRED_NOT_SETTLED", "SCHEDULED_CLOSE_MISSED"}
    for said in notices.values():
        _says_only_what_is_true_of_an_expired_contract(
            said.headline, said.explanation, said.operator_impact, said.next_step
        )
    notice = notices["EXIT_NOT_FLAT"]
    assert notice.recovery_status is not None
    assert (notice.recovery_status.kind, notice.recovery_status.reason_code) == ("on_hold", "OPTION_CONTRACT_EXPIRED")
    await _no_order_can_be_sent_and_nothing_offered_says_otherwise(dry_run)
    await _expired_and_unsold(dry_run)


@pytest.mark.parametrize(
    ("passes_on_thursday", "open_notices"),
    [
        pytest.param(0, {"EXIT_NOT_FLAT"}, id="stopped on Thursday and never passed over again"),
        pytest.param(50, {"EXIT_NOT_FLAT", "EXIT_STUCK"}, id="the Clerk had given up on Thursday"),
    ],
)
async def test_the_page_says_a_contract_expired_from_the_moment_it_did_without_waiting_for_a_pass(
    dry_run: _DryRun, passes_on_thursday: int, open_notices: set[str]  # noqa: F811
) -> None:
    """A stopped Dry Run's Clerk makes no pass on a timer. On Monday the page is read with none made since.

    The notices' own rows are still Thursday's: a close not taken, a retry
    promised or a flatten to press. The page reads each in the expired
    words, the recovery status is the expired hold and not "stuck", and
    neither a Flatten nor a write-off is offered.
    """
    await _stopped_on_thursday_with_nothing_bid(dry_run)
    await _passes(dry_run, passes_on_thursday)
    written = _transitions(dry_run)
    _wait_until(dry_run, MONDAY_NOON)

    notices = await _page_notices(dry_run)

    assert set(notices) == open_notices
    for notice in notices.values():
        _says_only_what_is_true_of_an_expired_contract(
            notice.headline, notice.explanation, notice.operator_impact, notice.next_step
        )
    status = notices["EXIT_NOT_FLAT"].recovery_status
    assert status is not None and (status.kind, status.reason_code) == ("on_hold", "OPTION_CONTRACT_EXPIRED")
    await _no_order_can_be_sent_and_nothing_offered_says_otherwise(dry_run)
    assert _transitions(dry_run) == written, "reading the page wrote to the books"


async def test_a_notice_raised_after_the_expiry_already_says_so_and_no_pass_rewrites_it(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Stopped on the Saturday after the expiry: the refused close raises the notice in the expired words.

    The Clerk's passes find it saying so already. Nothing is rewritten.
    """
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    notice = (await _page_notices(dry_run))["EXIT_NOT_FLAT"]
    _says_only_what_is_true_of_an_expired_contract(
        notice.headline, notice.explanation, notice.operator_impact, notice.next_step
    )
    _wait_until(dry_run, MONDAY_AFTER_THE_OPEN)

    await _passes(dry_run, 12)

    assert _transitions(dry_run, "UNCERTAINTY_REFRESHED") == 0
    assert _recovery_says(dry_run) == ("hold", "OPTION_CONTRACT_EXPIRED")


async def test_a_quote_stamped_after_the_clerks_own_clock_prices_no_close(dry_run: _DryRun) -> None:  # noqa: F811
    """A quote from a minute ahead is not a price the close could have met. The newest one by now is used."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(60_000)
    dry_run.hand_over({SYMBOL: _quote("2.20", "2.26", received_ms=dry_run.clock() - 500)})
    dry_run.hand_over({SYMBOL: _quote("9.00", "9.06", received_ms=dry_run.clock() + 60_000)})

    closed = await dry_run.exit(None)

    assert closed.state is EffectOperationState.FLAT, closed.explanation
    sale = dry_run.clerk.synthesized_option_fills()[closed.child_order_refs[-1]]
    assert (sale.fill_price, sale.quote_age_ms) == (Decimal("2.20"), 500)


async def test_an_exits_quotes_are_named_by_contract_and_one_for_another_contract_is_refused(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(60_000)
    other = "SPY260918C00590000"
    for_another = OptionExitEvidence(
        quotes={other: _quote("7.00", "7.06", received_ms=dry_run.clock() - 300)}, generation=GENERATION,
        read_at_ms=dry_run.clock() - 100,
    )
    orders = dry_run.orders()

    refused = await dry_run.exit(for_another)

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert refused.explanation.startswith("OPTION_EXIT_QUOTES_NOT_THE_HELD_CONTRACT: "), refused.explanation
    assert other in refused.explanation and SYMBOL in refused.explanation
    assert dry_run.orders() == orders and dry_run.held() == {SYMBOL: 1.0}
    assert LEG not in refused.explanation


def test_an_exit_keeps_no_quote_of_a_contract_it_does_not_close() -> None:
    """Beneath the Clerk's own refusal: what an EXIT keeps of the quotes handed with it is its contract's alone."""
    other = "SPY260918C00590000"
    its_own = OptionExitEvidence(
        quotes={SYMBOL: _quote("2.40", "2.46", received_ms=1_000)}, generation=GENERATION, read_at_ms=1_200
    )
    with_another = OptionExitEvidence(
        quotes={SYMBOL: _quote("2.40", "2.46", received_ms=1_000), other: _quote("7.00", "7.06", received_ms=1_000)},
        generation=GENERATION, read_at_ms=1_200,
    )

    kept = OptionExitFacts.of(its_own, closing=SYMBOL)

    assert kept.quote_for(SYMBOL) == its_own.quotes[SYMBOL] and kept.quote_for(other) is None
    with pytest.raises(ValueError, match=other):
        OptionExitFacts.of(with_another, closing=SYMBOL)


# ── after hours: who waits for the open, and who does not ────────────────


async def test_a_dry_runs_own_close_of_an_ended_run_sells_after_hours_at_the_newest_quote_kept(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The owner stops the bot at nine past four. A Dry Run's simulation closes what an ended run left at
    any hour, as it does for shares: at the newest quote kept, and it says how old that quote was."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(4 * 3_600_000 - 31_000)  # 15:59:30
    _hand_over(dry_run, "2.10", "2.16")
    dry_run.wait(9 * 60_000 + 30_000)  # 16:09:00
    await _stop(dry_run)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 2.10, 100)]
    [sale] = [fill for fill in dry_run.clerk.synthesized_option_fills().values() if fill.request.side is OrderSide.SELL]
    assert (sale.fill_model, sale.quote_age_ms) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, 9 * 60_000 + 30_200)


async def test_the_clerks_retry_of_a_failed_option_close_waits_for_the_open_and_its_words_stay_true(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Nothing was bid at five to four, so the close failed. After the close the Clerk holds its retry.

    What it records must not say that nothing closes an option position after
    hours: a Dry Run's own close of an ended run does. A bid kept overnight is
    what the retry sells at, once, a few passes after the next open.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(4 * 3_600_000 - 5 * 60_000)  # 15:55
    _hand_over(dry_run, "0", "0.05")
    await _stop(dry_run)
    await dry_run.clerk.reconcile_once()
    assert dry_run.held() == {SYMBOL: 1.0}
    dry_run.wait(10 * 60_000)  # 16:05
    _hand_over(dry_run, "0.90", "0.96")

    await _passes(dry_run, 4)

    outcome, reason_code, words = _recovery_recorded(dry_run)
    assert (outcome, reason_code) == ("hold", "OPTION_CLOSE_WAITS_FOR_OPEN")
    assert "never" not in words, "the hold claims that nothing closes an option position after hours"
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []

    _wait_until(dry_run, et_minute_of_day_ms(date(2026, 9, 9), 9 * 60 + 31))
    await _passes(dry_run, 12)

    assert dry_run.held() == {}
    assert _sales(dry_run) == [(SYMBOL, "sell", 1.0, 0.90, 100)]


async def test_a_refused_simulated_close_is_not_told_as_a_broker_refusing_an_order(dry_run: _DryRun) -> None:  # noqa: F811
    """No broker was asked: the simulation would not sell for nothing. The notice says what happened."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)

    await dry_run.exit(dry_run.exit_quotes("0", "0.05"))

    words = _notice_words(dry_run)
    assert SYMBOL in words
    assert "broker" not in words.lower()
    # An option position takes no limit price, so the notice does not send the owner to one.
    assert "limit" not in words.lower()


# ── the kept quotes cannot be read ───────────────────────────────────────


async def test_kept_quotes_that_cannot_be_read_are_told_as_unavailable_and_nothing_is_sold_from_them(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A whole line of the file is not a kept quote. Marks, hand-overs and closes each say so by name."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    kept = next((dry_run.artifacts_root / "accounts" / "alpaca").rglob("kept_option_quotes.jsonl"))
    with open(kept, "a", encoding="utf-8") as spoiled:
        spoiled.write('{"seq":99,"symbol":"SPY260918C00585000","quote":"not a quote","kept_at_ms":1}\n')

    with pytest.raises(SimulationEvidenceUnavailable, match=SYMBOL):
        dry_run.account_reading()
    with pytest.raises(KeptOptionQuotesUnavailable):
        _hand_over(dry_run, "2.10", "2.16")
    refused = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert dry_run.held() == {SYMBOL: 1.0} and _sales(dry_run) == []
    assert _notice(dry_run) is not None


# ── what one EXIT closes: one contract, the one its entry opened, on the side that closes it ──

OTHER_CALL = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=590)


async def _holding_a_second_contract(account: _DryRun, monkeypatch: pytest.MonkeyPatch) -> None:
    """Books the Clerk's own doors never make: a second contract bought while the first is held.

    Every ENTER is refused while the bot holds anything, so the refusal is
    taken away for this one entry to put the second contract on the books.
    """
    account.wait(20_000)
    await account.read_account()
    entry = work_out_option_entry(
        BUYS_ONE_CALL, evidence=replace(account.evidence(), picked={LEG: OTHER_CALL}), units=1, now_ms=account.clock()
    )
    with monkeypatch.context() as patched:
        patched.setattr(enter_module, "require_admission", lambda _repo, *, strategy_instance_id: None)
        accepted = accept_option_enter(
            account.repo, account_id=account.repo.account_id, strategy_instance_id=SID, decision_id="enter-second",
            lifecycle_run_id=RUN, entry=entry, envelope=account.clerk.live_envelope,
        )
    await submit_accepted_enter(
        account.repo, accepted=accepted, leg=entry.order,
        trade=_OptionQuoteBoundTradePort(
            account.clerk._trade, terms=entry.contract_terms, quote=entry.the_leg.quote.held
        ),
    )
    assert account.held() == {SYMBOL: 1.0, OTHER_CALL.occ_symbol: 1.0}


async def test_an_exit_of_a_bot_holding_two_contracts_is_refused_by_name_and_never_answered_flat(
    dry_run: _DryRun, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """One EXIT closes one contract. Closing one and answering "flat" would leave the other open unseen."""
    await dry_run.enter(dry_run.evidence())
    await _holding_a_second_contract(dry_run, monkeypatch)
    dry_run.wait(60_000)
    orders = dry_run.orders()

    refused = await dry_run.exit(None)

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert refused.explanation.startswith("OPTION_EXIT_SEVERAL_CONTRACTS_HELD: "), refused.explanation
    assert SYMBOL in refused.explanation and OTHER_CALL.occ_symbol in refused.explanation
    assert dry_run.orders() == orders and _sales(dry_run) == []
    assert dry_run.held() == {SYMBOL: 1.0, OTHER_CALL.occ_symbol: 1.0}


async def _a_finished_exit(account: _DryRun) -> tuple[str, str]:
    """An exit that sold the call: its effect and its closing order, to ask the close's maker about."""
    await account.enter(account.evidence())
    account.wait(60_000)
    closed = await account.exit(account.exit_quotes("2.40", "2.46"))
    reducing_ref = closed.child_order_refs[-1]
    reducing = account.repo.order(reducing_ref)
    assert reducing is not None
    return reducing.effect_operation_id, reducing_ref


async def test_the_close_of_an_exit_is_made_only_of_the_contract_its_own_entry_opened(dry_run: _DryRun) -> None:  # noqa: F811
    """A reduction recorded in another contract is not closed with this entry's: nothing is made of it."""
    effect_id, reducing_ref = await _a_finished_exit(dry_run)
    created = exit_resolution._reducing_order_facts(dry_run.repo, reducing_ref)

    its_own = recorded_option_close(dry_run.repo, effect_id, created=created)
    assert its_own is not None and its_own.request.contract.occ_symbol == SYMBOL

    with pytest.raises(ValueError, match=OTHER_CALL.occ_symbol):
        recorded_option_close(
            dry_run.repo, effect_id, created=replace(created, symbol=OTHER_CALL.occ_symbol)
        )


async def test_a_reduction_that_would_buy_what_the_entry_bought_is_not_made_into_a_close(dry_run: _DryRun) -> None:  # noqa: F811
    """The entry bought the call, so closing it is a sale. Books that ask for a purchase to get flat are a
    short position nobody opened: the close is not made, and nothing opens the other side."""
    effect_id, reducing_ref = await _a_finished_exit(dry_run)
    created = exit_resolution._reducing_order_facts(dry_run.repo, reducing_ref)

    with pytest.raises(ValueError, match="sell"):
        recorded_option_close(dry_run.repo, effect_id, created=replace(created, side="BUY"))


async def test_an_option_close_sent_before_to_a_real_broker_and_met_outside_the_session_is_past_its_session(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Such an order may be at the broker, so its EXIT keeps custody of it and it is never sent again.

    No real account holds an option yet. The rule is the one a scheduled
    end's sale has, asked directly: the hold of an option close counts as the
    session being over, before any send of the order is looked for.
    """
    _effect_id, reducing_ref = await _a_finished_exit(dry_run)
    created = exit_resolution._reducing_order_facts(dry_run.repo, reducing_ref)
    after_hours = dry_run.clock() + 5 * 3_600_000

    assert exit_resolution._past_its_session(
        dry_run.repo, order_ref="an-order-with-no-recorded-send", created=created, valid_until_ms=None,
        now_ms=after_hours, verdict=exit_resolution.option_close_waits_for_open(after_hours),
    )


async def test_a_port_that_is_not_a_simulation_binds_no_quote_to_an_option_close() -> None:
    """A real broker's port has no quotes kept to price a close from. The close is left unsent."""

    class _RealPort:
        broker_id = "alpaca"

    io = ClaimedBrokerIO(repo=_Repo(lose_on_call=0), effect_operation_id="op-1", claim_token="tok", trade=_RealPort())  # type: ignore[arg-type]

    bound = io.bind_option_close_quotes(
        "ref-1", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46", received_ms=1_000)],
        usable_generation=GENERATION,
    )

    assert bound is False


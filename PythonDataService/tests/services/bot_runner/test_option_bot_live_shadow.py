"""An option bot on a Live Shadow, through the real runner and the Shadow's real composition (#2913).

A Live Shadow sends nothing. It applies the rules a real account's Clerk
applies (Alpaca's listing of the contract, the session, the envelope, the
scheduled closes) and fills as a Dry Run fills: at once, from a quote, in
its own order book. The live account is a double that answers a Shadow's
reads and fails the test on any trade call (``_shadow_option_floor``).

The scene is the Dry Run floor's: SPY at 585 on a Tuesday morning, one
bought call expiring Friday, bid $4.08 and offered at $4.12.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk.held_option_quotes import KEEP_EVERY_MS, reset_held_option_quotes_for_testing
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.broker.alpaca.clerk.simulated_option_orders import SimulatedOptionOrders
from app.broker.alpaca.clerk.sqlite import runtime as clerk_runtime
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntrySettlement, EntryStanding, recorded_settlement
from app.broker.alpaca.clerk.sqlite.exit_resolution import OPTION_EXIT_UNPRICED
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionRequest,
    RecoveryExecutionResult,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryPolicyContext, build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.scheduled_end import ScheduledEnd, install_bot_end_schedule
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection, SimulationEvidenceUnavailable
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    SCHEDULED_CLOSE_MISSED_REASON_CODE,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import OrderSide
from app.broker.ibkr.config import reset_settings_for_testing
from app.options.simulated_fill import SimulatedFillModel
from app.schemas.bot_end import BotEnd
from app.schemas.market_liveness import MarketLivenessFact, SymbolTradingStatusEvidence
from app.services import market_liveness
from app.services.market_liveness import unknown_market_liveness
from tests.options import test_resolver as scenes

from ._option_floor import CALL_585, ENTER_MS, EXIT_MS, FIRST_GREEN_MS, MINUTE, Step, green, red
from ._shadow_option_floor import ShadowFloor

CALL = CALL_585.occ_symbol


@pytest.fixture
async def on_a_shadow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Callable[..., ShadowFloor]]:
    """Build a Clerk process with an option bot on a Live Shadow; everything it started is closed after."""
    reset_settings_for_testing()
    reset_held_option_quotes_for_testing()
    market_liveness.reset_market_liveness_store_for_testing()
    built: list[ShadowFloor] = []

    def build(**floor: Any) -> ShadowFloor:
        built.append(ShadowFloor(monkeypatch, tmp_path, **floor))
        return built[-1]

    try:
        yield build
    finally:
        for floor in built:
            floor.lane.close()
            await floor.held.stop()
            if floor.supervisor is not None:
                await floor.supervisor.close()
            await floor.close()
        reset_held_option_quotes_for_testing()
        reset_settings_for_testing()
        market_liveness.reset_market_liveness_store_for_testing()


@pytest.fixture
async def shadow(on_a_shadow: Callable[..., ShadowFloor]) -> ShadowFloor:
    """The usual Shadow floor: the bot listens to SPY and buys one SPY call at the money, with $1,000.00."""
    return await on_a_shadow().open()


@pytest.fixture
def alpaca_is_never_sent_to(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way the real Alpaca port sends, cancels or looks up an order fails the test, and is noted."""
    reached: list[str] = []

    def never(name: str) -> Any:
        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            reached.append(name)
            pytest.fail(f"A Live Shadow option bot reached AlpacaBroker.{name}")

        return refuse

    for name in (
        "submit", "cancel", "submit_option_order", "get_order_by_client_order_id", "get_order_by_broker_order_id",
        "get_option_order_by_client_order_id", "get_option_order_by_broker_order_id",
    ):
        monkeypatch.setattr(AlpacaBroker, name, never(name))
    return reached


def _two_greens() -> list[Step]:
    return [Step(green(FIRST_GREEN_MS)), Step(green(ENTER_MS))]


def _round_trip() -> list[Step]:
    return [*_two_greens(), Step(red(ENTER_MS + MINUTE)), Step(red(ENTER_MS + 2 * MINUTE)), Step(red(EXIT_MS))]


# ---- The whole run: entry, mark, exit, flat, and nothing sent ------------------------------------------


async def test_on_a_live_shadow_an_option_bot_buys_the_call_at_its_ask_marks_it_and_sells_it_at_its_bid(
    shadow: ShadowFloor, alpaca_is_never_sent_to: list[str]
) -> None:
    """The entry is one limit order at the ask, filled inside the send. While it is held the account's
    reading marks it at the bid the Clerk's own keeping stated. The EXIT sells it at the bid. Nothing is
    sent to Alpaca at any point: the live account is read, and that is all."""
    marked: list[Any] = []

    async def two_minutes_on(floor: ShadowFloor) -> None:
        # The call has moved, and its line ticks as time passes: the keeping states its new bid.
        floor.markets[585] = ("4.30", "4.34")
        floor.option_lines_tick = True
        await floor.bar_arrives(ENTER_MS + 2 * MINUTE + 300)
        marked.append(floor.clerk.live_envelope.fresh_observation(floor.clock.value))
        marked.append(floor.watched_as_held())

    script = _round_trip()
    script[3] = Step(script[3].bar, before=two_minutes_on)  # type: ignore[arg-type]

    ran = await shadow.run(script)

    assert [outcome for outcome, _ in ran.outcomes()] == [
        "no_action", "enter_intent", "no_action", "no_action", "exit_intent"
    ]
    # Bought at the ask and sold at the bid the market then showed: a hundred shares a contract.
    assert shadow.fills() == [
        (CALL, "buy", 1.0, 4.12, 100, Decimal("412.00")),
        (CALL, "sell", 1.0, 4.30, 100, Decimal("430.00")),
    ]
    assert shadow.holds() == {}
    bought, sold = shadow.simulated_fills()
    assert (bought.request.side, bought.fill_model, bought.request.limit_price) == (
        OrderSide.BUY, SimulatedFillModel.NATURAL_PRICE, Decimal("4.12")
    )
    # The EXIT handed over the quote its decision could use, so the sale is at that quote's own bid.
    assert (sold.request.side, sold.fill_model, sold.fill_price) == (
        OrderSide.SELL, SimulatedFillModel.NATURAL_PRICE, Decimal("4.30")
    )
    # Marked while held: $4.30 bid against $4.12 paid is $18.00 on one contract, less nothing yet.
    observation, named_by_the_run = marked
    assert observation is not None and observation.unrealized_pl_usd == Decimal("18.00")
    assert observation.position_count == 1
    # The held contract's line is the Clerk's own keeping's to ask for, not the run's.
    assert named_by_the_run == ()
    # The entry was answered on the first read of its outcome, and recorded.
    assert shadow.transitions("ENTRY_SETTLED") == 1 and shadow.outcome_reads_paused == 0
    # Alpaca was asked for its listing of the contract, once, and for nothing else about an order.
    assert shadow.live.listing_reads == [CALL]
    assert shadow.live.trade_calls == [] and alpaca_is_never_sent_to == []
    assert shadow.asked_of_the_clerk == ["ENTER", "EXIT"]
    # The Shadow's own pass finds its books and its order book agreeing, and its fees are a model's.
    assert (await shadow.a_pass()).verdict == "clean"
    with shadow.repo._write_lock:
        fees = custody_fee_attribution(
            shadow.repo._conn, now_ms=shadow.clock.value, simulated_fill_cutoff_ms=shadow.clock.value
        )
    assert fees.known and sum(share.amount for share in fees.shares) > 0


# ---- An entry that cannot fill -------------------------------------------------------------------------


async def test_an_entry_the_book_cannot_fill_ends_on_the_spot_is_answered_on_the_first_read_and_the_next_signal_enters(
    shadow: ShadowFloor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Shadow's book cannot rest an order. One priced above its limit ends unfilled inside the send. The
    runner asks what the entry came to and is answered on the first read, with no pause: the decision is
    dropped, and the next green bar is an ENTER again, which fills."""
    price = SimulatedOptionOrders._simulated_option_fill
    priced: list[Any] = []

    def above_the_limit_the_first_time(self: Any, order: Any, bound: Any, *, at_ms: int) -> Any:
        fill = price(self, order, bound, at_ms=at_ms)
        priced.append(order)
        return fill._replace(price=fill.price + Decimal("0.05")) if len(priced) == 1 else fill

    monkeypatch.setattr(SimulatedOptionOrders, "_simulated_option_fill", above_the_limit_the_first_time)

    ran = await shadow.run([*_two_greens(), Step(green(ENTER_MS + MINUTE))])

    assert [outcome for outcome, _ in ran.outcomes()] == ["no_action", "enter_intent", "enter_intent"]
    assert shadow.outcome_reads_paused == 0 and shadow.transitions("ENTRY_SETTLED") == 2
    first, second = [recorded_settlement(shadow.repo, order_ref) for order_ref in _entry_refs(shadow)]
    assert first is not None and (first.settlement, first.standing, first.units_filled) == (
        EntrySettlement.DISCARDED, EntryStanding.UNFILLED, 0
    )
    assert second is not None and (second.settlement, second.standing) == (
        EntrySettlement.ENTERED, EntryStanding.FILLED
    )
    assert shadow.holds() == {CALL: 1.0} and len(shadow.fills()) == 1
    assert shadow.live.trade_calls == []


def _entry_refs(shadow: ShadowFloor) -> list[str]:
    with shadow.repo._write_lock:
        rows = shadow.repo._conn.execute("SELECT order_ref FROM orders WHERE role = 'ENTRY' ORDER BY rowid").fetchall()
    return [row[0] for row in rows]


# ---- Alpaca's listing: a Shadow applies the Live Clerk's refusal -----------------------------------------


async def test_a_contract_alpaca_does_not_list_is_refused_on_a_shadow_by_name_and_nothing_is_filled(
    shadow: ShadowFloor,
) -> None:
    """A Live bot's order in a contract Alpaca does not list would be refused. Its Shadow asks the same
    listing, through its read of the live account, and refuses the entry the same way."""
    shadow.live.listed = {}

    ran = await shadow.run(_two_greens())

    [(reason_code, words)] = ran.blocked()
    assert reason_code == "OPTION_CONTRACT_NOT_LISTED" and CALL in words
    assert shadow.live.listing_reads == [CALL]
    assert shadow.holds() == {} and shadow.simulated_fills() == [] and _entry_refs(shadow) == []


async def test_a_listing_that_cannot_be_read_refuses_a_shadows_entry_for_the_listing(shadow: ShadowFloor) -> None:
    shadow.live.listing_error = BrokerUnavailable("Alpaca did not answer.", broker="alpaca")

    ran = await shadow.run(_two_greens())

    [(reason_code, _words)] = ran.blocked()
    assert reason_code == OptionEffectRefusal.CONTRACT_REFERENCE_UNREAD.value
    assert shadow.holds() == {} and shadow.simulated_fills() == []


# ---- Held with no runner: the Clerk's own closes --------------------------------------------------------

EXPIRY_DAY = scenes._FRIDAY
# 45 minutes before the four o'clock close of a full day.
DUE = scenes._at(EXPIRY_DAY, 15, 15)


async def _holding_one_call(shadow: ShadowFloor) -> None:
    await shadow.run(_two_greens())
    assert shadow.holds() == {CALL: 1.0}


async def _the_call_is_quoted(shadow: ShadowFloor, bid: str, ask: str) -> None:
    """IBKR quotes the held call now. The Clerk's keeping states a held contract's quote every quarter of a
    minute, so that much later this one is the newest kept for it, whoever is or is not running."""
    shadow.quote(585, bid, ask)
    await shadow.time_passes_to(shadow.clock.value + KEEP_EVERY_MS)


async def test_a_stopped_shadow_run_keeps_its_contract_and_the_clerk_goes_on_asking_for_its_line(
    shadow: ShadowFloor,
) -> None:
    """Only a Dry Run is closed as its run ends. A Shadow rehearses a Live bot, whose Stop sells nothing: the
    contract stays held, and its quote line stays asked for from the Shadow's own books."""
    await _holding_one_call(shadow)

    await shadow.stop_is_pressed()
    await shadow.time_passes_to(shadow.clock.value + 30_000)
    await shadow.a_pass()

    assert shadow.holds() == {CALL: 1.0}
    assert shadow.held.held() == {shadow.repo.account_id: (CALL,)}
    assert [contract.occ_symbol for request in shadow.held.requests() for contract in request.held] == [CALL]
    # The mark is still stated with nobody running: the newest whole quote kept is seconds old.
    newest = shadow.kept_quotes().stated_mark(CALL, at_or_before_ms=shadow.clock.value)
    assert newest is not None and shadow.clock.value - newest.kept_at_ms <= 15_000


async def test_on_its_expiry_day_a_shadows_contract_is_closed_by_the_clerks_keeper_at_the_close_lead(
    shadow: ShadowFloor,
) -> None:
    """The close an option position owes on its expiry day is the Clerk's own, 45 minutes before the bell,
    whatever its run is doing. The Shadow's keeper takes it on time, and the book fills it from the newest
    quote kept for the contract."""
    await _holding_one_call(shadow)
    await shadow.hours_pass_to(DUE - MINUTE)
    await _the_call_is_quoted(shadow, "2.10", "2.16")

    early = await shadow.keeper.tick()
    assert early.closes.taken == () and shadow.holds() == {CALL: 1.0}

    await shadow.time_passes_to(DUE)
    done = await shadow.keeper.tick()

    assert len(done.closes.taken) == 1 and done.failed == ()
    assert shadow.holds() == {}
    assert shadow.fills()[-1] == (CALL, "sell", 1.0, 2.10, 100, Decimal("210.00"))
    assert shadow.live.trade_calls == []
    # Asked again, nothing more is sold.
    again = await shadow.keeper.tick()
    assert again.closes.taken == () and len(shadow.fills()) == 2


async def test_with_no_keeper_round_the_shadows_own_pass_closes_the_contract_on_its_expiry_day(
    shadow: ShadowFloor,
) -> None:
    """A keeper that died costs one pass and never the close: the reconciliation pass runs the same step."""
    await _holding_one_call(shadow)
    await shadow.stop_is_pressed()
    await shadow.hours_pass_to(DUE + 1_000)
    await _the_call_is_quoted(shadow, "2.10", "2.16")

    await shadow.a_pass()

    assert shadow.holds() == {}
    assert shadow.fills()[-1] == (CALL, "sell", 1.0, 2.10, 100, Decimal("210.00"))
    assert shadow.live.trade_calls == []


async def test_a_close_owed_with_nothing_bid_is_not_made_and_the_session_that_ends_on_it_is_recorded_as_missed(
    shadow: ShadowFloor,
) -> None:
    """A Shadow invents no fill. With nothing bid for the call, the scheduled close is not sent, and when the
    session ends the notice says so in a Shadow's own words: nothing real is held."""
    await _holding_one_call(shadow)
    await shadow.hours_pass_to(DUE)
    await _the_call_is_quoted(shadow, "0", "0.05")

    await shadow.keeper.tick()
    assert shadow.holds() == {CALL: 1.0}
    # What the owner can do about it is a simulation's: wait for a quote with something bid. A real
    # account's close needs no quote of ours, and is told otherwise.
    not_flat = shadow.repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=shadow.sid
    )
    assert not_flat is not None and "quote" in json.loads(not_flat["facts_json"])["next_step"]

    await shadow.hours_pass_to(scenes._at(EXPIRY_DAY, 16, 0) + 1_000)
    await shadow.keeper.tick()

    notice = shadow.repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=SCHEDULED_CLOSE_MISSED_REASON_CODE, strategy_instance_id=shadow.sid
    )
    assert notice is not None
    words = json.loads(notice["facts_json"])["explanation"]
    assert "This is a Live Shadow: nothing real is held." in words and "Alpaca" not in words
    assert shadow.holds() == {CALL: 1.0}


# ---- The owner's own closes ----------------------------------------------------------------------------


async def _flatten(shadow: ShadowFloor) -> RecoveryExecutionResult:
    """The owner presses Reconcile now and then Flatten, as the bot's page does."""
    await shadow.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    presented = await _offered(shadow, "execute_safe_flatten")
    assert presented.available, (presented.unavailable_reason_code, presented.unavailable_reason)

    async def current_context() -> RecoveryPolicyContext:
        return await _context(shadow)

    return await execute_recovery_action(
        shadow.clerk,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten", concurrency_token=presented.concurrency_token,
            execution_ref=presented.execution_ref, reason="owner-flatten", confirmed_limit=None,
        ),
        current_context=current_context,
    )


async def _context(shadow: ShadowFloor) -> RecoveryPolicyContext:
    context = await read_recovery_context(shadow.clerk, strategy_instance_id=shadow.sid)
    assert context is not None
    return context


async def _offered(shadow: ShadowFloor, action_id: str) -> RecoveryCapability:
    return next(item for item in build_recovery_catalog(await _context(shadow)) if item.action_id == action_id)


async def test_the_owners_flatten_of_a_stopped_shadow_bot_sells_its_contract_at_the_newest_bid_kept(
    shadow: ShadowFloor,
) -> None:
    await _holding_one_call(shadow)
    await shadow.stop_is_pressed()
    await shadow.time_passes_to(shadow.clock.value + 2 * MINUTE)
    await _the_call_is_quoted(shadow, "4.20", "4.26")

    await _flatten(shadow)

    assert shadow.holds() == {}
    assert shadow.fills()[-1] == (CALL, "sell", 1.0, 4.20, 100, Decimal("420.00"))
    assert shadow.live.trade_calls == []


async def test_a_flatten_of_a_shadows_contract_nobody_bids_for_is_not_offered(shadow: ShadowFloor) -> None:
    """The book could not price it, so the owner is told before any EXIT is made that could never be sent."""
    await _holding_one_call(shadow)
    await shadow.stop_is_pressed()
    await shadow.time_passes_to(shadow.clock.value + 2 * MINUTE)
    await _the_call_is_quoted(shadow, "0", "0.05")

    await shadow.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    offered = await _offered(shadow, "execute_safe_flatten")

    assert not offered.available and offered.reduction_plan is None
    assert offered.unavailable_reason_code == OPTION_EXIT_UNPRICED
    assert shadow.holds() == {CALL: 1.0}


class _EndsOnTheBooks:
    """The runner's side of a bot's owner-set end, as the Clerk reads it."""

    def __init__(self, end: ScheduledEnd) -> None:
        self.ends = {end.strategy_instance_id: end}
        self.carried_out: list[ScheduledEnd] = []

    def pending_ends(self, strategy_instance_ids: Any) -> list[ScheduledEnd]:
        return [self.ends[sid] for sid in strategy_instance_ids if sid in self.ends]

    def stop_bot_at_its_end(self, strategy_instance_id: str, lifecycle_run_id: str) -> None:
        del strategy_instance_id, lifecycle_run_id

    def record_end_carried_out(self, end: ScheduledEnd, *, at_ms: int) -> None:
        del at_ms
        self.carried_out.append(end)
        self.ends.pop(end.strategy_instance_id)


async def test_at_the_end_the_owner_set_to_sell_a_shadows_run_is_stopped_and_its_contract_is_sold(
    shadow: ShadowFloor,
) -> None:
    """A run's end closes what it holds when the owner chose SELL for it, on a Shadow as on a real account."""
    await _holding_one_call(shadow)
    end_at_ms = shadow.clock.value + 5 * MINUTE
    schedule = _EndsOnTheBooks(
        ScheduledEnd(strategy_instance_id=shadow.sid, end=BotEnd(end_at_ms=end_at_ms, end_action="SELL"))
    )
    install_bot_end_schedule(schedule)
    try:
        await shadow.time_passes_to(end_at_ms + 1_000)
        await _the_call_is_quoted(shadow, "4.20", "4.26")
        await shadow.a_pass()
        await shadow.a_pass()
    finally:
        install_bot_end_schedule(None)

    assert shadow.repo.active_run(shadow.sid) is None
    assert shadow.holds() == {}
    assert shadow.fills()[-1] == (CALL, "sell", 1.0, 4.20, 100, Decimal("420.00"))
    assert [end.strategy_instance_id for end in schedule.carried_out] == [shadow.sid]
    assert shadow.live.trade_calls == []


# ---- What a Shadow still refuses -----------------------------------------------------------------------


async def test_a_plan_of_several_legs_is_refused_by_name_as_a_shadow_bot_is_registered(
    on_a_shadow: Callable[..., ShadowFloor],
) -> None:
    floor = await on_a_shadow(plan=scenes._spread()).open(register=False)

    with pytest.raises(OptionPlanRefused) as refused:
        await floor.clerk.register_strategy_run(floor.binding)

    assert refused.value.reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED
    assert "2 option legs" in refused.value.detail
    assert floor.repo.strategy_instance(floor.sid) is None


# ---- After a restart -----------------------------------------------------------------------------------


async def test_after_the_clerk_restarts_the_shadow_still_holds_its_contract_marks_it_and_its_exit_sells_it(
    shadow: ShadowFloor,
) -> None:
    """The book, the kept quotes and the Clerk's books are files. A new process finds the contract held,
    follows its books again, and a later run's EXIT sells it."""
    await _holding_one_call(shadow)

    await shadow.the_clerk_restarts()
    await shadow.time_passes_to(shadow.clock.value + 30_000)
    await _the_call_is_quoted(shadow, "4.20", "4.26")
    await shadow.account_is_read()

    assert shadow.holds() == {CALL: 1.0}
    assert [position.symbol for position in await shadow.clerk._read.list_positions()] == [CALL]
    observation = shadow.clerk.live_envelope.fresh_observation(shadow.clock.value)
    assert observation is not None and observation.unrealized_pl_usd == Decimal("8.00")
    assert shadow.held.held() == {shadow.repo.account_id: (CALL,)}


# ---- A contract held past its expiry: what a Shadow does until expiries are settled ----------------------


async def test_a_contract_a_shadow_still_holds_after_its_expiry_is_told_as_it_is_and_no_write_off_is_offered(
    shadow: ShadowFloor,
) -> None:
    """Settling an expired contract is not built for a simulation yet. Until it is, a Shadow that still holds
    one says so: the close it owed is recorded missed, no order is sent for it, its account's reading is
    unavailable, and the write-off is not offered, because the Shadow's own order book still holds the
    contract. Nothing real is held, and the words say that."""
    await _holding_one_call(shadow)
    await shadow.stop_is_pressed()
    await shadow.hours_pass_to(scenes._at(EXPIRY_DAY, 16, 0) + MINUTE)

    await shadow.a_pass()
    reading = await shadow.runtime.envelope_sync.tick()  # type: ignore[union-attr]
    await shadow.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    write_off = await _offered(shadow, "discharge_attributed_residue")
    flatten = await _offered(shadow, "execute_safe_flatten")

    assert shadow.holds() == {CALL: 1.0} and len(shadow.fills()) == 1
    assert reading != "observed"
    assert shadow.clerk.live_envelope.fresh_observation(shadow.clock.value + 60_000) is None
    notice = shadow.repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=SCHEDULED_CLOSE_MISSED_REASON_CODE, strategy_instance_id=shadow.sid
    )
    assert notice is not None and "This is a Live Shadow: nothing real is held." in notice["facts_json"]
    assert not write_off.available and write_off.unavailable_reason_code == "BROKER_AGREES_WITH_CUSTODY"
    assert "Live Shadow's order book" in (write_off.unavailable_reason or "")
    assert not flatten.available
    assert shadow.live.trade_calls == []


# ---- A real account's send rules, a simulation's price -------------------------------------------------


async def test_a_halt_of_the_underlying_holds_a_shadows_close_and_the_pass_sells_it_when_the_halt_lifts(
    shadow: ShadowFloor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Live bot's close is not sent into a halt, so its Shadow's is not filled in one: the book would have
    priced it, and the Clerk holds it all the same. The halt is asked of the underlying."""
    await _holding_one_call(shadow)
    await shadow.stop_is_pressed()
    await shadow.hours_pass_to(DUE)
    await _the_call_is_quoted(shadow, "2.10", "2.16")
    halted, asked = {"SPY"}, []

    def status(symbol: str, now_ms: int) -> MarketLivenessFact:
        asked.append(symbol)
        fact = unknown_market_liveness(symbol, observed_at_ms=now_ms)
        if symbol not in halted:
            return fact
        return fact.model_copy(update={
            "state": "HALTED",
            "symbol_status": SymbolTradingStatusEvidence(
                symbol=symbol, state="HALTED", source="ibkr.market_data.status", observed_at_ms=now_ms,
            ),
        })

    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", status)

    await shadow.a_pass()

    assert shadow.holds() == {CALL: 1.0} and len(shadow.fills()) == 1
    assert asked and set(asked) == {"SPY"}

    halted.clear()
    await shadow.time_passes_to(shadow.clock.value + 30_000)
    await shadow.a_pass()

    assert shadow.holds() == {}
    assert shadow.fills()[-1] == (CALL, "sell", 1.0, 2.10, 100, Decimal("210.00"))


async def test_kept_quotes_that_cannot_be_read_leave_a_shadow_with_no_reading_and_say_whose_they_are(
    shadow: ShadowFloor,
) -> None:
    """A whole line of the shadow account's file is not a kept quote. The contract cannot be marked, the
    account's reading says the Live Shadow's quotes need repair, and the EXIT sells nothing from them."""
    await _holding_one_call(shadow)
    with open(shadow.kept_quotes().path, "a", encoding="utf-8") as spoiled:
        spoiled.write(f'{{"seq":99,"symbol":"{CALL}","quote":"not a quote","kept_at_ms":1}}\n')
    reading = SimulatedAccountProjection(repo=shadow.repo, artifacts_root=shadow.root)

    with pytest.raises(SimulationEvidenceUnavailable, match="Live Shadow") as unreadable:
        reading.observe(reference_cash=100_000, observed_at_ms=shadow.clock.value, now_ms=shadow.clock.value)

    assert CALL in str(unreadable.value)
    assert not shadow.clerk._trade.recovery_price_available(CALL, side="sell")
    assert shadow.holds() == {CALL: 1.0}

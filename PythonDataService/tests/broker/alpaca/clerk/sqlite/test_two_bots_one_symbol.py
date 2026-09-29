"""Two bots trading one symbol in one Alpaca account (research #2469).

What the Clerk does today when two strategy instances hold and trade the same
symbol in one account. Findings note:
``docs/references/two-bots-one-symbol-2469.md``.

Passing tests pin current behaviour. Every fixture comes from the existing
Clerk suites (``conftest``, ``test_budget_commands``,
``test_envelope_reservations``, ``test_exit``, ``test_exit_send_session``,
``test_reconcile``); the only new object is Alpaca's wash-trade rejection,
built the way alpaca-py raises it.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLeg
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter, submit_accepted_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import DEFAULT_RECOVERY_INTERVAL_MS
from app.broker.alpaca.clerk.sqlite.facts import OrderSubmitFailedFacts
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryStatus
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import plan_account_reconciliation, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    ReductionIntent,
    decide_capability,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
)
from app.broker.alpaca.errors import map_api_error
from app.broker.alpaca.marketable_limit import marketable_limit_price
from app.broker.contract.errors import BrokerError, BrokerOrderRejected
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    NOON,
    _FakeTradePort,
    _fill_entry,
    _make_held_position,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice
from tests.broker.alpaca.clerk.sqlite.test_exit import POST_CLOSE_MS
from tests.broker.alpaca.clerk.sqlite.test_exit_send_session import _live_touch
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    WATCHDOG_POST_T0,
    WATCHDOG_RUN,
    WATCHDOG_SID,
    _broker_order,
    _exit_not_flat_redrive_policy,
    _FakeRead,
    _FakeTrade,
    _held_position,
    _leg,
    _position,
    _register_second_spy_lane,
    clocked_repo,  # noqa: F401 -- pytest fixture, used by name
)

# Alpaca's documented wash-trade refusal: HTTP 403 on POST /v2/orders
# (https://docs.alpaca.markets/us/docs/user-protection). The page states only
# the status. The message is the one quoted in the title of an Alpaca community
# forum thread; Alpaca documents no numeric code for this refusal, so the code
# below is illustrative. The Clerk maps on the status alone, keeps the code, and
# names another open order only from its own records (#2621).
_ILLUSTRATIVE_CODE = 40310000
_WASH_TRADE_MESSAGE = "potential wash trade detected. use complex orders"
_ET_0400_NEXT_MORNING = et_minute_of_day_ms(date(2023, 11, 16), 4 * 60)
"""The first instant after ``WATCHDOG_POST_T0``'s session that an exit allowance can price."""


def _wash_trade_rejection(code: int | None = _ILLUSTRATIVE_CODE) -> BrokerError:
    """The error the Clerk's broker port raises for the rejection (``client.submit_order``).

    Built the way alpaca-py raises it, with ``code`` (or no code) in the body.
    """
    body = {"message": _WASH_TRADE_MESSAGE} if code is None else {"code": code, "message": _WASH_TRADE_MESSAGE}
    response = SimpleNamespace(status_code=403, headers={})
    raw = APIError(json.dumps(body), http_error=SimpleNamespace(response=response, request=None))
    return map_api_error(raw, broker="alpaca", is_order_mutation=True)


# ── Attribution: whose shares are whose ───────────────────────────────────────


async def test_same_symbol_positions_stay_per_bot_while_reconciliation_sees_one_netted_position(
    clocked_repo,  # noqa: F811 -- the imported fixture
) -> None:
    """Each bot owns its own fills; the broker reports one net SPY position.

    Reconciliation can only compare the broker's net quantity with the sum of
    the bots' quantities. When they differ it knows the symbol drifted, never
    which bot's shares went missing.
    """
    repo, _clock = clocked_repo
    sid_b, run_b = _register_second_spy_lane(repo)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                              run_id=WATCHDOG_RUN, decision_id="a-buy", execution_id="a-exec", quantity=3)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b,
                              run_id=run_b, decision_id="b-buy", execution_id="b-exec", quantity=5)

    assert repo.position(WATCHDOG_SID, "SPY") == 3
    assert repo.position(sid_b, "SPY") == 5
    assert repo.attributed_positions_by_subject() == {
        (f"bot:{WATCHDOG_SID}", "SPY"): 3, (f"bot:{sid_b}", "SPY"): 5,
    }

    def verdict(broker_quantity: float) -> tuple[str, tuple[str, ...]]:
        plan = plan_account_reconciliation(
            namespaces=frozenset(), broker_orders=[],
            broker_positions=[_position("SPY", quantity=broker_quantity)],
            attributed_positions=repo.attributed_positions_by_symbol(),
            known_order_refs=repo.all_order_refs(),
        )
        return plan.verdict, plan.drifted_symbols

    assert verdict(8) == ("clean", ())
    # Three shares sold outside the Clerk: the account drifts; no bot is named.
    assert verdict(5) == ("position_drift", ("SPY",))


async def test_each_bots_exit_sells_only_its_own_attributed_shares(
    clocked_repo,  # noqa: F811
) -> None:
    """Bot A's EXIT sells A's 3 shares out of the broker's 8, never B's 5."""
    repo, _clock = clocked_repo
    sid_b, run_b = _register_second_spy_lane(repo)
    ref_a = await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                                      run_id=WATCHDOG_RUN, decision_id="a-buy", execution_id="a-exec", quantity=3)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b,
                              run_id=run_b, decision_id="b-buy", execution_id="b-exec", quantity=5)
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    trade = _FakeTradePort()

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade,
                       pricing=UNPRICEABLE_RECOVERY)

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [(OrderSide.SELL, 3)]


async def test_shares_sold_outside_the_clerk_strand_whichever_bot_exits_second(
    clocked_repo,  # noqa: F811
) -> None:
    """The broker holds 5 SPY; A holds 3 and B holds 5 by attribution.

    Under ``POSITION_DRIFT`` a reduction is allowed only if it moves both the
    broker quantity and the attributed sum toward zero without crossing.
    Either bot may go first. After A's 3 shares sell, the broker holds 2 and
    B's 5-share EXIT would cross zero, so B is refused, although nobody knows
    whose shares the outside sale took.
    """
    repo, _clock = clocked_repo
    sid_b, run_b = _register_second_spy_lane(repo)
    ref_a = await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                                      run_id=WATCHDOG_RUN, decision_id="a-buy", execution_id="a-exec", quantity=3)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b,
                              run_id=run_b, decision_id="b-buy", execution_id="b-exec", quantity=5)

    def may_sell(sid: str, quantity: float) -> bool:
        return decide_capability(repo, capability=Capability.REDUCE, strategy_instance_id=sid,
                                 reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=quantity)).allowed

    await reconcile_account(repo, read=_FakeRead(positions=[_position("SPY", quantity=5.0)]),
                            trade=_FakeTrade(), pricing=UNPRICEABLE_RECOVERY)
    assert may_sell(WATCHDOG_SID, 3) and may_sell(sid_b, 5)

    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    sent = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(),
                              pricing=UNPRICEABLE_RECOVERY)
    assert sent.reducing_order_ref is not None
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=_broker_order(
        sent.reducing_order_ref, order_id="bo-a-sell", side="sell", status="filled", quantity=3.0,
        filled_quantity=3.0, filled_avg_price=100.0))
    assert repo.position(WATCHDOG_SID, "SPY") == 0
    await reconcile_account(repo, read=_FakeRead(positions=[_position("SPY", quantity=2.0)]),
                            trade=_FakeTrade(), pricing=UNPRICEABLE_RECOVERY)

    assert not may_sell(sid_b, 5)
    assert may_sell(sid_b, 2)


@pytest.mark.parametrize("anchor", ["0.5731", "99.99", "452.18"])
@pytest.mark.parametrize(("entry_bps", "exit_bps"), [("0", "0"), ("5", "5"), ("20", "50")])
def test_opposite_extended_hours_legs_on_one_decision_bar_are_always_a_wash_trade_pair(
    anchor: str, entry_bps: str, exit_bps: str,
) -> None:
    """Alpaca rejects a limit buy against an open limit sell when buy limit >= sell limit.

    Outside the regular session every program leg is a marketable limit on the
    decision bar's close: a buy rounds up from it, a sell rounds down. Two bots
    that decide opposite sides on the same bar therefore always meet that
    condition, so whichever order reaches Alpaca second is refused.
    """
    close = Decimal(anchor)
    buy = marketable_limit_price(side=OrderSide.BUY, anchor=close, allowance_bps=Decimal(entry_bps))
    sell = marketable_limit_price(side=OrderSide.SELL, anchor=close, allowance_bps=Decimal(exit_bps))

    assert buy >= close >= sell


def _fill(sid: str, side: OrderSide, price: float, at_ms: int) -> FillRecord:
    return FillRecord(account_id="PA-TEST", sid=sid, intent_id=f"i{at_ms}",
                      order_ref=f"learn-ai/{sid}/v1:i{at_ms}", event_key=f"e{at_ms}", symbol="SPY",
                      side=side, quantity=1, fill_price=price, filled_at_ms=at_ms, fee=0)


def test_account_fifo_lets_one_bots_sale_close_the_other_bots_lot() -> None:
    """Bot results use per-bot FIFO; the account view runs one FIFO over both bots.

    A buys 1 at 100, B buys 1 at 200, B sells 1 at 210. Per bot, B realizes
    +10 and A still holds its 100 lot. Across the account, B's sale closes A's
    older lot for +110 and B's 200 lot stays open. Realized P&L differs by
    100; realized plus open P&L at any mark is the same. Exact Decimal money:
    no tolerance.
    """
    fills = [_fill("a", OrderSide.BUY, 100, 1), _fill("b", OrderSide.BUY, 200, 2), _fill("b", OrderSide.SELL, 210, 3)]
    mark = {"SPY": 205.0}
    per_bot = [compute_fifo_pnl([f for f in fills if f.sid == sid], mark_prices=mark) for sid in ("a", "b")]
    account = compute_fifo_pnl(fills, mark_prices=mark)

    assert [bot.exact_realized_pnl for bot in per_bot] == [Decimal(0), Decimal(10)]
    assert account.exact_realized_pnl == Decimal(110)
    [crossed] = account.closed_lots
    assert (crossed.entry_strategy_instance_id, crossed.exit_strategy_instance_id) == ("a", "b")
    per_bot_total = sum(bot.exact_realized_pnl for bot in per_bot) + Decimal(str(sum(bot.open_pnl for bot in per_bot)))
    assert per_bot_total == account.exact_realized_pnl + Decimal(str(account.open_pnl)) == Decimal(115)


# ── Alpaca's wash-trade refusal ───────────────────────────────────────────────


def test_a_wash_trade_refusal_is_an_order_rejection_that_keeps_alpacas_code() -> None:
    """A 403 on an order is Alpaca refusing that order, not our credentials (#2621)."""
    error = _wash_trade_rejection()

    assert isinstance(error, BrokerOrderRejected)
    assert error.message == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    assert error.detail == "HTTP 403"
    assert error.code == _ILLUSTRATIVE_CODE


async def test_an_enter_refused_as_a_wash_trade_fails_and_the_bot_may_enter_again(tmp_path: Path) -> None:
    """The refusal is definitive: the ENTER folds failed, nothing is left uncertain.

    Its record keeps Alpaca's code. No other order is open on SPY, so the
    record repeats only Alpaca's own words and never names another order.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "b", 50_000)
        leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-1",
                                lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())

        result = await submit_accepted_enter(repo, accepted=accepted, leg=leg,
                                             trade=_FakeTradePort(submit_error=_wash_trade_rejection()))

        effect = repo.effect_operation(result.effect_operation_id)
        assert effect is not None and effect.state == "failed"
        assert repo.uncertain_orders() == []
        [failed] = [row for row in repo.transitions_for_order(accepted.order_ref)
                    if row["transition_kind"] == "ORDER_SUBMIT_FAILED"]
        facts = OrderSubmitFailedFacts.from_facts_json(failed["facts_json"])
        assert facts.broker_error_code == _ILLUSTRATIVE_CODE
        assert facts.why == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
        again = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-2",
                             lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())
        assert again.created
    finally:
        repo.close()


def _register_spy_lane(repo: ClerkSqliteRepository, sid: str) -> str:
    run_id = f"{sid}-run"
    repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"{sid}-h")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=run_id)
    return run_id


def test_a_refusals_facts_omit_an_absent_code_so_every_earlier_row_hashes_the_same() -> None:
    """``broker_error_code`` joins the hash-chained facts only when Alpaca gave one.

    Every row written before #2621 -- and every failure no broker answer
    caused -- re-serializes byte-identically, so no sealed receipt moves.
    """
    plain = OrderSubmitFailedFacts(reason="The order did not reach the broker.", why="Alpaca refused the order: x")
    earlier_row = '{"reason":"The order did not reach the broker.","why":"Alpaca refused the order: x"}'
    coded = OrderSubmitFailedFacts(
        reason="The order did not reach the broker.", why="Alpaca refused the order: x", broker_error_code=40310000,
    )

    assert plain.to_facts_json() == earlier_row
    assert OrderSubmitFailedFacts.from_facts_json(earlier_row) == plain
    assert coded.to_facts_json() == (
        '{"broker_error_code":40310000,"reason":"The order did not reach the broker.",'
        '"why":"Alpaca refused the order: x"}'
    )
    assert OrderSubmitFailedFacts.from_facts_json(coded.to_facts_json()) == coded


async def _a_sells(repo: ClerkSqliteRepository, *, fills: bool) -> None:
    """A holds 10 SPY and sends its 10-share sell, which Alpaca accepts and then fills or leaves working."""
    ref_a = await _held_position(repo)
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)
    sent = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(),
                              pricing=UNPRICEABLE_RECOVERY)
    assert sent.reducing_order_ref is not None
    if fills:
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=_broker_order(
            sent.reducing_order_ref, order_id="bo-a-sell", side="sell", status="filled", quantity=10.0,
            filled_quantity=10.0, filled_avg_price=100.0))


async def _refused_enter_facts(
    repo: ClerkSqliteRepository, sid: str, run_id: str, refusal: BrokerError,
) -> OrderSubmitFailedFacts:
    """``sid`` sends a 5-share SPY buy that Alpaca refuses with ``refusal``; its durable record."""
    result = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id=f"{sid}-buy",
                                lifecycle_run_id=run_id, leg=_leg(quantity=5),
                                trade=_FakeTradePort(submit_error=refusal))
    assert result.effect_operation_id is not None and result.order_ref is not None
    effect = repo.effect_operation(result.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    [failed] = [row for row in repo.transitions_for_order(result.order_ref)
                if row["transition_kind"] == "ORDER_SUBMIT_FAILED"]
    return OrderSubmitFailedFacts.from_facts_json(failed["facts_json"])


async def test_an_enter_refused_while_another_bots_sell_is_open_names_that_order(
    clocked_repo,  # noqa: F811
) -> None:
    """The Clerk's own records show A's sell working when B's buy is refused, so the record says so."""
    repo, _clock = clocked_repo
    await _a_sells(repo, fills=False)
    run_b = _register_spy_lane(repo, "b")

    facts = await _refused_enter_facts(repo, "b", run_b, _wash_trade_rejection())

    assert facts.broker_error_code == _ILLUSTRATIVE_CODE
    assert facts.why == (
        "This account still had an open SPY sell order when Alpaca refused this buy; "
        "Alpaca refuses an order that could trade against another open order in the same account. "
        f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    )


@pytest.mark.parametrize("code", [_ILLUSTRATIVE_CODE, 40310001, None])
@pytest.mark.parametrize("other_order", ["none", "same_side_working", "opposite_side_filled"])
async def test_a_refusal_with_no_opposite_order_open_never_names_another_order(
    clocked_repo,  # noqa: F811
    code: int | None,
    other_order: str,
) -> None:
    """#2621: a 403 is described by the Clerk's evidence, never by Alpaca's code.

    Whatever the code, and even with Alpaca's wash-trade words, a refused buy
    names another order only while one on the other side is open: C's buy
    working on the same side, or A's sell that already filled, is not one.
    """
    repo, _clock = clocked_repo
    if other_order == "same_side_working":
        run_c = _register_spy_lane(repo, "c")
        await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id="c", decision_id="c-buy",
                           lifecycle_run_id=run_c, leg=_leg(quantity=3), trade=_FakeTrade())
    elif other_order == "opposite_side_filled":
        await _a_sells(repo, fills=True)
    run_b = _register_spy_lane(repo, "b")

    facts = await _refused_enter_facts(repo, "b", run_b, _wash_trade_rejection(code))

    assert facts.why == f"Alpaca refused the order: {_WASH_TRADE_MESSAGE}"
    assert facts.broker_error_code == code


@pytest.mark.parametrize("refusal", ["broker_wash_trade", "preflight"])
async def test_an_enter_that_never_reached_the_book_releases_its_cash_claim(
    tmp_path: Path, refusal: str,
) -> None:
    """An ENTER no order came of claims no cash, and a stopped bot holding nothing is Finished."""
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "b", 50_000)
        leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-1",
                                lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())
        if refusal == "broker_wash_trade":
            await submit_accepted_enter(repo, accepted=accepted, leg=leg,
                                        trade=_FakeTradePort(submit_error=_wash_trade_rejection()))
        else:
            await submit_accepted_enter(repo, accepted=accepted, leg=leg, trade=_FakeTradePort(),
                                        before_submit=lambda: "The market closed before the order was sent.")
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="b",
                        lifecycle_run_id="run-b", clock=repo.clock)

        budget = repo.account_budget(cash=1000, seen_before_ms=NOON + 1)

        assert budget.order_claims == 0
        assert budget.deployments[0].pending_orders == 0
        assert "b" not in repo.bots_holding_money()
    finally:
        repo.close()


def _exit_stuck(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_STUCK_REASON_CODE,
                                   strategy_instance_id=WATCHDOG_SID)


def _shown_recovery(repo: ClerkSqliteRepository) -> RecoveryStatus:
    """What A's bot page shows about its open exit notice: the Clerk's next automatic try."""
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        snapshot = reader.bot_snapshot(WATCHDOG_SID)
    finally:
        reader.close()
    assert snapshot is not None
    [notice] = [item for item in snapshot.uncertainties if item.reason_code == EXIT_NOT_FLAT_REASON_CODE]
    assert notice.recovery_status is not None
    return notice.recovery_status


async def _a_sells_into_bs_working_buy(
    repo: ClerkSqliteRepository, *, program_leg: ProgramLeg | None = None,
) -> EnterSubmission:
    """A holds 10 SPY; B's 5-share buy is working; Alpaca refuses A's sell as a potential wash trade."""
    ref_a = await _held_position(repo)
    sid_b, run_b = _register_second_spy_lane(repo)
    working_b = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b, decision_id="b-buy",
                                   lifecycle_run_id=run_b, leg=_leg(quantity=5), trade=_FakeTrade())
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a,
                           program_leg=program_leg)

    refusing = _FakeTradePort(submit_error=_wash_trade_rejection())
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=refusing,
                       pricing=UNPRICEABLE_RECOVERY if program_leg is None else _live_touch())

    [sent] = refusing.submitted_legs
    assert (sent.side, sent.quantity, sent.extended_hours) == (OrderSide.SELL, 10, program_leg is not None)
    exit_effect = repo.effect_operation(accepted.effect_operation_id)
    assert exit_effect is not None and exit_effect.state == "failed"
    assert repo.position(WATCHDOG_SID, "SPY") == 10
    assert repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
                                   strategy_instance_id=WATCHDOG_SID) is not None
    return working_b


async def test_a_refused_exit_keeps_alpacas_code_and_names_the_other_bots_open_buy(
    clocked_repo,  # noqa: F811
) -> None:
    """A's refused sell is recorded with Alpaca's code, beside B's buy the Clerk still shows working."""
    repo, _clock = clocked_repo
    await _a_sells_into_bs_working_buy(repo)

    refused = repo.last_strategy_transition(strategy_instance_id=WATCHDOG_SID, transition_kind="EXIT_NOT_FLAT")

    assert refused is not None
    facts = OrderSubmitFailedFacts.from_facts_json(refused["facts_json"])
    assert facts.broker_error_code == _ILLUSTRATIVE_CODE
    assert facts.why == (
        "This account still had an open SPY buy order when Alpaca refused this sell; "
        "Alpaca refuses an order that could trade against another open order in the same account. "
        f"Alpaca refused the order: {_WASH_TRADE_MESSAGE} attributed_qty=10.0 remains for 'SPY'."
    )


async def test_a_refused_exit_is_sent_again_two_minutes_later_once_the_other_bots_buy_fills(
    clocked_repo,  # noqa: F811
) -> None:
    """The ordinary case: B's market buy fills a moment after Alpaca refuses A's market sell.

    The stuck-EXIT watchdog first lets the refused exit's evidence settle for
    the policy's re-drive age (120 s); each of those passes is a hold. On the
    first pass after it, nothing works SPY and the broker holds the bots'
    total (10 + 5), so it re-sends A's full sell on its own. A's exit goes out
    two minutes late and nothing escalates.
    """
    repo, clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    refused_at = repo.clock()
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")
    policy = _exit_not_flat_redrive_policy()
    broker = _FakeRead(orders=[filled_b], positions=[_position("SPY", quantity=15.0)])
    redrive = _FakeTradePort()

    while not redrive.submitted_legs:
        assert repo.clock() - refused_at <= 2 * policy.after_ms, "never re-sent"
        await reconcile_account(repo, read=broker, trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        if not redrive.submitted_legs:
            clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)

    assert repo.clock() - refused_at == policy.after_ms
    assert [(leg.side, leg.quantity, leg.order_type) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.MARKET)]
    assert _exit_stuck(repo) is None


async def test_a_refused_exit_escalates_only_if_the_other_bots_order_keeps_working_eight_regular_session_minutes(
    clocked_repo,  # noqa: F811
) -> None:
    """B's buy stays ``new`` at the broker for ten regular-session minutes after A's refusal.

    The watchdog will not re-drive while any order on the symbol can still
    fill. After the 120 s settle wait, each regular-session pass on which B's
    order still works is a failure, and 480 s of them (120 s × 4) escalate A
    to ``EXIT_STUCK``: automatic re-drives stop and the owner must flatten.
    Escalation lands 600 s after the refusal; no second order is ever sent.
    """
    repo, clock = clocked_repo
    working_b = await _a_sells_into_bs_working_buy(repo)
    refused_at = repo.clock()
    policy = _exit_not_flat_redrive_policy()
    b_working = _broker_order(working_b.order_ref or "", order_id=f"bo-{working_b.order_ref}",
                              status="new", quantity=5.0)
    broker = _FakeRead(orders=[b_working], positions=[_position("SPY", quantity=10.0)])
    redrive = _FakeTrade()

    while _exit_stuck(repo) is None:
        assert repo.clock() - refused_at <= policy.after_ms * (policy.max_count + 2), "never escalated"
        await reconcile_account(repo, read=broker, trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        if _exit_stuck(repo) is None:
            clock.advance(DEFAULT_RECOVERY_INTERVAL_MS)

    stuck = _exit_stuck(repo)
    assert "work in flight that could fill under it" in stuck["explanation"]
    assert repo.clock() - refused_at == policy.after_ms + policy.after_ms * (policy.max_count + 1)
    assert redrive.submit_calls == []
    assert repo.position(WATCHDOG_SID, "SPY") == 10


async def test_a_refused_after_hours_exit_waits_for_the_next_session_and_never_escalates(
    clocked_repo,  # noqa: F811
) -> None:
    """Outside the regular session the refused exit is not retried in the session it was priced for.

    At 17:00 ET A's after-hours limit (valid to the 20:00 close) is refused
    while B's buy works; B's buy then fills. Although nothing works SPY any
    more and the broker holds the bots' total, every pass through the rest of
    the session is a hold: no order, no ``EXIT_STUCK``. A's page names the
    next try, 04:00 the next morning (the first instant its sealed exit
    allowance can price), and at 04:00 the watchdog re-sends A's sell as a
    pre-market limit.
    """
    repo, _clock = clocked_repo
    _walk_clock_to(repo, WATCHDOG_POST_T0)
    after_hours_sell = ProgramLeg(
        LegShape(order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=99.80,
                 extended_hours=True, side=OrderSide.SELL),
        valid_until_ms=POST_CLOSE_MS,
    )
    working_b = await _a_sells_into_bs_working_buy(repo, program_leg=after_hours_sell)
    filled_b = await _fill_entry(repo, working_b, quantity=5, execution_id="b-exec")
    broker = _FakeRead(orders=[filled_b], positions=[_position("SPY", quantity=15.0)])
    redrive = _FakeTradePort()

    for at_ms in (WATCHDOG_POST_T0 + 180_000, WATCHDOG_POST_T0 + 3_600_000, POST_CLOSE_MS - 30_000):
        _walk_clock_to(repo, at_ms)
        await reconcile_account(repo, read=broker, trade=redrive, pricing=_live_touch())

    assert redrive.submitted_legs == []
    assert _exit_stuck(repo) is None
    shown = _shown_recovery(repo)
    assert (shown.kind, shown.allowed_from_ms) == ("allowed_from", _ET_0400_NEXT_MORNING)

    _walk_clock_to(repo, _ET_0400_NEXT_MORNING)
    await reconcile_account(repo, read=broker, trade=redrive, pricing=_live_touch())

    assert [(leg.side, leg.quantity, leg.order_type, leg.extended_hours) for leg in redrive.submitted_legs] == [
        (OrderSide.SELL, 10, OrderType.LIMIT, True)]
    assert _exit_stuck(repo) is None


# ── One account reading for every bot ─────────────────────────────────────────


def test_one_bots_fill_refuses_the_other_bots_entry_until_the_next_account_reading(tmp_path: Path) -> None:
    """A fill recorded after the last account reading refuses every bot's ENTER.

    Two bots deciding on the same bar: A's buy fills first, so B's ENTER is
    refused until the next observation (15 s cadence) re-reads cash.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 50_000)
        _deploy(repo, "b", 50_000)
        gate = _gate()
        leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)
        entry_a = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="a-1",
                               lifecycle_run_id="run-a", leg=leg, reference_price=100, envelope=gate)
        _append_slice(repo, entry_a, execution_id="a-fill", quantity=1, source_event_at_ms=NOON)

        with pytest.raises(AdmissionBlockedError) as refused:
            accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-1",
                         lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=gate)

        assert refused.value.decision.reason_code == "LIVE_ENVELOPE_UNOBSERVED"
        assert "Executions changed after the last account reading" in (refused.value.decision.why or "")
    finally:
        repo.close()

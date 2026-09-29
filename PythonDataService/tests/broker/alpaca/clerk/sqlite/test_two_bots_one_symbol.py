"""Two bots trading one symbol in one Alpaca account (research #2469).

What the Clerk does today when two strategy instances hold and trade the same
symbol in one account. Findings note:
``docs/references/two-bots-one-symbol-2469.md``.

Passing tests pin current behaviour. ``xfail(strict=True)`` tests state the
behaviour a named follow-up must deliver; they flip to passing when it lands.
Every fixture comes from the existing Clerk suites (``conftest``,
``test_budget_commands``, ``test_envelope_reservations``, ``test_reconcile``);
the only new object is Alpaca's documented wash-trade rejection, built the way
alpaca-py raises it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter, submit_accepted_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import plan_account_reconciliation, reconcile_account
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
from app.broker.contract.errors import BrokerAuthError, BrokerError, BrokerOrderRejected
from app.broker.contract.models import BrokerOrderLeg, OrderSide
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _FakeTradePort, _make_held_position
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
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

# Placeholders the orchestrator replaces with the filed issue numbers.
_RELEASE_REJECTED_CLAIM = "#FOLLOWUP-A: a refused or rejected ENTER keeps its cash claim forever"
_NAME_ORDER_REJECTIONS = "#FOLLOWUP-B: Alpaca's order-level 403 (wash trade) reads as a credentials failure"

# Alpaca's documented wash-trade refusal: HTTP 403 on POST /v2/orders
# (https://docs.alpaca.markets/us/docs/user-protection). The page states only
# the status; the body's code and message are as reported on Alpaca's community
# forum. The Clerk maps on the status alone.
_WASH_TRADE_CODE = 40310000
_WASH_TRADE_MESSAGE = "potential wash trade detected. use complex orders"


def _wash_trade_api_error() -> APIError:
    body = json.dumps({"code": _WASH_TRADE_CODE, "message": _WASH_TRADE_MESSAGE})
    response = SimpleNamespace(status_code=403, headers={})
    return APIError(body, http_error=SimpleNamespace(response=response, request=None))


def _wash_trade_rejection() -> BrokerError:
    """The error the Clerk's broker port raises for the rejection (``client.submit_order``)."""
    return map_api_error(_wash_trade_api_error(), broker="alpaca", is_order_mutation=True)


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


@pytest.mark.xfail(strict=True, reason=_NAME_ORDER_REJECTIONS)
def test_a_wash_trade_refusal_is_an_order_rejection_that_keeps_alpacas_code() -> None:
    error = _wash_trade_rejection()

    assert isinstance(error, BrokerOrderRejected)
    assert str(_WASH_TRADE_CODE) in (error.detail or "")


def test_a_wash_trade_refusal_reads_today_as_a_credentials_failure() -> None:
    """Current mapping: every 403 is ``BrokerAuthError``; Alpaca's code is dropped."""
    error = _wash_trade_rejection()

    assert isinstance(error, BrokerAuthError)
    assert error.message == f"Alpaca rejected our credentials: {_WASH_TRADE_MESSAGE}"
    assert error.detail == "HTTP 403"


async def test_an_enter_refused_as_a_wash_trade_fails_and_the_bot_may_enter_again(tmp_path: Path) -> None:
    """The refusal is definitive: the ENTER folds failed, nothing is left uncertain."""
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
        [failed] = [row for row in repo.transitions_for_order(accepted.order_ref)
                    if row["transition_kind"] == "ORDER_SUBMIT_FAILED"]
        assert _WASH_TRADE_MESSAGE in failed["facts_json"]
        assert str(_WASH_TRADE_CODE) not in failed["facts_json"]
        again = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-2",
                             lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())
        assert again.created
    finally:
        repo.close()


@pytest.mark.parametrize("refusal", ["broker_wash_trade", "preflight"])
@pytest.mark.xfail(strict=True, reason=_RELEASE_REJECTED_CLAIM)
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


async def test_a_refused_enter_keeps_its_cash_claim_today(tmp_path: Path) -> None:
    """Current behaviour behind ``_RELEASE_REJECTED_CLAIM``: 2 × $100 + fee stays claimed after Stop."""
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "b", 50_000)
        leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=2)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="b-1",
                                lifecycle_run_id="run-b", leg=leg, reference_price=100, envelope=_gate())
        await submit_accepted_enter(repo, accepted=accepted, leg=leg,
                                    trade=_FakeTradePort(submit_error=_wash_trade_rejection()))
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="b",
                        lifecycle_run_id="run-b", clock=repo.clock)

        budget = repo.account_budget(cash=1000, seen_before_ms=NOON + 1)

        assert budget.order_claims == Decimal("200.01")
        assert budget.available == Decimal("799.99")
        assert repo.bots_holding_money() == frozenset({"b"})
    finally:
        repo.close()


async def test_an_exit_refused_as_a_wash_trade_while_the_other_bot_buys_escalates_without_a_retry(
    clocked_repo,  # noqa: F811
) -> None:
    """A's EXIT is refused while B's buy works on SPY; the watchdog waits for B, then escalates.

    Alpaca refuses a market sell while a market buy on the symbol is open in
    the account (the wash-trade table). The EXIT folds ``EXIT_NOT_FLAT`` with
    A's shares still held. The stuck-EXIT watchdog will not re-drive while
    any order on the symbol can still fill, so B's working order defers A's
    retry, and after the policy's escalation age A is ``EXIT_STUCK`` without
    a second order ever being sent.
    """
    repo, clock = clocked_repo
    ref_a = await _held_position(repo)
    sid_b, run_b = _register_second_spy_lane(repo)
    working_b = await submit_enter(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b, decision_id="b-buy",
                                   lifecycle_run_id=run_b, leg=_leg(quantity=5), trade=_FakeTrade())
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
                           decision_id="a-sell", lifecycle_run_id=WATCHDOG_RUN, entry_order_ref=ref_a)

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id,
                       trade=_FakeTrade(submit_error=_wash_trade_rejection()), pricing=UNPRICEABLE_RECOVERY)

    exit_effect = repo.effect_operation(accepted.effect_operation_id)
    assert exit_effect is not None and exit_effect.state == "failed"
    assert repo.position(WATCHDOG_SID, "SPY") == 10
    assert repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
                                   strategy_instance_id=WATCHDOG_SID) is not None

    policy = _exit_not_flat_redrive_policy()
    b_working = _broker_order(working_b.order_ref or "", order_id=f"bo-{working_b.order_ref}",
                              status="new", quantity=5.0)
    broker = _FakeRead(orders=[b_working], positions=[_position("SPY", quantity=10.0)])
    redrive = _FakeTrade()
    passes = 0
    while repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_STUCK_REASON_CODE,
                                  strategy_instance_id=WATCHDOG_SID) is None:
        assert passes <= (policy.after_ms * (policy.max_count + 2)) // 15_000, "never escalated"
        await reconcile_account(repo, read=broker, trade=redrive, pricing=UNPRICEABLE_RECOVERY)
        clock.advance(15_000)
        passes += 1

    stuck = repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_STUCK_REASON_CODE,
                                    strategy_instance_id=WATCHDOG_SID)
    assert "work in flight that could fill under it" in stuck["explanation"]
    assert redrive.submit_calls == []
    assert repo.position(WATCHDOG_SID, "SPY") == 10
    assert passes * 15_000 >= policy.after_ms * (policy.max_count + 1)


# ── One account reading for every bot ─────────────────────────────────────────


def test_one_bots_fill_holds_the_other_bots_entry_until_the_next_account_reading(tmp_path: Path) -> None:
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

"""Conservation at execution/observation boundaries, including older custody."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca import regulatory_fees
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.budget_projection import _external_cash_claim
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderLeg, OrderSide
from app.services.alpaca_fee_attribution import FeeFill
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _accept_day_pnl_enter
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_correction, _append_slice
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID, filled_order


def _record_sale(repo: ClerkSqliteRepository, accepted: EnterSubmission, *, key: str, price: float, at_ms: int, quantity: float = 1) -> None:
    """As in canonical FIFO tests, record economic fills on the owned order."""
    repo.append_transition(TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id, run_id=accepted.command.run_id,
        command_id=accepted.command.command_id, effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK", operation_state="in_progress",
        source_event_at_ms=at_ms, clerk_observed_at_ms=repo.clock(), summary_code="EXECUTION_SLICE_FILLED",
        facts_json=ExecutionSliceFilledFacts(execution_id=key, symbol="SPY", side="SELL", slice_qty=quantity,
            slice_price=price, fee=0, fee_fidelity="reported", evidence_source="websocket", source_event_at_ms=at_ms).to_facts_json(),
    ))


def test_completed_manual_buy_claims_its_unseen_exact_debit(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        accepted = accept_manual_order(
            repo, account_id=repo.account_id, operator_id=OPERATOR_ID, ticket_id=TICKET_ID,
            leg_id=LEG_ID, leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=6),
        )
        leg = accepted.leg
        assert leg.order_ref and leg.effect_operation_id
        repo.append_transition(TransitionInput(
            command_id=accepted.command.command_id, effect_operation_id=leg.effect_operation_id,
            order_ref=leg.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress", source_event_at_ms=NOON, clerk_observed_at_ms=NOON,
            summary_code="EXECUTION_SLICE_FILLED", facts_json=ExecutionSliceFilledFacts(
                execution_id="manual-exact", symbol="SPY", side="BUY", slice_qty=6,
                slice_price=100, fee=.03, fee_fidelity="reported", evidence_source="websocket",
                source_event_at_ms=NOON,
            ).to_facts_json(),
        ))
        fold_order_acknowledgement(repo, effect_operation_id=leg.effect_operation_id, order=filled_order(leg.order_ref).model_copy(update={
            "quantity": 6, "filled_quantity": 6, "filled_avg_price": 100,
            "updated_at_ms": NOON, "observed_at_ms": NOON,
        }))
        assert repo.effect_operation(leg.effect_operation_id).state == "succeeded"
        unseen = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert unseen.order_claims == Decimal("600.03")
        assert unseen.fee_claims == 0  # Reported fee rides with its fill, exactly once.
        assert unseen.available == Decimal("399.97")
        seen = repo.account_budget(cash="399.97", seen_before_ms=NOON + 1)
        assert seen.order_claims == 0 and seen.available == unseen.available
    finally:
        repo.close()


def test_manual_shares_are_outside_any_bot_and_counted_once_before_and_after_cash_sees_them(tmp_path: Path) -> None:
    """PRD #2560: the money bar places manual shares outside every bot, at
    cost, and never counts a bought share as cash too -- whether or not the
    cash observation has caught up with the fill."""
    from app.broker.alpaca.clerk.account_money import money_bar

    repo = _new_budget_repo(tmp_path)
    try:
        accepted = accept_manual_order(
            repo, account_id=repo.account_id, operator_id=OPERATOR_ID, ticket_id=TICKET_ID,
            leg_id=LEG_ID, leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=6),
        )
        leg = accepted.leg
        assert leg.order_ref and leg.effect_operation_id
        repo.append_transition(TransitionInput(
            command_id=accepted.command.command_id, effect_operation_id=leg.effect_operation_id,
            order_ref=leg.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress", source_event_at_ms=NOON, clerk_observed_at_ms=NOON,
            summary_code="EXECUTION_SLICE_FILLED", facts_json=ExecutionSliceFilledFacts(
                execution_id="manual-outside", symbol="SPY", side="BUY", slice_qty=6,
                slice_price=100, fee=.03, fee_fidelity="reported", evidence_source="websocket",
                source_event_at_ms=NOON,
            ).to_facts_json(),
        ))
        fold_order_acknowledgement(repo, effect_operation_id=leg.effect_operation_id, order=filled_order(leg.order_ref).model_copy(update={
            "quantity": 6, "filled_quantity": 6, "filled_avg_price": 100, "updated_at_ms": NOON, "observed_at_ms": NOON,
        }))
        unseen = repo.account_money(cash=1000, seen_before_ms=NOON)
        seen = repo.account_money(cash="399.97", seen_before_ms=NOON + 1)
        for money in (unseen, seen):
            assert money.outside == Decimal(600) and money.cash == Decimal("399.97")
            assert money.total == Decimal("999.97")
            bar = money_bar(money)
            assert [(segment.kind, segment.cents) for segment in bar.segments] == [("outside", 60_000), ("free", 39_997)]
            assert bar.total_cents == 99_997
    finally:
        repo.close()


def test_stopped_bot_still_holding_keeps_its_shares_on_the_bar_and_releases_its_free_budget(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.account_money import money_bar

    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 50_000)
        _deploy(repo, "b", 30_000)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="hold",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2), reference_price=100, envelope=_gate())
        _append_slice(repo, accepted, execution_id="held", quantity=2, price=100, source_event_at_ms=NOON - 1, fee=0)
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=repo.clock)

        money = repo.account_money(cash=800, seen_before_ms=NOON + 1)
        bar = money_bar(money)

        assert [(segment.kind, segment.strategy_instance_id, segment.cents) for segment in bar.segments] == [
            ("bot", "b", 30_000), ("stopped", "a", 20_000), ("free", None, 50_000),
        ]
        assert bar.segments[1].released_cents == 30_000
        assert bar.total_cents == 100_000 and money.budget.unreserved_cents == 50_000
    finally:
        repo.close()


def _manual_fill(repo: ClerkSqliteRepository, *, side: str, price: float, fee: float, ticket: str, leg_id: str, order_id: str) -> None:
    """One filled manual order of 6 SPY, recorded at the repository clock."""
    accepted = accept_manual_order(repo, account_id=repo.account_id, operator_id=OPERATOR_ID, ticket_id=ticket,
        leg_id=leg_id, leg=BrokerOrderLeg(symbol="SPY", side=side, quantity=6))
    leg = accepted.leg
    assert leg.order_ref and leg.effect_operation_id
    now = repo.clock()
    repo.append_transition(TransitionInput(
        command_id=accepted.command.command_id, effect_operation_id=leg.effect_operation_id,
        order_ref=leg.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress", source_event_at_ms=now, clerk_observed_at_ms=now,
        summary_code="EXECUTION_SLICE_FILLED", facts_json=ExecutionSliceFilledFacts(
            execution_id=f"{order_id}-exact", symbol="SPY", side=side.upper(), slice_qty=6,
            slice_price=price, fee=fee, fee_fidelity="reported", evidence_source="websocket",
            source_event_at_ms=now,
        ).to_facts_json(),
    ))
    fold_order_acknowledgement(repo, effect_operation_id=leg.effect_operation_id, order=filled_order(leg.order_ref).model_copy(update={
        "order_id": order_id, "side": side, "quantity": 6, "filled_quantity": 6, "filled_avg_price": price,
        "updated_at_ms": now, "observed_at_ms": now,
    }))


def test_a_sale_the_cash_has_not_seen_settles_on_the_bar_and_the_total_holds(tmp_path: Path) -> None:
    """Review A2: canonical FIFO drops a sold lot at once while Alpaca's cash
    catches up ~20 s later. The sale's net proceeds are counted by the same
    ``seen_before_ms`` boundary as an unseen purchase, so the account's total
    never dips and free to deploy stays the budget's conservative figure,
    with the proceeds drawn as settling -- never as a shortfall."""
    from app.broker.alpaca.clerk.account_money import money_bar

    repo = _new_budget_repo(tmp_path)
    try:
        _manual_fill(repo, side="buy", price=100, fee=.03, ticket=TICKET_ID, leg_id=LEG_ID, order_id="bought")
        held = repo.account_money(cash="399.97", seen_before_ms=NOON + 5)
        repo.clock.advance(10)
        _manual_fill(repo, side="sell", price=110, fee=.02, ticket="3f0c7a2e-1b7d-4f7e-9d0a-6a1c3e5b7d90",
                     leg_id="5a2b8c4d-6e7f-4a1b-8c2d-3e4f5a6b7c8d", order_id="sold")

        unseen = repo.account_money(cash="399.97", seen_before_ms=NOON + 5)
        seen = repo.account_money(cash="1059.95", seen_before_ms=NOON + 20)

        assert held.total == Decimal("999.97") and held.outside == Decimal(600)
        # $660 of proceeds less the reported $0.02 fee, exactly as an unseen
        # purchase is its cost plus its reported fee.
        assert unseen.total == seen.total == Decimal("1059.95") and unseen.outside == 0
        unseen_bar, seen_bar = money_bar(unseen), money_bar(seen)
        assert [(segment.kind, segment.cents) for segment in unseen_bar.segments] == [("settling", 65_998), ("free", 39_997)]
        assert unseen_bar.shortfall_cents == 0 and unseen_bar.cents_of("free") == unseen.budget.unreserved_cents
        assert [(segment.kind, segment.cents) for segment in seen_bar.segments] == [("free", 105_995)]
    finally:
        repo.close()


def test_shares_bought_outside_every_bot_are_on_the_bar_at_fifo_cost(tmp_path: Path) -> None:
    """Review A3: external (non-Clerk) shares are held outside any bot, valued
    by canonical FIFO over the complete external population, before and after
    the cash observation sees the purchase -- total = cash + every position."""
    from app.broker.alpaca.clerk.account_money import money_bar
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        observe_external_order(repo, order=_external_order(state="filled", filled=6, observed_at=NOON))
        record_fee_evidence(repo, [_external_fill()], checked_at_ms=NOON, history_complete=True)
        for money in (repo.account_money(cash=1000, seen_before_ms=NOON), repo.account_money(cash=400, seen_before_ms=NOON + 1)):
            assert money.outside == Decimal(600) and money.cash == Decimal(400) and money.total == Decimal(1000)
            assert money.unvalued == ()
            bar = money_bar(money)
            assert [(segment.kind, segment.cents) for segment in bar.segments] == [("outside", 60_000), ("charges", 1), ("free", 39_999)]
    finally:
        repo.close()


def test_outside_shares_that_cannot_be_valued_are_named_never_dropped(tmp_path: Path) -> None:
    """Review A3: an external sale with no purchase in the population is a
    short the long-only bar cannot place; it is named beside the bar."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        sold = _external_order(state="filled", filled=6, observed_at=NOON).model_copy(update={"side": "sell"})
        observe_external_order(repo, order=sold)
        record_fee_evidence(repo, [_external_fill().model_copy(update={"side": "sell"})], checked_at_ms=NOON, history_complete=True)
        money = repo.account_money(cash=1600, seen_before_ms=NOON + 1)
        assert money.outside == 0
        assert money.unvalued == ("6 SPY sold short by outside orders",)
    finally:
        repo.close()


def _outside_sale(order_id: str, symbol: str, *, at_ms: int) -> tuple[BrokerOrder, BrokerActivity]:
    """One filled one-share outside sale and its execution, both dated ``at_ms``."""
    order = _external_order(state="filled", filled=1, observed_at=at_ms).model_copy(update={
        "order_id": order_id, "symbol": symbol, "side": "sell", "quantity": 1,
    })
    fill = _external_fill(quantity=1).model_copy(update={
        "activity_id": f"{order_id}-fill", "native_order_id": order_id, "symbol": symbol, "side": "sell",
        "occurred_at_ms": at_ms,
    })
    return order, fill


def test_outside_shorts_are_named_once_per_symbol_never_once_per_lot(tmp_path: Path) -> None:
    """H35: FIFO keeps one open lot per one-share sale, and the note listed
    every lot ("1 TSLA sold short by outside orders" four times). It names
    the position instead: one phrase, a total per symbol."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        sales = [
            _outside_sale(f"sale-{index}", symbol, at_ms=NOON + index)
            for index, symbol in enumerate(["TSLA", "TSLA", "QQQ", "QQQ", "QQQ", "AAPL"])
        ]
        for order, _ in sales:
            observe_external_order(repo, order=order)
        record_fee_evidence(repo, [fill for _, fill in sales], checked_at_ms=NOON, history_complete=True)
        money = repo.account_money(cash=1600, seen_before_ms=NOON + 10)
        # The one holding formatter the bot rows use (``holdings_text``).
        assert money.unvalued == ("1 AAPL, 3 QQQ, 2 TSLA sold short by outside orders",)
    finally:
        repo.close()


def _outside_trade(order_id: str, symbol: str, side: str, *, quantity: float = 1, at_ms: int) -> tuple[BrokerOrder, BrokerActivity]:
    """One filled outside order of ``quantity`` at $100 and its execution, dated ``at_ms``."""
    order, fill = _outside_sale(order_id, symbol, at_ms=at_ms)
    return (
        order.model_copy(update={"side": side, "quantity": quantity, "filled_quantity": quantity}),
        fill.model_copy(update={"side": side, "quantity": quantity}),
    )


def _drawn_total(money) -> tuple[Decimal, int, int]:
    """The account's total, and what its drawn bar says: total and shortfall cents."""
    from app.broker.alpaca.clerk.account_money import money_bar

    bar = money_bar(money)
    return money.total, bar.total_cents, bar.shortfall_cents


# Custody (``_new_budget_repo``) began at NOON exactly.
_BEFORE_CUSTODY_DAY = NOON - 4 * 86_400_000
_EARLIER_ON_GENESIS_DAY = NOON - 3_600_000


@pytest.mark.parametrize("before_custody", [_BEFORE_CUSTODY_DAY, _EARLIER_ON_GENESIS_DAY], ids=["days-before", "same-day"])
def test_an_outside_purchase_from_before_custody_is_inside_the_cash(tmp_path: Path, before_custody: int) -> None:
    """Review A1 (H35): an outside BUY from before custody began, recorded
    only now -- at or after the cash observation's boundary -- is inside the
    cash custody started from. Pricing it as an unseen purchase subtracted
    shares no longer on the bar from the cash: $400 cash with 6 SPY bought
    before custody drew -$200. Total = cash."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        order, fill = _outside_trade("old-buy", "SPY", "buy", quantity=6, at_ms=before_custody)
        observe_external_order(repo, order=order)
        record_fee_evidence(repo, [fill], checked_at_ms=NOON, history_complete=True)

        money = repo.account_money(cash=400, seen_before_ms=NOON)

        assert _drawn_total(money) == (Decimal(400), 40_000, 0)
        assert money.cash == Decimal(400) and money.outside == 0
        assert not money.holds_positions and money.unvalued == ()
    finally:
        repo.close()


def test_outside_trades_from_before_custody_never_move_the_total(tmp_path: Path) -> None:
    """Review A1 probe: 3 purchases and 4 sales of one AAPL at $100, all
    before custody began and recorded now, drew $700 against $1,000 of cash
    (and $1,100 before H35). They are inside the cash: the total is $1,000."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        trades = [
            _outside_trade(f"old-{index}", "AAPL", side, at_ms=_BEFORE_CUSTODY_DAY + index)
            for index, side in enumerate(["buy", "sell", "buy", "sell", "buy", "sell", "sell"])
        ]
        for order, _ in trades:
            observe_external_order(repo, order=order)
        record_fee_evidence(repo, [fill for _, fill in trades], checked_at_ms=NOON, history_complete=True)

        money = repo.account_money(cash=1000, seen_before_ms=NOON)

        assert _drawn_total(money) == (Decimal(1000), 100_000, 0)
        assert money.unvalued == () and money.unseen_sales == 0 and not money.holds_positions
    finally:
        repo.close()


def test_an_outside_short_from_after_custody_began_is_still_named(tmp_path: Path) -> None:
    """Review A1: the population is split by the instant custody began, not
    its ET day. A sale earlier on genesis day is inside the starting cash; a
    sale after genesis with no purchase is a genuine short, still named."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        trades = [
            _outside_trade("same-day-old-sale", "AAPL", "sell", at_ms=_EARLIER_ON_GENESIS_DAY),
            _outside_trade("new-sale", "TSLA", "sell", at_ms=NOON + 1),
        ]
        for order, _ in trades:
            observe_external_order(repo, order=order)
        record_fee_evidence(repo, [fill for _, fill in trades], checked_at_ms=NOON, history_complete=True)

        money = repo.account_money(cash=1000, seen_before_ms=NOON)

        assert money.unvalued == ("1 TSLA sold short by outside orders",)
        # Only the custody-era sale's proceeds are still on their way to cash.
        assert money.unseen_sales == Decimal(100)
    finally:
        repo.close()


def test_an_outside_sale_from_before_custody_began_is_never_a_short(tmp_path: Path) -> None:
    """H35 (paper account PA3KWXU1C4C3): a tracked outside order that sold
    before custody began closed a purchase made before custody too. Lotting
    the sale alone read it as a short -- on an account Alpaca reported flat.
    Custody starts from a flat account, so the execution only proves its
    order's filled quantity: never a lot, a short, or an unseen sale."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        before_custody = NOON - 4 * 86_400_000
        order, fill = _outside_sale("old-sale", "AAPL", at_ms=before_custody)
        observe_external_order(repo, order=order)
        # Recorded now (observed at NOON), as the paper clerk's startup
        # recovery recorded its old outside orders' history.
        record_fee_evidence(repo, [fill], checked_at_ms=NOON, history_complete=True)

        money = repo.account_money(cash=1000, seen_before_ms=NOON)

        assert money.unvalued == ()
        assert not money.holds_positions
        assert money.total == Decimal(1000) and money.unseen_sales == 0
    finally:
        repo.close()


@pytest.mark.parametrize("status,reported,recorded", [("filled", 10, 0), ("canceled", 3, 0), ("canceled", 3, 2.9999999999)])
def test_missing_legacy_terminal_execution_is_unknown_until_reconciled(
    day_pnl_repo, status: str, reported: float, recorded: float,
) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="legacy-without-reservation")
    assert accepted.order_ref and accepted.effect_operation_id
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id=accepted.command.strategy_instance_id,
                    lifecycle_run_id=accepted.command.run_id.split(":", 1)[1], clock=repo.clock)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    if recorded:
        _append_slice(repo, accepted, execution_id="initial-legacy-fill", quantity=recorded, source_event_at_ms=NOON, fee=0)
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=filled_order(accepted.order_ref).model_copy(update={
        "status": status, "quantity": 10, "filled_quantity": reported, "filled_avg_price": 100,
        "updated_at_ms": NOON, "observed_at_ms": NOON,
    }))
    fees = repo.fee_attribution(now_ms=NOON)
    assert not fees.known
    with pytest.raises(BudgetUnavailable, match="fill coverage"):
        repo.account_budget(cash=1000, seen_before_ms=NOON + 1)
    _append_slice(repo, accepted, execution_id="late-legacy-fill",
                  quantity=float(Decimal(str(reported)) - Decimal(str(recorded))), source_event_at_ms=NOON, fee=0)
    assert repo.fee_attribution(now_ms=NOON).known
    assert repo.account_budget(cash=1000, seen_before_ms=NOON + 1).available == 1000


def test_canceled_order_without_fills_does_not_invent_missing_population(day_pnl_repo) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="canceled-zero")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id,
        order=filled_order(accepted.order_ref).model_copy(update={"status": "canceled", "filled_quantity": 0}))
    assert repo.fee_attribution(now_ms=NOON).known


def test_first_tiny_broker_fill_is_retained_instead_of_becoming_zero(day_pnl_repo) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="tiny-total")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    order = filled_order(accepted.order_ref).model_copy(update={"status": "canceled", "filled_quantity": 1e-10})
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=order)
    assert repo.latest_reported_filled_quantity(accepted.order_ref) == 1e-10
    assert not repo.fee_attribution(now_ms=NOON).known
    _append_slice(repo, accepted, execution_id="tiny-exact", quantity=1e-10, source_event_at_ms=NOON, fee=0)
    assert repo.fee_attribution(now_ms=NOON).known


@pytest.mark.parametrize("prior,current", [(1, 1.0000000001), (1.0000000001, 1)])
def test_same_state_broker_quantity_changes_are_retained_exactly(day_pnl_repo, prior: float, current: float) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="tiny-change")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    _append_slice(repo, accepted, execution_id="exact-one", quantity=1, source_event_at_ms=NOON, fee=0)
    order = filled_order(accepted.order_ref).model_copy(update={"filled_quantity": prior})
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=order)
    restated = order.model_copy(update={"filled_quantity": current})
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=restated)
    assert repo.latest_reported_filled_quantity(accepted.order_ref) == current
    assert repo.fee_attribution(now_ms=NOON).known == (current == 1)
    after = repo.custody_transitions()
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=restated)
    assert repo.custody_transitions() == after


def test_a_partly_filled_entry_keeps_claiming_its_whole_fee_provision(tmp_path: Path) -> None:
    """Owner decision 2026-09-29 (#2553): no proportional share of the recorded fee.

    While any of the order is unfilled, its remainder claims the whole
    provision the ENTER was admitted with, until the order fills or ends.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo)
        accepted = accept_enter(
            repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="partial",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=4000),
            reference_price=.1, envelope=_gate(),
        )
        _append_slice(repo, accepted, execution_id="filled-part", quantity=3000, price=.1, source_event_at_ms=NOON)
        projection = repo.account_budget(cash=1000, seen_before_ms=NOON)
        own = projection.deployments[0]
        # CAT: 3000 filled shares settle to .01 in the fee attribution; the
        # unfilled 1000 still claim the whole recorded .02 provision.
        assert own.fees == Decimal(".01")
        assert own.pending_orders == Decimal("100.02")
        assert own.free == Decimal("599.97")
        assert projection.available == 0
        _append_correction(repo, accepted, execution_id="corrected-part", superseded_execution_ref="filled-part",
                           quantity=2000, source_event_at_ms=NOON + 1)
        corrected = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert corrected.deployments[0].pending_orders == Decimal("200.02")
    finally:
        repo.close()


@pytest.mark.parametrize(("filled", "pending"), [
    # Nothing filled: the whole recorded .02 provision (4000 x $0.000003 CAT, rounded up).
    (0, Decimal("400.02")),
    # 3000 filled: the unfilled 1000 still claim the whole recorded .02.
    (3000, Decimal("100.02")),
])
def test_a_reservation_claims_its_recorded_fee_provision_after_a_fee_model_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filled: int, pending: Decimal,
) -> None:
    """Regression (#2553): the fee an ENTER was admitted with is the fee it claims.

    The provision is recorded once, from the entry requirement at admission.
    Re-quoting it from the fee model on every read let a later rate change
    silently move a past claim.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo)
        accepted = accept_enter(
            repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="recorded-fee",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=4000),
            reference_price=.1, envelope=_gate(),
        )
        if filled:
            _append_slice(repo, accepted, execution_id="filled-part", quantity=filled, price=.1, source_event_at_ms=NOON)
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).deployments[0].pending_orders == pending

        # A later model: CAT a full cent a share. The recorded provision stands.
        monkeypatch.setattr(regulatory_fees, "_CAT_PER_SHARE", ((date(2026, 9, 1), Decimal("0.01")),))

        assert repo.account_budget(cash=1000, seen_before_ms=NOON).deployments[0].pending_orders == pending
    finally:
        repo.close()


def test_interleaved_same_symbol_deployments_keep_own_fifo_after_correction_and_stop(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 50_000)
        _deploy(repo, "b", 50_000)
        entries = {}
        for sid, price in (("a", 100), ("b", 200)):
            accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id=sid,
                decision_id=f"buy-{sid}", lifecycle_run_id=f"run-{sid}",
                leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2), reference_price=price, envelope=_gate())
            entries[sid] = accepted
        for sid, price in (("a", 100), ("b", 200)):
            accepted = entries[sid]
            _append_slice(repo, accepted, execution_id=f"buy-{sid}", quantity=2, price=price,
                          source_event_at_ms=NOON - (20 if sid == "a" else 10), fee=0)
        _record_sale(repo, entries["b"], key="sell-b", price=210, at_ms=NOON - 2)
        _record_sale(repo, entries["a"], key="sell-a", price=110, at_ms=NOON - 1)
        projection = repo.account_budget(cash=720, seen_before_ms=NOON + 1)
        by_sid = {row.strategy_instance_id: row for row in projection.deployments}
        assert by_sid["a"].realized_gross == by_sid["b"].realized_gross == 10
        assert by_sid["a"].free == 410 and by_sid["b"].free == 310
        assert projection.available == 0
        _append_correction(repo, entries["a"], execution_id="buy-a-correction", superseded_execution_ref="buy-a",
                           quantity=1.5, source_event_at_ms=NOON + 20)
        corrected = repo.account_budget(cash=770, seen_before_ms=NOON + 1)
        assert {row.strategy_instance_id: row.free for row in corrected.deployments} == {"a": 460, "b": 310}
        assert corrected.order_claims == 0 and corrected.available == 0
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=repo.clock)
        stopped = repo.account_budget(cash=770, seen_before_ms=NOON + 1)
        assert stopped.available == 460
        assert {row.strategy_instance_id: row.position_cost for row in stopped.deployments} == {"a": 50, "b": 200}
    finally:
        repo.close()


@pytest.mark.parametrize("buy_qty,buy_price,sell_qty,sell_price,realized,position_cost,spendable", [
    # 1 x ($0.03 - $0.01) is exactly $0.02; binary float made it 0.019999999999999997.
    (1, .01, 1, .03, Decimal("0.02"), Decimal(0), 50_002),
    # 0.8 - 0.2 shares leaves exactly 0.6 at $10; binary float left 0.6000000000000001.
    (.8, 10, .2, 10, Decimal(0), Decimal("6"), 49_400),
])
def test_budget_reads_fifo_money_exactly_at_whole_cent_boundaries(
    tmp_path: Path, buy_qty: float, buy_price: float, sell_qty: float, sell_price: float,
    realized: Decimal, position_cost: Decimal, spendable: int,
) -> None:
    """Regression (#2550): canonical FIFO feeds the budget exact money.

    A float FIFO normalized after multiplication lost a real spendable cent
    whenever the true free balance sat on a whole cent, the common case.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 50_000)
        accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id="a",
            decision_id="cent-boundary", lifecycle_run_id="run-a",
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=buy_qty), reference_price=buy_price, envelope=_gate())
        _append_slice(repo, accepted, execution_id="buy", quantity=buy_qty, price=buy_price,
                      source_event_at_ms=NOON - 2, fee=0)
        _record_sale(repo, accepted, key="sell", price=sell_price, at_ms=NOON - 1, quantity=sell_qty)
        own = repo.account_budget(cash=1000, seen_before_ms=NOON + 1).deployments[0]
        assert own.realized_gross == realized
        assert own.position_cost == position_cost
        assert own.free == Decimal(spendable) / 100
        assert own.spendable_cents == spendable
    finally:
        repo.close()


def _external_order(*, state: str = "accepted", filled: float = 0, observed_at: int = NOON - 86_400_000) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca", order_id="external-budget-order", client_order_id="console-order",
        symbol="SPY", asset_class="us_equity", side="buy", order_type="limit", time_in_force="gtc",
        quantity=6, filled_quantity=filled, limit_price=100, stop_price=None,
        filled_avg_price=100 if filled else None, status=state, submitted_at_ms=observed_at,
        created_at_ms=observed_at, updated_at_ms=observed_at, filled_at_ms=None,
        canceled_at_ms=None, expired_at_ms=None, observed_at_ms=observed_at,
    )


def _external_fill(*, quantity: float = 6, observed_at: int = NOON) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca", activity_id="external-budget-fill", native_order_id="external-budget-order",
        activity_type="FILL", category="trade_activity", symbol="SPY", side="buy",
        quantity=quantity, price=100, net_amount=None, occurred_at_ms=NOON, observed_at_ms=observed_at,
    )


class _AnonymousExternalFee:
    """A ``BudgetFees`` reader whose fee evidence boundary did NOT prove identity.

    ``fee_evidence.py`` never emits a ``FeeFill`` with ``native_order_id=None``
    for the shipped ``custody_fee_attribution`` reader — it drops such an
    activity and marks the population incomplete instead. This double exists
    to exercise ``_external_cash_claim`` in isolation, for the boundary itself
    rather than that one caller.
    """

    external_fills = (
        FeeFill(fill_id="anonymous-external-fill", subject_id="external:?", side=OrderSide.BUY,
            quantity=Decimal(1), price=Decimal(100), native_order_id=None, observed_at_ms=NOON),
    )
    pre_custody_quantities: dict[str, Decimal] = {}


def test_external_fill_without_native_order_id_fails_closed(tmp_path: Path) -> None:
    """#2550: an unidentified external fill must refuse, never key claims by None."""
    repo = _new_budget_repo(tmp_path)
    try:
        with repo.write_fence() as conn, pytest.raises(BudgetUnavailable):
            _external_cash_claim(conn, _AnonymousExternalFee(), seen_before_ms=NOON)
    finally:
        repo.close()


def test_reviewing_prior_day_external_gtc_order_does_not_release_its_money(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        external = observe_external_order(repo, order=_external_order())
        acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
        with pytest.raises(BudgetUnavailable, match="external order"):
            _deploy(repo)
        observe_external_order(repo, order=_external_order(state="canceled", observed_at=NOON))
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).available == 1000
    finally:
        repo.close()


def test_external_fill_before_order_observation_keeps_its_unseen_debit(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        record_fee_evidence(repo, [_external_fill()], checked_at_ms=NOON, history_complete=True)
        unseen = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert unseen.order_claims == 600
        assert unseen.fee_claims == Decimal("0.01")
        assert unseen.available == Decimal("399.99")
        # Duplicate polling cannot move an already recognized debit's cutoff.
        repo.clock.advance(1)
        record_fee_evidence(repo, [_external_fill(observed_at=NOON + 1)], checked_at_ms=NOON + 1, history_complete=True)
        seen = repo.account_budget(cash=400, seen_before_ms=NOON + 1)
        assert seen.order_claims == 0 and seen.available == unseen.available
        assert repo.account_budget(cash=1000, seen_before_ms=NOON) == unseen
    finally:
        repo.close()


def test_terminal_external_order_needs_all_its_exact_executions_then_replays(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order

    repo = _new_budget_repo(tmp_path)
    external = observe_external_order(repo, order=_external_order(state="canceled", filled=3))
    acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
    record_fee_evidence(repo, [_external_fill(quantity=2)], checked_at_ms=NOON, history_complete=True)
    with pytest.raises(BudgetUnavailable, match="external order"):
        repo.account_budget(cash=1000, seen_before_ms=NOON)
    # The exact missing execution is a separate activity, never a made-up cumulative fill.
    remainder = _external_fill(quantity=1).model_copy(update={"activity_id": "external-remainder"})
    record_fee_evidence(repo, [_external_fill(quantity=2), remainder], checked_at_ms=NOON, history_complete=True)
    expected = repo.account_budget(cash=1000, seen_before_ms=NOON)
    assert expected.order_claims == 300
    database = repo.db_path
    repo.close()
    database.rename(database.with_suffix(".saved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=lambda: NOON)
    try:
        # Freshness is this process's producer read, never replayed: the
        # rebuilt authority refuses until it reads again, which adds nothing.
        with pytest.raises(BudgetUnavailable, match="missing or stale"):
            rebuilt.account_budget(cash=1000, seen_before_ms=NOON)
        assert not record_fee_evidence(rebuilt, [_external_fill(quantity=2), remainder], checked_at_ms=NOON, history_complete=True)
        assert rebuilt.account_budget(cash=1000, seen_before_ms=NOON) == expected
    finally:
        rebuilt.close()


def test_legacy_external_observation_stays_hash_compatible_and_unknown_until_refreshed(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
    from app.broker.alpaca.clerk.sqlite.facts import ExternalOrderObservedFacts
    from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize

    repo = _new_budget_repo(tmp_path)
    try:
        legacy = canonicalize({
            "external_order_id": "external-budget-order", "broker_order_id": "external-budget-order",
            "client_order_id": "console-order", "symbol": "SPY", "side": "BUY", "qty": 6.0,
            "order_type": "limit", "limit_price": 100.0, "stop_price": None, "filled_avg_price": None,
            "observed_at_ms": NOON - 86_400_000, "evidence_refs": ["external-budget-order"],
        })
        assert ExternalOrderObservedFacts.from_facts_json(legacy).to_facts_json() == legacy
        repo.append_transition(TransitionInput(
            broker_order_id="external-budget-order", transition_kind="EXTERNAL_ORDER_OBSERVED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
            clerk_observed_at_ms=NOON, summary_code="EXTERNAL_ORDER_OBSERVED", facts_json=legacy,
        ))
        acknowledge_external_order(repo, external_order_id="external-budget-order", operator="owner")
        history = tuple(repo.custody_transitions())
        with pytest.raises(BudgetUnavailable, match="unknown cash obligation"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
        # Same original fields, now with explicit cancellation proof: not a duplicate.
        observe_external_order(repo, order=_external_order(state="canceled"))
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).available == 1000
        assert tuple(repo.custody_transitions())[:len(history)] == history
    finally:
        repo.close()


@pytest.mark.parametrize("recorded", [2.9999999999, 3.0000000001])
def test_external_fill_coverage_has_no_quantity_epsilon(tmp_path: Path, recorded: float) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        observe_external_order(repo, order=_external_order(state="canceled", filled=3))
        record_fee_evidence(repo, [_external_fill(quantity=recorded)], checked_at_ms=NOON, history_complete=True)
        with pytest.raises(BudgetUnavailable, match="complete execution population"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
    finally:
        repo.close()


@pytest.mark.parametrize("filled", [0, 3])
async def test_reconcile_refreshes_old_reviewed_gtc_cancellation_and_retains_exact_debit(tmp_path: Path, filled: int) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
    from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    repo = _new_budget_repo(tmp_path)
    try:
        external = observe_external_order(repo, order=_external_order())
        acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
        with pytest.raises(BudgetUnavailable, match="external order"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
        terminal = _external_order(state="canceled", filled=filled, observed_at=NOON)
        trade = _FakeTrade(lookup_result=terminal)
        # This GTC was submitted yesterday. It is absent from today's open
        # snapshot; exact identity lookup must supply its cancellation proof.
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY,
            trigger="OPERATOR_RECONCILE_NOW")
        assert trade.lookup_calls == ["console-order"]
        assert repo.external_order(external.external_order_id).broker_state == "canceled"
        if filled:
            with pytest.raises(BudgetUnavailable, match="Reconcile account executions"):
                repo.account_budget(cash=1000, seen_before_ms=NOON)
            record_fee_evidence(repo, [_external_fill(quantity=filled)], checked_at_ms=NOON, history_complete=True)
        budget = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert budget.order_claims == Decimal(filled * 100)
        # Confirmed terminal evidence is retained; a second pass needs no
        # lookup and cannot forget the debit before a newer cash observation.
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY)
        assert trade.lookup_calls == ["console-order"]
        assert repo.account_budget(cash=1000, seen_before_ms=NOON) == budget
    finally:
        repo.close()


@pytest.mark.parametrize("answer", ["absent", "unavailable", "wrong_identity"])
async def test_external_refresh_requires_positive_matching_order_proof(tmp_path: Path, answer: str) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
    from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
    from app.broker.contract.errors import BrokerUnavailable
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    repo = _new_budget_repo(tmp_path)
    try:
        external = observe_external_order(repo, order=_external_order())
        acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
        trade = _FakeTrade(lookup_absent=answer == "absent",
            lookup_error=BrokerUnavailable("offline") if answer == "unavailable" else None,
            lookup_result=_external_order(state="canceled").model_copy(update={"order_id": "other"}))
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY)
        assert trade.lookup_calls == ["console-order"]
        assert repo.external_order(external.external_order_id).broker_state == "accepted"
        with pytest.raises(BudgetUnavailable, match="external order"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
        retry = _FakeTrade(lookup_result=_external_order(state="canceled", observed_at=NOON))
        await reconcile_account(repo, read=_FakeRead(), trade=retry, pricing=UNPRICEABLE_RECOVERY)
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).available == 1000
    finally:
        repo.close()


async def test_external_lookup_cannot_overwrite_a_fill_arriving_during_its_read(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
    from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    repo = _new_budget_repo(tmp_path)

    class FillDuringLookup(_FakeTrade):
        async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
            observe_external_order(repo, order=_external_order(state="filled", filled=6, observed_at=NOON))
            return await super().get_order_by_client_order_id(client_order_id)

    try:
        external = observe_external_order(repo, order=_external_order())
        trade = FillDuringLookup(lookup_result=_external_order(state="canceled", observed_at=NOON))
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY)
        assert repo.external_order(external.external_order_id).broker_state == "filled"
        record_fee_evidence(repo, [_external_fill()], checked_at_ms=NOON, history_complete=True)
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).order_claims == 600
    finally:
        repo.close()

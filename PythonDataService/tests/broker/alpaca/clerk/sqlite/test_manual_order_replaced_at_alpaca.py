"""A manual order Alpaca replaced, followed to its own ending (#2656).

The owner edits a Clerk manual order's price or quantity on Alpaca's own
website, and Alpaca reports the original ``replaced``: the order lives on
under a new broker id, so ``replaced`` is not an ending (#2647 stands). These
tests pin that the Clerk follows the replacement chain -- from ``replaced_by``
on the original and from a replacement's own execution, on the stream, the
sweep and a Clerk cancel alike -- ends the manual leg exactly once when the
chain's last order ends, credits each fill exactly once, never treats a
replacement as a foreign order, never moves the chain on a replacement that
did not take over (whatever fill count it reports), and contains a
replacement record it cannot follow.

Every sweep and cancel here runs through ``guard_broker_trade_port``, the
wrapper the runtime puts around the real port: a capability the guard does
not forward does not exist in production. Replacement orders carry
UUID-shaped broker ids and a client id Alpaca generated, as Alpaca's do.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.broker.alpaca.adapter import from_alpaca_order
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_trade_port
from app.broker.alpaca.clerk.sqlite.external_orders import unfoldable_broker_order_is_unreviewed
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    submit_manual_order_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_order_replacement import UNNAMED_REPLACEMENT_REASON
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission, submit_manual_order
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import _opt_ms_to_rfc3339
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.broker.contract.ports import BrokerTradePort
from app.schemas.manual_orders import ManualOrderLegResponse
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import FakeTrade as _AlpacaWebsite
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    _FakeRead,
    _NoReconciler,
    _position,
    _register_second_spy_lane,
    clocked_repo,  # noqa: F401 -- pytest fixture, used by name
)

_MANUAL_ENDINGS = {"MANUAL_ORDER_CANCELED", "MANUAL_ORDER_TERMINAL", "MANUAL_ORDER_FILLED"}
_REPLACED_NOTE = "Replaced at Alpaca; now following the new order."
_B = str(uuid.UUID(int=0xB))
_C = str(uuid.UUID(int=0xC))
_CANCEL_REQUEST_ID = "9a6d5b4c-3e2f-4a5b-8c9d-0e1f2a3b4c5d"


class _Website(_AlpacaWebsite):
    """Alpaca's site plus its order-replace endpoint and the broker-id GET.

    A replacement made on the website is a new order with a new broker id and
    a client order id Alpaca generated; the original reports ``replaced``
    with ``replaced_by``, the new order carries ``replaces``.
    """

    def __init__(self, *, repo: ClerkSqliteRepository) -> None:
        super().__init__(repo=repo)
        self.replacements: dict[str, BrokerOrder] = {}
        self.broker_id_lookups: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        """Alpaca books the order under a UUID broker id, as every link names one."""
        booked = await super().submit(leg, client_order_id=client_order_id)
        booked = booked.model_copy(update={"order_id": str(uuid.UUID(int=0xA0 + len(self.submit_calls)))})
        self.orders[client_order_id] = booked
        return booked

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        # alpaca-py refuses a non-UUID id client-side, before any request.
        uuid.UUID(order_id)
        self.broker_id_lookups.append(order_id)
        for order in self.orders.values():
            if order.order_id == order_id:
                return order
        return self.replacements.get(order_id)

    async def cancel(self, order_id: str) -> None:
        """Alpaca cancels the order now: its answer is stamped at the Clerk's clock, after every earlier one."""
        replacement = self.replacements.get(order_id)
        if replacement is not None:
            self.cancel_calls.append(order_id)
            self.replacements[order_id] = _ended(replacement, self.repo, status="canceled")
            return
        await super().cancel(order_id)
        for client_order_id, order in self.orders.items():
            if order.order_id == order_id:
                self.orders[client_order_id] = _ended(order, self.repo, status="canceled")


def _guarded(website: _Website) -> BrokerTradePort:
    """The trade port exactly as the runtime hands it to the Clerk."""
    return guard_broker_trade_port(website, intake=ReentrantAsyncLock())


async def _buy_limit(repo: ClerkSqliteRepository, website: _Website, *, quantity: float = 5) -> ManualOrderSubmission:
    """The owner's Clerk manual buy limit on SPY, working at Alpaca."""
    submitted = await submit_manual_order(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, ticket_id=TICKET_ID, leg_id=LEG_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity, order_type="limit",
                           limit_price=99.90, time_in_force="gtc"),
        trade=_guarded(website),
    )
    assert submitted.leg.order_ref is not None and submitted.leg.effect_operation_id is not None
    return submitted


def _replacement_of(
    original: BrokerOrder,
    repo: ClerkSqliteRepository,
    *,
    replacement_id: str,
    status: str = "new",
    quantity: float | None = None,
    limit_price: float | None = None,
) -> BrokerOrder:
    """The order Alpaca books for an edit: new broker id, Alpaca-generated client id."""
    now = repo.clock()
    return BrokerOrder(
        broker="alpaca", order_id=replacement_id, client_order_id=str(uuid.uuid4()),
        symbol=original.symbol, asset_class=original.asset_class, side=original.side,
        order_type=original.order_type, time_in_force=original.time_in_force,
        quantity=quantity if quantity is not None else original.quantity, filled_quantity=0,
        limit_price=limit_price if limit_price is not None else original.limit_price, stop_price=None,
        filled_avg_price=None, status=status, submitted_at_ms=now, created_at_ms=now, updated_at_ms=now,
        filled_at_ms=None, canceled_at_ms=None, expired_at_ms=None, events=[], observed_at_ms=now,
        replaces=original.order_id,
    )


def _replace_at_alpaca(
    repo: ClerkSqliteRepository,
    website: _Website,
    order_ref: str,
    *,
    replacement_id: str = _B,
    quantity: float | None = None,
    limit_price: float | None = None,
) -> tuple[BrokerOrder, BrokerOrder]:
    """The owner edits the order on Alpaca's site and the replace takes: the original is replaced."""
    now = repo.clock()
    original = website.orders[order_ref]
    replaced = original.model_copy(update={
        "status": "replaced", "replaced_by": replacement_id, "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = replaced
    replacement = _replacement_of(
        original, repo, replacement_id=replacement_id, quantity=quantity, limit_price=limit_price
    )
    website.replacements[replacement_id] = replacement
    return replaced, replacement


def _ended(broker_order: BrokerOrder, repo: ClerkSqliteRepository, *, status: str) -> BrokerOrder:
    now = repo.clock()
    return broker_order.model_copy(update={
        "status": status,
        "canceled_at_ms": now if status == "canceled" else None,
        "expired_at_ms": now if status == "expired" else None,
        "updated_at_ms": now,
        "observed_at_ms": now,
    })


def _filled(broker_order: BrokerOrder, repo: ClerkSqliteRepository, *, filled_quantity: float) -> BrokerOrder:
    now = repo.clock()
    return broker_order.model_copy(update={
        "status": "filled", "filled_quantity": filled_quantity, "filled_avg_price": 99.90,
        "filled_at_ms": now, "updated_at_ms": now, "observed_at_ms": now,
    })


async def _frame(repo: ClerkSqliteRepository, order: BrokerOrder, *, event_type: str,
                 execution_id: str | None = None, quantity: float | None = None,
                 event_key: str | None = None) -> str:
    """One ``trade_updates`` frame for ``order``, folded by the Clerk's sink."""
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler())
    return await sink.record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=BrokerOrderEvent(
            event_type=event_type, occurred_at_ms=order.updated_at_ms or repo.clock(),
            price=99.90 if execution_id else None, quantity=quantity, execution_id=execution_id,
        ),
        event_key=event_key or f"{event_type}:{execution_id or order.order_id}",
        order=order,
        recovery_source=None,
        recovery_window_limit=None,
    )


async def _reconciliation_pass(
    repo: ClerkSqliteRepository, website: _Website, *, open_orders: list[BrokerOrder] | None = None,
    spy_held: float = 0.0,
):
    return await reconcile_account(
        repo,
        read=_FakeRead(orders=open_orders or [], positions=[_position("SPY", quantity=spy_held)] if spy_held else []),
        trade=_guarded(website),
        pricing=UNPRICEABLE_RECOVERY,
    )


async def _clerk_cancel(repo: ClerkSqliteRepository, website: _Website, order_ref: str):
    return await submit_manual_order_cancellation(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, order_ref=order_ref,
        cancel_request_id=_CANCEL_REQUEST_ID, trade=_guarded(website),
    )


def _manual_endings(repo: ClerkSqliteRepository, order_ref: str) -> list[dict]:
    return [t for t in repo.transitions_for_order(order_ref) if t["transition_kind"] in _MANUAL_ENDINGS]


def _replaced_transitions(repo: ClerkSqliteRepository, order_ref: str) -> list[dict]:
    return [t for t in repo.transitions_for_order(order_ref) if t["transition_kind"] == "MANUAL_ORDER_REPLACED"]


def _owner_reads(repo: ClerkSqliteRepository) -> ManualOrderLegResponse:
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    return ManualOrderLegResponse.from_resource(ticket.legs[0], repo=repo)


def _another_bots_entry(repo: ClerkSqliteRepository, sid: str) -> tuple[bool, str | None]:
    decision = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=sid)
    return decision.allowed, decision.reason_code


def _account_hold_active(repo: ClerkSqliteRepository, reason_code: str) -> bool:
    return repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=reason_code, strategy_instance_id=None,
    ) is not None


def _unexplained_hold_active(repo: ClerkSqliteRepository) -> bool:
    return _account_hold_active(repo, UNEXPLAINED_ORDER_HOLD_REASON_CODE)


# ── The leg ends when the replacement ends ────────────────────────────────────


@pytest.mark.parametrize("route", ["trade_updates", "reconcile_sweep"])
async def test_a_replaced_manual_order_ends_when_its_replacement_is_cancelled(
    clocked_repo, route: str,  # noqa: F811
) -> None:
    """The chain's live order is cancelled; the manual effect ends and bots may enter."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")

    replaced, replacement = _replace_at_alpaca(repo, website, order_ref)
    if route == "trade_updates":
        await _frame(repo, replaced, event_type="replaced")
        effect = repo.effect_operation(effect_id)
        assert effect is not None and effect.state == "in_progress", "replaced is not an ending"
        assert repo.order(order_ref).broker_order_id == _B
        assert _owner_reads(repo).replacement_note == _REPLACED_NOTE
        await _frame(repo, replaced, event_type="replaced")  # a redelivery advances nothing
        assert len(_replaced_transitions(repo, order_ref)) == 1
        cancelled = _ended(replacement, repo, status="canceled")
        website.replacements[_B] = cancelled
        await _frame(repo, cancelled, event_type="canceled")
        assert repo.effect_operation(effect_id).state == "failed", "the frame itself must end the leg"
    else:
        website.replacements[_B] = _ended(replacement, repo, status="canceled")
        result = await _reconciliation_pass(repo, website)
        assert result.verdict == "clean"
        assert website.broker_id_lookups == [_B], "the sweep follows the chain through the guarded port"
    await _reconciliation_pass(repo, website)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed" and effect.terminal_receipt_id is not None
    assert not repo.has_nonterminal_manual_order()
    assert _another_bots_entry(repo, sid_b) == (True, None)
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_CANCELED"
    assert _owner_reads(repo).ending == "Cancelled at Alpaca."
    assert _owner_reads(repo).replacement_note is None, "the ticket stops naming a replacement once the leg ended"
    assert not _unexplained_hold_active(repo)


async def test_the_live_replacement_in_the_open_orders_snapshot_is_not_a_foreign_order(
    clocked_repo,  # noqa: F811
) -> None:
    """While the replacement works its client id is Alpaca's, yet it is ours.

    Without following the chain the sweep would judge it ``unexplained_order``
    and hold the whole account. With the chain it folds as the manual leg's
    own continuation; entries stay refused only for the honest reason -- the
    manual order is still outstanding.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    _replaced, replacement = _replace_at_alpaca(repo, website, order_ref)

    result = await _reconciliation_pass(repo, website, open_orders=[replacement])

    assert result.verdict == "clean"
    assert not _unexplained_hold_active(repo)
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "in_progress"
    assert repo.order(order_ref).broker_order_id == _B
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")


@pytest.mark.parametrize("replacement_status", ["pending_new", "new", "accepted"])
async def test_a_replacement_pending_beside_its_original_is_ours_but_moves_nothing(
    clocked_repo, replacement_status: str,  # noqa: F811
) -> None:
    """Alpaca holds the original in ``pending_replace`` until the new order is live.

    The new order, with the client id Alpaca generated, is in the open-orders
    snapshot beside it. It is not foreign -- no account hold -- yet nothing
    proves it took over, so the leg keeps following the original.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref = manual.leg.order_ref
    original = website.orders[order_ref]
    now = repo.clock()
    pending_replace = original.model_copy(update={"status": "pending_replace", "updated_at_ms": now})
    website.orders[order_ref] = pending_replace
    replacement = _replacement_of(original, repo, replacement_id=_B, status=replacement_status)

    result = await _reconciliation_pass(repo, website, open_orders=[pending_replace, replacement])

    assert result.verdict == "clean"
    assert not _unexplained_hold_active(repo)
    assert repo.order(order_ref).broker_order_id == original.order_id
    assert _replaced_transitions(repo, order_ref) == []


async def test_a_rejected_replacement_leaves_the_working_original_in_custody(clocked_repo) -> None:  # noqa: F811
    """A replace can lose at Alpaca: the new order is rejected, the original keeps working.

    ``replaces`` on the rejected order proves nothing. The leg stays on the
    original -- outstanding, entries refused, the sweep still watching it --
    and a Clerk cancel goes to the original, the order actually working.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    original = website.orders[order_ref]
    rejected = _replacement_of(original, repo, replacement_id=_B, status="rejected")
    website.replacements[_B] = rejected

    assert await _frame(repo, rejected, event_type="rejected") == "order_event"

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "in_progress"
    assert _manual_endings(repo, order_ref) == []
    assert repo.order(order_ref).broker_order_id == original.order_id
    assert _owner_reads(repo).ending is None
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")
    assert not _unexplained_hold_active(repo)

    result = await _reconciliation_pass(repo, website, open_orders=[original])
    assert result.verdict == "clean"
    assert repo.effect_operation(effect_id).state == "in_progress"

    cancelled = await _clerk_cancel(repo, website, order_ref)

    assert cancelled.cancellation.state == "SUCCEEDED"
    assert website.cancel_calls == [original.order_id]
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_CANCELED"


# ── Only the replacement's own execution proves it took over ─────────────────


def _original_partially_filled(
    repo: ClerkSqliteRepository, website: _Website, order_ref: str, *, filled_quantity: float
) -> BrokerOrder:
    """The original, still working, after ``filled_quantity`` of its shares filled."""
    now = repo.clock()
    partial = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": filled_quantity, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = partial
    return partial


def _replacement_carrying(
    original: BrokerOrder, repo: ClerkSqliteRepository, *, status: str, filled_quantity: float
) -> BrokerOrder:
    """A replacement Alpaca booked for an edit, reporting ``filled_quantity`` before any execution of its own."""
    return _replacement_of(original, repo, replacement_id=_B, status=status).model_copy(update={
        "filled_quantity": filled_quantity, "filled_avg_price": 99.90 if filled_quantity else None,
    })


async def _observe_pending_replacement(
    repo: ClerkSqliteRepository, website: _Website, route: str, *, original: BrokerOrder, pending: BrokerOrder,
) -> None:
    website.replacements[_B] = pending
    if route == "trade_updates":
        await _frame(repo, pending, event_type=pending.status)
    else:
        await _reconciliation_pass(
            repo, website, open_orders=[original, pending], spy_held=original.filled_quantity,
        )


@pytest.mark.parametrize("carried_status", ["pending_new", "accepted", "new"])
@pytest.mark.parametrize("route", ["trade_updates", "reconcile_sweep"])
async def test_fills_a_replacement_carries_from_its_original_never_move_the_chain(
    clocked_repo, route: str, carried_status: str,  # noqa: F811
) -> None:
    """A fills 2 of 5; the owner's edit books B reporting A's 2 shares; the replace is then rejected.

    A replacement's cumulative fill count can be the chain's, not its own --
    so it proves nothing, whatever it says. When the replace loses, A keeps
    working its last 3 shares: the leg stays on A -- outstanding, entries
    refused, no ending -- and a Clerk cancel DELETEs A, the order working.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    a_partial = _original_partially_filled(repo, website, order_ref, filled_quantity=2)
    await _frame(repo, a_partial, event_type="partial_fill", execution_id="exec-a", quantity=2)
    pending_b = _replacement_carrying(a_partial, repo, status=carried_status, filled_quantity=2)

    await _observe_pending_replacement(repo, website, route, original=a_partial, pending=pending_b)
    assert repo.order(order_ref).broker_order_id == a_partial.order_id, "carried fills moved the chain"
    rejected_b = _ended(pending_b, repo, status="rejected")
    website.replacements[_B] = rejected_b
    await _frame(repo, rejected_b, event_type="rejected")
    result = await _reconciliation_pass(repo, website, open_orders=[a_partial], spy_held=2.0)

    assert result.verdict == "clean"
    assert repo.order(order_ref).broker_order_id == a_partial.order_id
    assert _replaced_transitions(repo, order_ref) == []
    assert repo.effect_operation(effect_id).state == "in_progress"
    assert _manual_endings(repo, order_ref) == []
    assert _owner_reads(repo).ending is None
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")
    assert not _unexplained_hold_active(repo)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 2.0}, abs=1e-9, rel=0)

    cancelled = await _clerk_cancel(repo, website, order_ref)

    assert cancelled.cancellation.state == "SUCCEEDED"
    assert website.cancel_calls == [a_partial.order_id]
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_CANCELED"]


@pytest.mark.parametrize("route", ["trade_updates", "reconcile_sweep"])
async def test_an_original_that_fills_before_its_replacement_reaches_the_venue_ends_the_leg_filled(
    clocked_repo, route: str,  # noqa: F811
) -> None:
    """Alpaca's documented race: the original fills first, so its replacement is rejected.

    B was booked reporting A's first 2 shares; A then fills its other 3. All
    5 shares filled on A, the order the leg still follows: the leg ends
    filled, once, and B's rejection -- a replace that never took -- ends
    nothing.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    a_partial = _original_partially_filled(repo, website, order_ref, filled_quantity=2)
    await _frame(repo, a_partial, event_type="partial_fill", execution_id="exec-a1", quantity=2)
    pending_b = _replacement_carrying(a_partial, repo, status="pending_new", filled_quantity=2)
    await _observe_pending_replacement(repo, website, route, original=a_partial, pending=pending_b)

    a_filled = _filled(a_partial, repo, filled_quantity=5)
    website.orders[order_ref] = a_filled
    await _frame(repo, a_filled, event_type="fill", execution_id="exec-a2", quantity=3)
    rejected_b = _ended(pending_b, repo, status="rejected")
    website.replacements[_B] = rejected_b
    await _frame(repo, rejected_b, event_type="rejected")
    await _reconciliation_pass(repo, website, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _owner_reads(repo).ending is None, "a filled leg names no unfilled ending"
    assert repo.order(order_ref).broker_order_id == a_filled.order_id
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert _another_bots_entry(repo, sid_b) == (True, None)


@pytest.mark.parametrize("carried", [True, False], ids=["filled_qty_carried", "filled_qty_own"])
async def test_the_replacements_own_execution_moves_the_chain_to_it(
    clocked_repo, carried: bool,  # noqa: F811
) -> None:
    """B's first execution on its own frame is the proof it took A's place.

    Whether or not Alpaca carries A's 2 shares into B's ``filled_qty``, B's
    pending report moves nothing and its own execution moves the head, once.
    The leg then completes on B's ``qty`` -- the chain's total (Alpaca
    refuses a replace whose qty is not above what already filled) -- when
    the exact executions across the chain cover it.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    a_partial = _original_partially_filled(repo, website, order_ref, filled_quantity=2)
    await _frame(repo, a_partial, event_type="partial_fill", execution_id="exec-a", quantity=2)
    carried_quantity = 2 if carried else 0
    pending_b = _replacement_carrying(a_partial, repo, status="pending_new", filled_quantity=carried_quantity)
    await _frame(repo, pending_b, event_type="pending_new")
    assert repo.order(order_ref).broker_order_id == a_partial.order_id

    b_partial = pending_b.model_copy(update={
        "status": "partially_filled", "filled_quantity": carried_quantity + 1, "filled_avg_price": 99.90,
    })
    await _frame(repo, b_partial, event_type="partial_fill", execution_id="exec-b1", quantity=1)

    assert repo.order(order_ref).broker_order_id == _B
    assert len(_replaced_transitions(repo, order_ref)) == 1
    assert repo.effect_operation(effect_id).state == "in_progress"
    website.orders[order_ref] = a_partial.model_copy(update={"status": "replaced", "replaced_by": _B})
    b_filled = _filled(b_partial, repo, filled_quantity=carried_quantity + 3)
    website.replacements[_B] = b_filled
    await _frame(repo, b_filled, event_type="fill", execution_id="exec-b2", quantity=2)
    await _reconciliation_pass(repo, website, spy_held=5.0)

    assert repo.effect_operation(effect_id).state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert len(_replaced_transitions(repo, order_ref)) == 1
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert _another_bots_entry(repo, sid_b) == (True, None)


# ── Fills credit the leg exactly once ─────────────────────────────────────────


async def test_a_two_hop_chain_ends_once_credits_the_fill_once_and_its_late_frames_hold_nothing(
    clocked_repo,  # noqa: F811
) -> None:
    """A replaced by B, B replaced by C, C filled: one ending, one credit.

    Frames the stream redelivers afterwards -- C's fill, and a stale frame of
    B from before it was replaced -- belong to the ended leg's chain, so they
    are neither foreign nor able to restate anything.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)

    replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref)
    await _frame(repo, replaced_a, event_type="replaced")
    replaced_b = replacement_b.model_copy(update={"status": "replaced", "replaced_by": _C})
    website.replacements[_B] = replaced_b
    await _frame(repo, replaced_b, event_type="replaced")
    assert repo.order(order_ref).broker_order_id == _C
    assert len(_replaced_transitions(repo, order_ref)) == 2

    filled_c = _filled(_replacement_of(replaced_b, repo, replacement_id=_C), repo, filled_quantity=5)
    website.replacements[_C] = filled_c
    await _frame(repo, filled_c, event_type="fill", execution_id="exec-c", quantity=5)
    await _frame(repo, filled_c, event_type="fill", execution_id="exec-c", quantity=5,
                 event_key="fill:exec-c:redelivery")
    assert await _frame(repo, replacement_b, event_type="new", event_key="new:b:stale") == "order_event"

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_FILLED"
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and ticket.state == "COMPLETED"
    assert not _unexplained_hold_active(repo)
    assert _another_bots_entry(repo, sid_b) == (True, None)

    await _reconciliation_pass(repo, website, spy_held=5.0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert len(_manual_endings(repo, order_ref)) == 1


async def test_a_replayed_ending_of_a_replaced_leg_holds_nothing(clocked_repo) -> None:  # noqa: F811
    """A reconnecting stream replays the replacement's ``canceled`` after the leg ended."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    replaced, replacement = _replace_at_alpaca(repo, website, order_ref)
    await _frame(repo, replaced, event_type="replaced")
    cancelled = _ended(replacement, repo, status="canceled")
    website.replacements[_B] = cancelled
    await _frame(repo, cancelled, event_type="canceled")
    assert repo.effect_operation(effect_id).state == "failed"

    assert await _frame(repo, cancelled, event_type="canceled", event_key="canceled:redelivery") == "order_event"
    assert await _frame(repo, replaced, event_type="replaced", event_key="replaced:redelivery") == "order_event"

    assert not _unexplained_hold_active(repo)
    assert _another_bots_entry(repo, sid_b) == (True, None)
    assert len(_manual_endings(repo, order_ref)) == 1


async def test_a_partial_fill_on_the_original_and_the_rest_on_its_replacement_credit_once(
    clocked_repo,  # noqa: F811
) -> None:
    """A fills 2 of 5; the owner's edit books B, which fills the other 3.

    B's fill frame reaches the Clerk before A's ``replaced`` frame: B's own
    execution is the proof it took over, so the chain advances on it.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    a_partial = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = a_partial
    await _frame(repo, a_partial, event_type="partial_fill", execution_id="exec-a", quantity=2)
    replaced, replacement = _replace_at_alpaca(repo, website, order_ref)
    b_filled = _filled(replacement, repo, filled_quantity=5)
    website.replacements[_B] = b_filled

    await _frame(repo, b_filled, event_type="fill", execution_id="exec-b", quantity=3)
    await _frame(repo, replaced, event_type="replaced")
    await _reconciliation_pass(repo, website, spy_held=5.0)

    assert repo.effect_operation(effect_id).state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert repo.order(order_ref).broker_order_id == _B


async def test_a_replacement_with_an_edited_quantity_that_fills_ends_the_leg(clocked_repo) -> None:  # noqa: F811
    """The owner raised the quantity at Alpaca; the replacement fills in full."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website, quantity=5)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id

    replaced, replacement = _replace_at_alpaca(repo, website, order_ref, quantity=10, limit_price=99.95)
    await _frame(repo, replaced, event_type="replaced")
    filled = _filled(replacement, repo, filled_quantity=10)
    website.replacements[_B] = filled
    await _frame(repo, filled, event_type="fill", execution_id="exec-10", quantity=10)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 10.0}, abs=1e-9, rel=0)
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]


async def test_a_raised_quantity_is_not_complete_on_exact_fills_that_reach_only_the_original(
    clocked_repo,  # noqa: F811
) -> None:
    """The head filled 10 but exact executions cover only the leg's original 5.

    The governing quantity is the head's own, never either of two: the leg
    stays open for the missing exact executions instead of ending
    ``MANUAL_ORDER_FILLED`` on 5 of 10.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website, quantity=5)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    replaced, replacement = _replace_at_alpaca(repo, website, order_ref, quantity=10)
    await _frame(repo, replaced, event_type="replaced")
    partial = replacement.model_copy(update={
        "status": "partially_filled", "filled_quantity": 5, "filled_avg_price": 99.90,
    })
    await _frame(repo, partial, event_type="partial_fill", execution_id="exec-5", quantity=5)
    website.replacements[_B] = _filled(replacement, repo, filled_quantity=10)

    await _reconciliation_pass(repo, website, spy_held=10.0)

    assert repo.effect_operation(effect_id).state == "in_progress"
    assert _manual_endings(repo, order_ref) == []
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 10.0}, abs=1e-9, rel=0)


# ── One acknowledgement per head, not one per member per sweep ───────────────


def _order_evidence(repo: ClerkSqliteRepository, order_ref: str) -> list[dict]:
    """The order's journal minus the sweep's per-pass receipt, which any working order gets."""
    return [
        t for t in repo.transitions_for_order(order_ref) if t["transition_kind"] != "RECONCILIATION_ATTEMPTED"
    ]


async def test_repeated_sweeps_over_an_unchanged_replacement_append_nothing(clocked_repo) -> None:  # noqa: F811
    """Each sweep re-reads the original by client id and the head by broker id.

    The original's answer is a former member's: it records its link once and
    nothing more, so an unchanged chain costs no journal rows per sweep.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref = manual.leg.order_ref
    _replaced, replacement = _replace_at_alpaca(repo, website, order_ref)

    await _reconciliation_pass(repo, website, open_orders=[replacement])
    settled = _order_evidence(repo, order_ref)
    for _ in range(4):
        clock.value += 15_000
        await _reconciliation_pass(repo, website, open_orders=[replacement])

    assert _order_evidence(repo, order_ref) == settled
    acks = [t for t in settled if t["transition_kind"] == "ORDER_SUBMIT_ACKED"]
    assert acks[-1]["broker_order_id"] == _B
    assert all(t["broker_state"] != "replaced" for t in acks), "a former member never restates the head"


# ── A Clerk cancel follows the chain to the live order ────────────────────────


async def test_a_clerk_cancel_of_a_replaced_order_cancels_the_live_replacement(
    clocked_repo,  # noqa: F811
) -> None:
    """Not refused as terminal: the leg lives on, so the DELETE goes to the head."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    _replace_at_alpaca(repo, website, order_ref)

    cancelled = await _clerk_cancel(repo, website, order_ref)

    assert cancelled.cancellation.state == "SUCCEEDED"
    assert website.cancel_calls == [_B]
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed"
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_CANCELED"
    assert _owner_reads(repo).ending == "Cancelled."
    assert not repo.has_nonterminal_manual_order()
    assert _another_bots_entry(repo, _register_second_spy_lane(repo)[0]) == (True, None)


class _FlakyHeadWebsite(_Website):
    """Alpaca's broker-id GET times out for the replacement, only while ``flaky``."""

    def __init__(self, *, repo: ClerkSqliteRepository, fail_after_cancel: bool = False) -> None:
        super().__init__(repo=repo)
        self.flaky = True
        self.fail_after_cancel = fail_after_cancel

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        if self.flaky and (not self.fail_after_cancel or self.cancel_calls):
            raise BrokerUnavailable("GET /v2/orders/{order_id} timed out", broker="alpaca")
        return await super().get_order_by_broker_order_id(order_id)


async def test_a_failed_read_of_the_replacement_after_the_delete_is_an_uncertain_cancel(
    clocked_repo,  # noqa: F811
) -> None:
    """The DELETE reached Alpaca but the head's confirming read timed out.

    The cancel records ``ORDER_CANCEL_UNCERTAIN`` rather than raising, and a
    later resume with Alpaca answering proves it.
    """
    repo, _clock = clocked_repo
    website = _FlakyHeadWebsite(repo=repo, fail_after_cancel=True)
    manual = await _buy_limit(repo, website)
    order_ref = manual.leg.order_ref
    _replace_at_alpaca(repo, website, order_ref)

    cancelled = await _clerk_cancel(repo, website, order_ref)

    assert website.cancel_calls == [_B]
    assert cancelled.cancellation.state == "UNKNOWN"
    assert any(
        t["transition_kind"] == "ORDER_CANCEL_UNCERTAIN" for t in repo.transitions_for_order(order_ref)
    )

    website.flaky = False
    await _reconciliation_pass(repo, website)
    assert repo.manual_order_cancellation(order_ref=order_ref).state == "SUCCEEDED"
    assert website.cancel_calls == [_B], "a proven cancel is never re-sent"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_CANCELED"]


async def test_a_failed_read_of_the_replacement_is_contained_to_its_own_leg(clocked_repo) -> None:  # noqa: F811
    """A flaky broker-id GET never aborts the account's pass (#2656 acceptance criterion).

    The leg folds uncertain on its own, the sweep still reaches its verdict
    with no ``RECONCILIATION_INCOMPLETE`` hold, and the next pass settles it.
    """
    repo, _clock = clocked_repo
    website = _FlakyHeadWebsite(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    _replaced, replacement = _replace_at_alpaca(repo, website, order_ref)

    result = await _reconciliation_pass(repo, website)

    assert result.verdict == "clean"
    assert not _account_hold_active(repo, RECONCILIATION_INCOMPLETE_REASON_CODE)
    assert repo.effect_operation(effect_id).state == "unknown"
    assert _order_evidence(repo, order_ref)[-1]["transition_kind"] == "ORDER_SUBMIT_UNCERTAIN"

    website.flaky = False
    website.replacements[_B] = _ended(replacement, repo, status="canceled")
    await _reconciliation_pass(repo, website)
    assert repo.effect_operation(effect_id).state == "failed"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_CANCELED"]


# ── A replacement the Clerk cannot follow is contained ────────────────────────


@pytest.mark.parametrize("replaced_by", [None, ""])
async def test_a_replacement_named_by_nothing_is_contained_and_never_freezes_the_sweep(
    clocked_repo, replaced_by: str | None, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """Alpaca reports ``replaced`` readably but names no replacement in ``replaced_by``.

    Nothing raises and the sweep reaches its verdict, pass after pass. The
    leg stays honestly outstanding on the original -- no link to follow --
    and the order is named on #2363's entry fence with that cause, durable
    and owner-visible, once: a sweep re-seeing it appends nothing.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    replaced = website.orders[order_ref].model_copy(update={
        "status": "replaced", "replaced_by": replaced_by, "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = replaced

    with caplog.at_level(logging.WARNING):
        await _frame(repo, replaced, event_type="replaced")
    result = await _reconciliation_pass(repo, website)
    journal_after_first_pass = len(repo.custody_transitions())
    clock.value += 15_000
    again = await _reconciliation_pass(repo, website)

    assert result.verdict == "clean" and again.verdict == "clean"
    assert website.broker_id_lookups == []
    assert repo.effect_operation(effect_id).state == "in_progress"
    assert _replaced_transitions(repo, order_ref) == []
    assert unfoldable_broker_order_is_unreviewed(repo, broker_order_id=replaced.order_id)
    fence = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE, strategy_instance_id=None,
    )
    assert fence is not None and UNNAMED_REPLACEMENT_REASON in fence["facts_json"]
    assert any(
        getattr(record, "action", None) == "manual_order_replacement_unfollowable" for record in caplog.records
    )
    assert len(repo.custody_transitions()) - journal_after_first_pass <= 1, "only the sweep's own receipt"
    assert _owner_reads(repo).replacement_note is None


# ── A row the adapter could not fully read never moves the chain (#2648) ──────


def _from_alpaca(order: BrokerOrder, **wire: object) -> BrokerOrder:
    """``order`` as Alpaca's wire carries it, ``wire`` overriding raw fields, mapped by the real adapter."""
    payload: dict[str, object] = {
        "id": order.order_id, "client_order_id": order.client_order_id, "symbol": order.symbol,
        "asset_class": order.asset_class, "side": order.side, "order_type": order.order_type,
        "time_in_force": order.time_in_force,
        "qty": None if order.quantity is None else str(order.quantity),
        "filled_qty": str(order.filled_quantity),
        "limit_price": None if order.limit_price is None else str(order.limit_price),
        "stop_price": None,
        "filled_avg_price": None if order.filled_avg_price is None else str(order.filled_avg_price),
        "status": order.status, "extended_hours": False,
        "submitted_at": _opt_ms_to_rfc3339(order.submitted_at_ms),
        "created_at": _opt_ms_to_rfc3339(order.created_at_ms),
        "updated_at": _opt_ms_to_rfc3339(order.updated_at_ms),
        "filled_at": _opt_ms_to_rfc3339(order.filled_at_ms),
        "canceled_at": _opt_ms_to_rfc3339(order.canceled_at_ms),
        "expired_at": _opt_ms_to_rfc3339(order.expired_at_ms),
        "replaced_by": order.replaced_by, "replaces": order.replaces,
    }
    return from_alpaca_order({**payload, **wire}, observed_at_ms=order.observed_at_ms)


def _withheld_why(repo: ClerkSqliteRepository, order_ref: str) -> str:
    [latest] = [t for t in _order_evidence(repo, order_ref) if t["transition_kind"] == "ORDER_SUBMIT_UNCERTAIN"][-1:]
    return latest["facts_json"]


@pytest.mark.parametrize("wire", [{"filled_qty": "five"}, {"filled_qty": True}, {"updated_at": "later"}])
async def test_an_unreadable_replacement_row_neither_moves_the_chain_nor_credits_a_fill(
    clocked_repo, wire: dict[str, object],  # noqa: F811
) -> None:
    """The replacement's fill frame reaches the Clerk with a value the adapter could not read.

    Its execution would be the proof it took over -- but a row with *any*
    unreadable value proves nothing, whatever its readable fields say.
    #2679's gate withholds it by the field it names: the original stays the
    head, nothing is credited, the leg does not end.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    degraded = _from_alpaca(
        _filled(_replacement_of(original, repo, replacement_id=_B), repo, filled_quantity=5), **wire
    )
    assert degraded.unreadable_fields == tuple(wire)

    disposition = await _frame(repo, degraded, event_type="fill", execution_id="exec-b", quantity=5)

    assert disposition == "withheld_order"
    assert repo.order(order_ref).broker_order_id == original.order_id
    assert _replaced_transitions(repo, order_ref) == []
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(0.0, abs=1e-9, rel=0)
    assert _manual_endings(repo, order_ref) == []
    assert repo.effect_operation(effect_id).state == "unknown"
    assert next(iter(wire)) in _withheld_why(repo, order_ref)
    assert not _unexplained_hold_active(repo)


@pytest.mark.parametrize("replaced_by", ["not-a-uuid", 7])
async def test_an_unreadable_replaced_by_is_withheld_by_the_one_gate_and_never_followed(
    clocked_repo, replaced_by: object,  # noqa: F811
) -> None:
    """A link id that is not a UUID string is an unreadable value, not a replacement to contain.

    The adapter names ``replaced_by`` unreadable; every route withholds the
    answer through #2679's gate, which names that field, so there is one
    containment for it -- no chain link, no broker-id read, no entry fence of
    the replacement path -- and the sweep never freezes.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    degraded = _from_alpaca(
        website.orders[order_ref].model_copy(update={"status": "replaced", "updated_at_ms": now, "observed_at_ms": now}),
        replaced_by=replaced_by,
    )
    assert degraded.unreadable_fields == ("replaced_by",) and degraded.replaced_by is None
    website.orders[order_ref] = degraded

    assert await _frame(repo, degraded, event_type="replaced") == "withheld_order"
    result = await _reconciliation_pass(repo, website)

    assert result.verdict == "clean"
    assert website.broker_id_lookups == []
    assert _replaced_transitions(repo, order_ref) == []
    assert repo.effect_operation(effect_id).state == "unknown"
    assert "replaced_by" in _withheld_why(repo, order_ref)
    assert not unfoldable_broker_order_is_unreviewed(repo, broker_order_id=degraded.order_id)


async def test_a_replaced_report_with_an_unreadable_fill_count_never_links(clocked_repo) -> None:  # noqa: F811
    """The original names a readable ``replaced_by``, but its fill count would not parse.

    A partial fill on the original must not be lost behind a link: the whole
    answer is withheld and the chain waits for a readable one.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref = manual.leg.order_ref
    replaced, _replacement = _replace_at_alpaca(repo, website, order_ref)
    degraded = _from_alpaca(replaced, filled_qty="two")

    assert await _frame(repo, degraded, event_type="replaced") == "withheld_order"

    assert repo.order(order_ref).broker_order_id == replaced.order_id
    assert _replaced_transitions(repo, order_ref) == []

    await _frame(repo, replaced, event_type="replaced", event_key="replaced:readable")
    assert repo.order(order_ref).broker_order_id == _B

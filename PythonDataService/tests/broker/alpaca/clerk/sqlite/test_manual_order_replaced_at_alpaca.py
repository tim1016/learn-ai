"""A manual order Alpaca replaced, followed to its own ending (#2656).

The owner edits a Clerk manual order's price or quantity on Alpaca's own
website, and Alpaca reports the original ``replaced``: the order lives on
under a new broker id, so ``replaced`` is not an ending (#2647 stands). These
tests pin that the Clerk follows the replacement chain -- from ``replaced_by``
on the original and from a replacement's own fills, on the stream, the sweep
and a Clerk cancel alike -- ends the manual leg exactly once when the chain's
last order ends, credits each fill exactly once, never treats a replacement
as a foreign order, never moves the chain on a replacement that did not take
over, and contains a replacement record it cannot follow.

Every sweep and cancel here runs through ``guard_broker_trade_port``, the
wrapper the runtime puts around the real port: a capability the guard does
not forward does not exist in production. Replacement orders carry
UUID-shaped broker ids and a client id Alpaca generated, as Alpaca's do.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_trade_port
from app.broker.alpaca.clerk.sqlite.external_orders import unfoldable_broker_order_is_unreviewed
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    submit_manual_order_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission, submit_manual_order
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
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

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        # alpaca-py refuses a non-UUID id client-side, before any request.
        uuid.UUID(order_id)
        self.broker_id_lookups.append(order_id)
        for order in self.orders.values():
            if order.order_id == order_id:
                return order
        return self.replacements.get(order_id)

    async def cancel(self, order_id: str) -> None:
        replacement = self.replacements.get(order_id)
        if replacement is not None:
            self.cancel_calls.append(order_id)
            self.replacements[order_id] = _ended(replacement, self.repo, status="canceled")
            return
        await super().cancel(order_id)


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

    B's fill frame reaches the Clerk before A's ``replaced`` frame: B's fills
    are the proof it took over, so the chain advances on them.
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


@pytest.mark.parametrize("replaced_by", [None, "", "not-a-uuid"])
async def test_a_replacement_the_clerk_cannot_follow_is_contained_and_never_freezes_the_sweep(
    clocked_repo, replaced_by: str | None, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """Alpaca reports ``replaced`` naming no replacement id the Clerk can follow.

    Nothing raises and the sweep reaches its verdict, pass after pass. The
    leg stays honestly outstanding on the original -- no link to follow --
    and the order is named on #2363's entry fence, durable and owner-visible,
    once: a sweep re-seeing it appends nothing.
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
    assert any(
        getattr(record, "action", None) == "manual_order_replacement_unfollowable" for record in caplog.records
    )
    assert len(repo.custody_transitions()) - journal_after_first_pass <= 1, "only the sweep's own receipt"
    assert _owner_reads(repo).replacement_note is None


async def test_a_working_manual_order_shows_no_replacement_note(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    await _buy_limit(repo, website)
    assert _owner_reads(repo).replacement_note is None

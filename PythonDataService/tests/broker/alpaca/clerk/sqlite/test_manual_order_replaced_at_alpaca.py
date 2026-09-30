"""A manual order Alpaca replaced, followed to its own ending (#2656).

The owner edits a Clerk manual order's price or quantity on Alpaca's own
website, and Alpaca reports the original ``replaced``: the order lives on
under a new broker id, so ``replaced`` is not an ending (#2647 stands). These
tests pin that the Clerk follows the replacement chain -- from ``replaced_by``
on the original and from ``replaces`` on the new order, on the stream, the
sweep and a Clerk cancel alike -- ends the manual leg exactly once when the
chain's last order ends, credits each fill exactly once, never treats the
live replacement as a foreign order, and contains a replacement record it
cannot read.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    submit_manual_order_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission, submit_manual_order
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import UNEXPLAINED_ORDER_HOLD_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
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


class _Website(_AlpacaWebsite):
    """Alpaca's site plus its order-replace endpoint and a broker-id GET.

    A replacement made on the website is a new order with a new broker id
    and no client order id of ours; the original reports ``replaced`` with
    ``replaced_by``, the new order carries ``replaces``.
    """

    def __init__(self, *, repo: ClerkSqliteRepository) -> None:
        super().__init__(repo=repo)
        self.replacements: dict[str, BrokerOrder] = {}

    async def get_order(self, order_id: str) -> BrokerOrder | None:
        for order in self.orders.values():
            if order.order_id == order_id:
                return order
        return self.replacements.get(order_id)

    async def cancel(self, order_id: str) -> None:
        replacement = self.replacements.get(order_id)
        if replacement is not None:
            self.cancel_calls.append(order_id)
            now = self.repo.clock()
            self.replacements[order_id] = replacement.model_copy(
                update={
                    "status": "canceled",
                    "canceled_at_ms": now,
                    "updated_at_ms": now,
                    "observed_at_ms": now,
                }
            )
            return
        await super().cancel(order_id)


async def _buy_limit(repo: ClerkSqliteRepository, website: _Website, *, quantity: float = 5) -> ManualOrderSubmission:
    """The owner's Clerk manual buy limit on SPY, working at Alpaca."""
    submitted = await submit_manual_order(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, ticket_id=TICKET_ID, leg_id=LEG_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity, order_type="limit",
                           limit_price=99.90, time_in_force="gtc"),
        trade=website,
    )
    assert submitted.leg.order_ref is not None and submitted.leg.effect_operation_id is not None
    return submitted


def _replace_at_alpaca(
    repo: ClerkSqliteRepository,
    website: _Website,
    order_ref: str,
    *,
    replacement_id: str,
    quantity: float | None = None,
    limit_price: float | None = None,
) -> tuple[BrokerOrder, BrokerOrder]:
    """The owner edits the order on Alpaca's site: the original is replaced."""
    now = repo.clock()
    original = website.orders[order_ref]
    replaced = original.model_copy(update={
        "status": "replaced",
        "replaced_by": replacement_id,
        "updated_at_ms": now,
        "observed_at_ms": now,
    })
    website.orders[order_ref] = replaced
    replacement = BrokerOrder(
        broker="alpaca",
        order_id=replacement_id,
        client_order_id=None,
        symbol=original.symbol,
        asset_class=original.asset_class,
        side=original.side,
        order_type=original.order_type,
        time_in_force=original.time_in_force,
        quantity=quantity if quantity is not None else original.quantity,
        filled_quantity=0,
        limit_price=limit_price if limit_price is not None else original.limit_price,
        stop_price=None,
        filled_avg_price=None,
        status="new",
        submitted_at_ms=now,
        created_at_ms=now,
        updated_at_ms=now,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=now,
        replaces=original.order_id,
    )
    website.replacements[replacement_id] = replacement
    return replaced, replacement


def _ended(broker_order: BrokerOrder, repo: ClerkSqliteRepository, *, status: str) -> BrokerOrder:
    now = repo.clock()
    ended = broker_order.model_copy(update={
        "status": status,
        "canceled_at_ms": now if status == "canceled" else None,
        "expired_at_ms": now if status == "expired" else None,
        "updated_at_ms": now,
        "observed_at_ms": now,
    })
    return ended


async def _frame(repo: ClerkSqliteRepository, order: BrokerOrder, *, event_type: str,
                 execution_id: str | None = None, quantity: float | None = None,
                 event_key: str | None = None) -> None:
    """One ``trade_updates`` frame for ``order``, folded by the Clerk's sink.

    ``order.client_order_id`` may be ``None``: a website replacement carries
    no client id of ours, so the frame must resolve through the chain.
    """
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler())
    await sink.record_lifecycle_event(
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
    repo: ClerkSqliteRepository, trade: _Website, *, open_orders: list[BrokerOrder] | None = None,
    spy_held: float = 0.0,
):
    return await reconcile_account(repo, read=_FakeRead(orders=open_orders or [], positions=[
        _position("SPY", quantity=spy_held)] if spy_held else []), trade=trade,
        pricing=UNPRICEABLE_RECOVERY)


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


def _unexplained_hold_active(repo: ClerkSqliteRepository) -> bool:
    return repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE,
        strategy_instance_id=None,
    ) is not None


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

    replaced, replacement = _replace_at_alpaca(repo, website, order_ref, replacement_id="repl-1")
    if route == "trade_updates":
        await _frame(repo, replaced, event_type="replaced")
        effect = repo.effect_operation(effect_id)
        assert effect is not None and effect.state == "in_progress", "replaced is not an ending"
        assert repo.order(order_ref).broker_order_id == "repl-1"
        assert _owner_reads(repo).replacement_note == _REPLACED_NOTE
        await _frame(repo, replaced, event_type="replaced")  # a redelivery advances nothing
        assert len(_replaced_transitions(repo, order_ref)) == 1
        cancelled = _ended(replacement, repo, status="canceled")
        website.replacements["repl-1"] = cancelled
        await _frame(repo, cancelled, event_type="canceled")
        assert repo.effect_operation(effect_id).state == "failed", "the frame itself must end the leg"
    else:
        cancelled = _ended(replacement, repo, status="canceled")
        website.replacements["repl-1"] = cancelled
        result = await _reconciliation_pass(repo, website)
        assert result.verdict == "clean"
    await _reconciliation_pass(repo, website)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed" and effect.terminal_receipt_id is not None
    assert not repo.has_nonterminal_manual_order()
    assert _another_bots_entry(repo, sid_b) == (True, None)
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_CANCELED"
    assert _owner_reads(repo).ending == "Cancelled at Alpaca."
    assert _owner_reads(repo).replacement_note is None, "the ticket stops naming a replacement once the leg ended"


async def test_the_live_replacement_in_the_open_orders_snapshot_is_not_a_foreign_order(
    clocked_repo,  # noqa: F811
) -> None:
    """While the replacement works it has no client id of ours, yet it is ours.

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
    _replaced, replacement = _replace_at_alpaca(repo, website, order_ref, replacement_id="repl-1")

    result = await _reconciliation_pass(repo, website, open_orders=[replacement])

    assert result.verdict == "clean"
    assert not _unexplained_hold_active(repo)
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "in_progress"
    assert repo.order(order_ref).broker_order_id == "repl-1"
    assert _another_bots_entry(repo, sid_b) == (False, "MANUAL_ORDER_OUTSTANDING")


async def test_a_two_hop_chain_ends_once_and_credits_the_fill_once(clocked_repo) -> None:  # noqa: F811
    """A replaced by B, B replaced by C, C filled: one ending, one credit."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id

    replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref, replacement_id="repl-1")
    await _frame(repo, replaced_a, event_type="replaced")
    replaced_b = replacement_b.model_copy(update={"status": "replaced", "replaced_by": "repl-2"})
    website.replacements["repl-1"] = replaced_b
    await _frame(repo, replaced_b, event_type="replaced")
    assert repo.order(order_ref).broker_order_id == "repl-2"
    assert len(_replaced_transitions(repo, order_ref)) == 2

    now = repo.clock()
    filled_c = BrokerOrder(
        broker="alpaca", order_id="repl-2", client_order_id=None, symbol="SPY",
        asset_class="us_equity", side="buy", order_type="limit", time_in_force="gtc",
        quantity=5, filled_quantity=5, limit_price=99.95, stop_price=None,
        filled_avg_price=99.95, status="filled", submitted_at_ms=now, created_at_ms=now,
        updated_at_ms=now, filled_at_ms=now, canceled_at_ms=None, expired_at_ms=None,
        events=[], observed_at_ms=now, replaces="repl-1",
    )
    website.replacements["repl-2"] = filled_c
    await _frame(repo, filled_c, event_type="fill", execution_id="exec-c", quantity=5)
    await _frame(repo, filled_c, event_type="fill", execution_id="exec-c", quantity=5,
                 event_key="fill:exec-c:redelivery")

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_FILLED"
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and ticket.state == "COMPLETED"


async def test_a_replacement_with_an_edited_quantity_that_fills_ends_the_leg(clocked_repo) -> None:  # noqa: F811
    """The owner raised the quantity at Alpaca; the replacement fills in full."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website, quantity=5)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id

    replaced, replacement = _replace_at_alpaca(repo, website, order_ref, replacement_id="repl-1",
                                               quantity=10, limit_price=99.95)
    await _frame(repo, replaced, event_type="replaced")
    now = repo.clock()
    filled = replacement.model_copy(update={
        "status": "filled", "filled_quantity": 10, "filled_avg_price": 99.95, "filled_at_ms": now,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.replacements["repl-1"] = filled
    await _frame(repo, filled, event_type="fill", execution_id="exec-10", quantity=10)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 10.0}, abs=1e-9, rel=0)
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]


# ── A Clerk cancel follows the chain to the live order ────────────────────────


async def test_a_clerk_cancel_of_a_replaced_order_cancels_the_live_replacement(
    clocked_repo,  # noqa: F811
) -> None:
    """Not refused as terminal: the leg lives on, so the DELETE goes to the head."""
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    _replaced, _replacement = _replace_at_alpaca(repo, website, order_ref, replacement_id="repl-1")

    cancelled = await submit_manual_order_cancellation(
        repo, account_id=ACCOUNT_ID, operator_id=OPERATOR_ID, order_ref=order_ref,
        cancel_request_id="9a6d5b4c-3e2f-4a5b-8c9d-0e1f2a3b4c5d", trade=website,
    )

    assert cancelled.cancellation.state == "SUCCEEDED"
    assert website.cancel_calls == ["repl-1"]
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "failed"
    [terminal] = _manual_endings(repo, order_ref)
    assert terminal["transition_kind"] == "MANUAL_ORDER_CANCELED"
    assert _owner_reads(repo).ending == "Cancelled."
    assert not repo.has_nonterminal_manual_order()
    assert _another_bots_entry(repo, _register_second_spy_lane(repo)[0]) == (True, None)


# ── A replacement record the Clerk cannot read is contained ───────────────────


async def test_an_unreadable_replacement_record_never_freezes_the_sweep(clocked_repo) -> None:  # noqa: F811
    """Alpaca reports ``replaced`` with no replacement id the Clerk can follow.

    Nothing raises: the sweep still completes, the leg stays honestly
    outstanding, and the account's verdict is not stale.
    """
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    replaced = website.orders[order_ref].model_copy(update={
        "status": "replaced", "replaced_by": "", "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = replaced

    await _frame(repo, replaced, event_type="replaced")
    result = await _reconciliation_pass(repo, website)

    assert result.verdict == "clean"
    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "in_progress"
    assert _replaced_transitions(repo, order_ref) == []
    assert _owner_reads(repo).replacement_note is None


async def test_a_working_manual_order_shows_no_replacement_note(clocked_repo) -> None:  # noqa: F811
    repo, _clock = clocked_repo
    website = _Website(repo=repo)
    await _buy_limit(repo, website)
    assert _owner_reads(repo).replacement_note is None

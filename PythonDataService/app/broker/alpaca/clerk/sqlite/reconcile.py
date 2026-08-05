"""Automatic reconciliation and UNKNOWN resolution over the SQLite spine
(#1378).

Two independent concerns, both driven by the same broker-facing pass:

- :func:`reconcile_uncertain_order` — resolve one order still sitting in
  ``unknown`` (R4) by asking the broker whether it landed, exactly the way
  :func:`~app.broker.alpaca.clerk.sqlite.enter.resolve_enter_submission`
  already does for a lost submit response. This module does not duplicate
  that resolution logic; it wraps it and records the *attempt* (durable
  ``reconciliations`` audit row) so an operator's "Reconcile now" and the
  automatic sweep are both auditable — an already-resolved order is a no-op,
  never a second write (#1378 acceptance: "creates no second intent").
- :func:`plan_account_reconciliation` — a pure decision function (no I/O)
  over one freshly-read broker snapshot: a foreign order (not ours by
  namespace) outranks a position drift, and a symbol with our own
  non-terminal in-flight order is never flagged as drift (a fill/ack still
  in transit is expected, not corruption). :func:`reconcile_account` is the
  I/O wrapper: read broker orders/positions, run the plan, raise the
  ``ACCOUNT_CLERK`` hold the plan calls for (idempotent — a persistent
  foreign order re-raises nothing after the first, same bounded-growth
  policy the pre-SQLite Alpaca clerk's ``reconcile.py`` used), and resolve
  every currently-uncertain order.

A ``position_drift`` verdict also raises a durable, ``ACCOUNT_CLERK``-scoped
R5 uncertainty (#1380) — the same idempotent, bounded-growth discipline
``_raise_account_hold`` already established for ``unexplained_order``: a
persistent drift re-raises nothing after the first. It blocks new exposure
account-wide (``blocks_new_exposure=True``) but explicitly allows reduction
— proven cancellation and closing exposure are never gated by it. Admission
enforcement itself (actually blocking a new ENTER) lives in
:func:`~app.broker.alpaca.clerk.sqlite.uncertainty.require_admission`,
called from :func:`~app.broker.alpaca.clerk.sqlite.enter.accept_enter`; this
module's job is only to raise the uncertainty, never to enforce it.

Deliberately deferred: the 6 named, backend-authored recovery actions (#1380
Part B) an operator would consult to resolve a raised uncertainty/hold —
this module raises the uncertainty and stops there, the same way
``_raise_account_hold`` already does for ``unexplained_order``. HTTP wiring
(an operator "Reconcile now" endpoint) is deferred alongside #1377's own
undelivered ENTER endpoint, per that slice's own scope note.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from app.broker.alpaca.clerk.exposure import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    signed_broker_position_quantity,
)
from app.broker.alpaca.clerk.sqlite.enter import resolve_enter_submission
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import (
    AccountHoldRaisedFacts,
    ReconciliationAttemptedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.order_identity import build_bot_order_namespace, order_ref_namespace_matches

logger = logging.getLogger(__name__)

UNEXPLAINED_ORDER_REASON_CODE = "UNEXPLAINED_ORDER"
POSITION_DRIFT_REASON_CODE = "POSITION_DRIFT"

Trigger = Literal["AUTOMATIC", "OPERATOR_RECONCILE_NOW"]
ReconciliationOutcome = Literal["STILL_UNKNOWN", "RESOLVED_SUCCESS", "RESOLVED_FAILURE"]
AccountVerdict = Literal["clean", "unexplained_order", "position_drift", "stale"]

__all__ = [
    "AccountReconciliationResult",
    "ReconcilePlan",
    "plan_account_reconciliation",
    "reconcile_account",
    "reconcile_uncertain_order",
]


@dataclass(frozen=True)
class ReconcilePlan:
    """What one account-level reconciliation pass decided (pure, no I/O)."""

    verdict: AccountVerdict
    foreign_orders: tuple[BrokerOrder, ...] = field(default_factory=tuple)
    drifted_symbols: tuple[str, ...] = field(default_factory=tuple)


def plan_account_reconciliation(
    *,
    namespaces: frozenset[str],
    broker_orders: list[BrokerOrder],
    broker_positions: list[BrokerPosition],
    attributed_positions: dict[str, float],
) -> ReconcilePlan:
    """Decide the account-level verdict from one freshly-read broker snapshot.

    Priority (highest first): ``unexplained_order`` (any order whose
    ``client_order_id`` does not parse into one of our own bot namespaces) ->
    ``position_drift`` (a symbol where the broker's signed position disagrees
    with our attributed sum by more than the tolerance, AND we have no
    non-terminal order of our own working that symbol) -> ``clean``.
    ``stale`` is decided by the caller (:func:`reconcile_account`) when the
    broker read itself fails, never by this function.
    """
    foreign = tuple(
        order for order in broker_orders if not order_ref_namespace_matches(order.client_order_id, namespaces)
    )
    if foreign:
        return ReconcilePlan(verdict="unexplained_order", foreign_orders=foreign)

    # broker_orders here is guaranteed all-ours: the `if foreign: return` above
    # already exited for any order outside our namespaces, so no explicit
    # ownership filter is needed on this pass over the same list.
    in_flight_symbols = {
        order.symbol.upper()
        for order in broker_orders
        if order.status.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
    }
    broker_by_symbol = {
        position.symbol.upper(): signed_broker_position_quantity(position) for position in broker_positions
    }
    symbols = set(broker_by_symbol) | set(attributed_positions)
    drifted = tuple(
        sorted(
            symbol
            for symbol in symbols
            if symbol not in in_flight_symbols
            and abs(broker_by_symbol.get(symbol, 0.0) - attributed_positions.get(symbol, 0.0))
            > POSITION_QTY_EPSILON
        )
    )
    if drifted:
        return ReconcilePlan(verdict="position_drift", drifted_symbols=drifted)
    return ReconcilePlan(verdict="clean")


async def reconcile_uncertain_order(
    repo: ClerkSqliteRepository, *, order_ref: str, trigger: Trigger, trade: BrokerTradePort
) -> ReconciliationOutcome:
    """Resolve one uncertain order and record the attempt (#1378).

    Already-resolved (terminal effect) is a true no-op: no broker call, no
    new transition — "Reconcile now" on an operation that already finished
    creates no second intent.

    Dispatches on the owning effect's ``kind`` rather than treating every
    uncertain order identically, because "resolved" means something
    different for each (#1379 correction — see
    ``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`` §6):

    - **EXIT** — delegates to
      :func:`~app.broker.alpaca.clerk.sqlite.exit.resolve_exit`, which
      already knows how to advance an EXIT's cancel/reduce/verify state
      machine from wherever it currently stands (possibly completing the
      whole operation in this pass). Outcome comes from the EXIT effect's
      *own* terminal state after that call. Checking
      ``order.broker_order_id is not None`` here — as the ENTER path below
      does — would be wrong for the reassigned entry order: that field was
      set by the *original* ENTER submission long before EXIT ever began,
      and proves nothing about whether EXIT's cancel has resolved. Before
      this dispatch existed, that exact conflation made the sweep silently
      report ``RESOLVED_SUCCESS`` for an entry stuck in cancel-uncertainty,
      with no broker call and no audit row at all.
    - **ENTER** (or any other kind) — delegates to
      :func:`~app.broker.alpaca.clerk.sqlite.enter.resolve_enter_submission`
      (never duplicated here) and derives the outcome from the before/after
      snapshot rather than threading a new return shape through that
      function — deliberately, to avoid widening an already-shipped
      function's return contract for one new caller. This does couple the
      derivation to ``resolve_enter_submission``'s current, exhaustively-
      enumerated set of outcomes (found-and-acked, confirmed-absent-past-
      grace, or unchanged); a future outcome shape it doesn't already know
      about would read here as ``STILL_UNKNOWN`` rather than fail loudly.
    """
    order_before = repo.order(order_ref)
    assert order_before is not None
    effect_before = repo.effect_operation(order_before.effect_operation_id)
    assert effect_before is not None

    if effect_before.state in ("succeeded", "failed"):
        return "RESOLVED_SUCCESS" if effect_before.state == "succeeded" else "RESOLVED_FAILURE"

    if effect_before.kind == "EXIT":
        await resolve_exit(repo, effect_operation_id=effect_before.effect_operation_id, trade=trade)
        effect_after = repo.effect_operation(effect_before.effect_operation_id)
        assert effect_after is not None
        if effect_after.state == "succeeded":
            outcome: ReconciliationOutcome = "RESOLVED_SUCCESS"
        elif effect_after.state == "failed":
            outcome = "RESOLVED_FAILURE"
        else:
            outcome = "STILL_UNKNOWN"
    else:
        if order_before.broker_order_id is not None:
            return "RESOLVED_SUCCESS"

        await resolve_enter_submission(repo, order_ref=order_ref, trade=trade)

        order_after = repo.order(order_ref)
        assert order_after is not None
        effect_after = repo.effect_operation(order_after.effect_operation_id)
        assert effect_after is not None

        if order_after.broker_order_id is not None:
            outcome = "RESOLVED_SUCCESS"
        elif effect_after.state == "failed":
            outcome = "RESOLVED_FAILURE"
        else:
            outcome = "STILL_UNKNOWN"

    facts = ReconciliationAttemptedFacts(
        trigger=trigger,
        outcome=outcome,
        why=f"reconciliation ({trigger}) resolved {order_ref!r} to {outcome}",
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect_after.strategy_instance_id,
            run_id=effect_after.run_id,
            command_id=effect_after.command_id,
            effect_operation_id=effect_after.effect_operation_id,
            order_ref=order_ref,
            transition_kind="RECONCILIATION_ATTEMPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=effect_after.state,
            clerk_observed_at_ms=repo.clock(),
            summary_code="RECONCILIATION_ATTEMPTED",
            facts_json=facts.to_facts_json(),
        )
    )
    return outcome


def _raise_account_hold(repo: ClerkSqliteRepository, *, foreign_orders: tuple[BrokerOrder, ...]) -> None:
    """Idempotent: a hold already ``ACTIVE`` for this reason code re-raises
    nothing (bounded growth — see the module docstring). The check and the
    append happen atomically under one continuous lock hold
    (:meth:`~.repository.ClerkSqliteRepository.raise_hold_if_none_active`)
    so two genuinely concurrent callers (the sweep and an operator's
    "Reconcile now" landing at the same instant) can never both observe "no
    active hold" and both append one."""

    def build_transition() -> TransitionInput:
        facts = AccountHoldRaisedFacts(
            reason_code=UNEXPLAINED_ORDER_REASON_CODE,
            evidence_refs=[order.order_id for order in foreign_orders],
        )
        return TransitionInput(
            transition_kind="ACCOUNT_HOLD_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ACCOUNT_HOLD_RAISED",
            facts_json=facts.to_facts_json(),
        )

    repo.raise_hold_if_none_active(
        scope="ACCOUNT_CLERK",
        reason_code=UNEXPLAINED_ORDER_REASON_CODE,
        build_transition=build_transition,
    )


def _raise_position_drift_uncertainty(
    repo: ClerkSqliteRepository, *, drifted_symbols: tuple[str, ...]
) -> None:
    """#1380: a position-drift verdict raises a durable, ``ACCOUNT_CLERK``-
    scoped R5 uncertainty — idempotent via
    :func:`~.uncertainty.raise_uncertainty`'s own atomic check-then-raise, so
    a persistent drift re-raises nothing after the first (bounded growth,
    same policy as :func:`_raise_account_hold`). Blocks new exposure
    account-wide but explicitly allows reduction: an operator investigating
    drift must still be able to close existing positions."""
    symbols = ", ".join(drifted_symbols)
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=POSITION_DRIFT_REASON_CODE,
        headline="Account position doesn't match broker records",
        explanation=(
            f"The broker's reported position for {symbols} differs from what the Clerk "
            "has recorded, outside the accepted tolerance."
        ),
        operator_impact=(
            "New positions are paused account-wide until this is resolved. Existing "
            "positions can still be reduced or closed."
        ),
        next_step="Reconcile now, then review the drifted symbols before resuming.",
        evidence_refs=drifted_symbols,
        blocks_new_exposure=True,
        allows_reduction=True,
    )


@dataclass(frozen=True)
class AccountReconciliationResult:
    verdict: AccountVerdict
    #: Orders that reached a terminal outcome (``RESOLVED_SUCCESS`` or
    #: ``RESOLVED_FAILURE``) this pass — excludes an order that stayed
    #: ``STILL_UNKNOWN`` (still within its R4 grace window, or a lookup
    #: ``BrokerError``) and one skipped this pass for live claim contention.
    resolved_count: int = 0
    foreign_order_count: int = 0
    drifted_symbols: tuple[str, ...] = field(default_factory=tuple)


async def reconcile_account(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    trigger: Trigger = "AUTOMATIC",
) -> AccountReconciliationResult:
    """One reconciliation pass for this account: read broker truth, raise a
    hold for any foreign order, and resolve every order still ``unknown``.

    A broker read failure is a **truthful stale verdict** (#1378 acceptance):
    no hold is raised, no order is resolved, nothing is fabricated as clean
    or terminal — the pass simply reports it does not have fresh evidence
    this time and returns.
    """
    try:
        broker_orders, broker_positions = await asyncio.gather(
            read.list_orders(status="all", limit=500), read.list_positions()
        )
    except BrokerError:
        logger.warning(
            "alpaca sqlite reconciliation sweep could not read the broker; reporting stale",
            extra={"action": "reconcile_account_stale", "account_id": repo.account_id},
        )
        return AccountReconciliationResult(verdict="stale")

    namespaces = frozenset(
        build_bot_order_namespace(instance["strategy_instance_id"]) for instance in repo.strategy_instances()
    )
    plan = plan_account_reconciliation(
        namespaces=namespaces,
        broker_orders=broker_orders,
        broker_positions=broker_positions,
        attributed_positions=repo.attributed_positions_by_symbol(),
    )
    if plan.verdict == "unexplained_order":
        _raise_account_hold(repo, foreign_orders=plan.foreign_orders)
    elif plan.verdict == "position_drift":
        _raise_position_drift_uncertainty(repo, drifted_symbols=plan.drifted_symbols)

    resolved_count = 0
    for order_row in repo.uncertain_orders():
        try:
            outcome = await reconcile_uncertain_order(
                repo, order_ref=order_row.order_ref, trigger=trigger, trade=trade
            )
        except OperationClaimError:
            # A live claim held by a concurrent submit_enter/resolve for this
            # *specific* order is transient contention, not a pass-wide
            # failure — one stuck order must not block the rest of this
            # pass's otherwise-unrelated, perfectly resolvable orders. It
            # simply waits for the next pass (or "Reconcile now" on it
            # directly), same as an order this pass never got to.
            logger.info(
                "alpaca sqlite reconciliation: order claimed by a live concurrent owner, "
                "deferring to a later pass",
                extra={
                    "action": "reconcile_order_claim_contended",
                    "account_id": repo.account_id,
                    "order_ref": order_row.order_ref,
                },
            )
            continue
        if outcome != "STILL_UNKNOWN":
            resolved_count += 1

    return AccountReconciliationResult(
        verdict=plan.verdict,
        resolved_count=resolved_count,
        foreign_order_count=len(plan.foreign_orders),
        drifted_symbols=plan.drifted_symbols,
    )


type Sleep = Callable[[float], Awaitable[None]]

_DEFAULT_INTERVAL_S = 15.0


class ReconciliationSweep:
    """Background loop that runs one :func:`reconcile_account` pass per
    interval for one account's repository — the "bounded cadence" half of
    #1378's acceptance criteria. Mirrors the pre-SQLite Alpaca clerk's
    ``sweep.ReconciliationSweep`` shape: an injectable :type:`Sleep` and an
    optional ``max_passes`` budget keep tests timer-free and deterministic.

    Not wired into any FastAPI lifespan in this slice — like #1377's ENTER,
    this is the domain-level primitive; lifecycle wiring is deferred until an
    HTTP surface actually needs it.
    """

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        interval_s: float = _DEFAULT_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
        max_passes: int | None = None,
    ) -> None:
        self._repo = repo
        self._read = read
        self._trade = trade
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_passes = max_passes
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="alpaca-sqlite-reconciliation-sweep")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run(self) -> None:
        passes = 0
        while True:
            await self._run_one_pass()
            passes += 1
            if self._max_passes is not None and passes >= self._max_passes:
                return
            await self._sleep(self._interval_s)

    async def _run_one_pass(self) -> None:
        try:
            await reconcile_account(self._repo, read=self._read, trade=self._trade, trigger="AUTOMATIC")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "alpaca sqlite reconciliation sweep pass errored; will retry next interval",
                extra={"action": "reconcile_sweep_error", "account_id": self._repo.account_id},
                exc_info=True,
            )

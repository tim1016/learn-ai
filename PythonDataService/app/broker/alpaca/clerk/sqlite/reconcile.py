"""Ordered account reconciliation and effect-operation recovery."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal
from weakref import WeakKeyDictionary

from app.broker.alpaca.clerk.recovery_reduction import (
    UNPRICEABLE_RECOVERY,
    RecoveryPricing,
)
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_watchdog import (
    BrokerSymbolReader,
    BrokerSymbolView,
    redrive_or_escalate_stale_exits,
)
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import (
    ReconciliationAttemptedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import (
    position_quantity_is_nonzero,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    resolve_manual_order_cancellation,
)
from app.broker.alpaca.clerk.sqlite.models import (
    EffectOperationResource,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.off_loop import to_thread
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_order_evidence,
    resolve_order_submission,
    trade_port_folds_simulated_evidence,
)
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    signed_broker_position_quantity,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteError,
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.run_liveness import retire_runs_whose_runner_is_gone
from app.broker.alpaca.clerk.sqlite.stopped_run_entries import (
    cancel_entries_of_inactive_runs,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    AdmissionBlockedError,
    raise_account_hold,
    raise_uncertainty,
    resolve_account_hold,
    resolve_exit_not_flat_uncertainty,
    resolve_exit_stuck_uncertainty,
    resolve_incomplete_reconciliation_uncertainty,
    resolve_reconciliation_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    PositionDriftCause,
    PositionDriftObservation,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    order_ref_namespace_matches,
)

logger = logging.getLogger(__name__)

MAX_OPEN_ORDER_SNAPSHOT = 500

_RECONCILIATION_LOCKS: WeakKeyDictionary[ClerkSqliteRepository, asyncio.Lock] = WeakKeyDictionary()
_DIRECT_RECONCILIATION_INTAKES: WeakKeyDictionary[ClerkSqliteRepository, ReentrantAsyncLock] = (
    WeakKeyDictionary()
)
_RECONCILIATION_LOCKS_GUARD = threading.Lock()

Trigger = Literal["AUTOMATIC", "OPERATOR_RECONCILE_NOW"]
ReconciliationOutcome = Literal["STILL_UNKNOWN", "RESOLVED_SUCCESS", "RESOLVED_FAILURE"]
AccountVerdict = Literal["clean", "unexplained_order", "position_drift", "stale"]


@dataclass(frozen=True)
class ReconcilePlan:
    verdict: AccountVerdict
    foreign_orders: tuple[BrokerOrder, ...] = field(default_factory=tuple)
    drifted_symbols: tuple[str, ...] = field(default_factory=tuple)
    indeterminate_symbols: tuple[str, ...] = field(default_factory=tuple)


class ReconciliationInvariantError(ClerkSqliteError):
    """Required local custody state was absent during fail-closed recovery."""


class ReconciliationLockOrderError(ClerkSqliteError):
    """Reconciliation was entered while owning intake, which would invert locks."""


def _invariant_failure_outcome(effect_kind: str) -> ReconciliationOutcome:
    """Classify malformed nonterminal effects without pretending they resolved."""
    if effect_kind == "EXIT":
        return "RESOLVED_FAILURE"
    if effect_kind in {"ENTER", "MANUAL_ORDER", "CANCEL"}:
        return "STILL_UNKNOWN"
    raise ReconciliationInvariantError(
        f"effect kind {effect_kind!r} has no invariant-failure recovery policy"
    )


async def _under_intake[LocalResult](
    intake: ReentrantAsyncLock,
    operation: Callable[..., LocalResult],
    *args: object,
    **kwargs: object,
) -> LocalResult:
    """Run one bounded repository fold off the loop, in the shared intake domain."""
    return await intake.off_loop(operation, *args, **kwargs)


def _broker_quantity_by_symbol(broker_positions: list[BrokerPosition]) -> dict[str, float]:
    """The broker's signed position per upper-cased symbol."""
    broker_by_symbol: dict[str, float] = {}
    for position in broker_positions:
        symbol = position.symbol.upper()
        broker_by_symbol[symbol] = broker_by_symbol.get(symbol, 0.0) + signed_broker_position_quantity(
            position
        )
    return broker_by_symbol


def _attributed_quantity_by_symbol(attributed_positions: dict[str, float]) -> dict[str, float]:
    """The Clerk's account-wide attributed position per upper-cased symbol."""
    attributed_by_symbol: dict[str, float] = {}
    for symbol, quantity in attributed_positions.items():
        normalized = symbol.upper()
        attributed_by_symbol[normalized] = attributed_by_symbol.get(normalized, 0.0) + quantity
    return attributed_by_symbol


def _broker_symbol_reader(
    repo: ClerkSqliteRepository,
    *,
    broker_orders: list[BrokerOrder],
    broker_positions: list[BrokerPosition],
) -> BrokerSymbolReader:
    """Read one symbol of this pass's broker snapshot against current attribution.

    The stuck-EXIT watchdog's view of broker truth (#2343). A symbol agrees
    only when the broker's signed position equals the account-wide
    attribution *and* no order for it is working in the snapshot: a working
    order can fill under the re-drive and carry the account past zero, so it
    is never proof, whether it is foreign, manual, or another EXIT of ours.
    Attribution is read at the moment of the call, so a fill folded since the
    snapshot that the snapshot did not see reads as disagreement and the
    watchdog defers rather than trusting either side.
    """
    broker_by_symbol = _broker_quantity_by_symbol(broker_positions)
    in_flight = _in_flight_symbols(broker_orders)

    def read(symbol: str) -> BrokerSymbolView:
        normalized = symbol.upper()
        broker_qty = broker_by_symbol.get(normalized, 0.0)
        attributed_qty = _attributed_quantity_by_symbol(
            repo.attributed_positions_by_symbol()
        ).get(normalized, 0.0)
        return BrokerSymbolView(
            broker_qty=broker_qty,
            attributed_qty=attributed_qty,
            agrees=(
                not position_quantity_is_nonzero(broker_qty - attributed_qty)
                and normalized not in in_flight
            ),
        )

    return read


def _in_flight_symbols(broker_orders: list[BrokerOrder]) -> frozenset[str]:
    """Upper-cased symbols with an order the broker may still act on."""
    return frozenset(
        order.symbol.upper()
        for order in broker_orders
        if order.status.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
    )


def plan_account_reconciliation(
    *,
    namespaces: frozenset[str],
    broker_orders: list[BrokerOrder],
    broker_positions: list[BrokerPosition],
    attributed_positions: dict[str, float],
    known_order_refs: frozenset[str] | None = None,
) -> ReconcilePlan:
    """Derive residual account safety only after local evidence was folded."""
    # Once the active SQLite authority supplies its full captured order-ref
    # set, that durable identity is stronger than the historical bot-only
    # namespace heuristic and correctly recognizes manual custody too.
    foreign = tuple(
        order
        for order in broker_orders
        if (
            order.client_order_id not in known_order_refs
            if known_order_refs is not None
            else not order_ref_namespace_matches(order.client_order_id, namespaces)
        )
    )
    in_flight_symbols = _in_flight_symbols(broker_orders)
    broker_by_symbol = _broker_quantity_by_symbol(broker_positions)
    attributed_by_symbol = _attributed_quantity_by_symbol(attributed_positions)
    symbols = set(broker_by_symbol) | set(attributed_by_symbol)
    mismatched_symbols = {
        symbol
        for symbol in symbols
        if position_quantity_is_nonzero(
            broker_by_symbol.get(symbol, 0.0)
            - attributed_by_symbol.get(symbol, 0.0)
        )
    }
    drifted = tuple(
        sorted(symbol for symbol in mismatched_symbols if symbol not in in_flight_symbols)
    )
    indeterminate = tuple(
        sorted(symbol for symbol in mismatched_symbols if symbol in in_flight_symbols)
    )
    verdict: AccountVerdict
    if foreign:
        verdict = "unexplained_order"
    elif drifted or indeterminate:
        # #1655: a symbol whose broker/attributed mismatch is only explained
        # by a still-working captured order is not proven equal — it is
        # unproven, not clean. Treat it the same as a confirmed drift for
        # admission purposes until a later pass proves exact equality.
        verdict = "position_drift"
    else:
        verdict = "clean"
    return ReconcilePlan(
        verdict=verdict,
        foreign_orders=foreign,
        drifted_symbols=drifted,
        indeterminate_symbols=indeterminate,
    )


async def _reconcile_effect(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    trigger: Trigger,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
) -> ReconciliationOutcome:
    if effect.state in ("succeeded", "failed", "rejected"):
        return "RESOLVED_SUCCESS" if effect.state == "succeeded" else "RESOLVED_FAILURE"

    if effect.kind == "EXIT":
        entry_orders = [
            item
            for item in await to_thread(
                lambda: repo.orders_for_effect_operation(effect.effect_operation_id)
            )
            if item.role == "ENTRY"
        ]
        if not entry_orders:
            raise ReconciliationInvariantError(
                f"EXIT effect {effect.effect_operation_id!r} has no captured ENTRY order"
            )
        order = entry_orders[0]
        await resolve_exit(
            repo,
            effect_operation_id=effect.effect_operation_id,
            trade=trade,
            off_loop=to_thread,
        )
    elif effect.kind == "CANCEL":

        def _cancel_branch_target() -> OrderResource:
            cancellation = repo.manual_order_cancellation_for_effect(
                effect_operation_id=effect.effect_operation_id
            )
            if cancellation is None:
                raise ReconciliationInvariantError(
                    f"CANCEL effect {effect.effect_operation_id!r} has no manual cancellation target"
                )
            target = repo.order(cancellation.order_ref)
            if target is None:
                raise ReconciliationInvariantError(
                    f"CANCEL effect {effect.effect_operation_id!r} has no captured target order"
                )
            return target

        order = await to_thread(_cancel_branch_target)
        await resolve_manual_order_cancellation(
            repo,
            effect_operation_id=effect.effect_operation_id,
            trade=trade,
            off_loop=to_thread,
        )
    else:
        order = await to_thread(lambda: repo.order_for_effect_operation(effect.effect_operation_id))
        if order is None:
            raise ReconciliationInvariantError(
                f"ENTER effect {effect.effect_operation_id!r} has no captured order"
            )
        await resolve_order_submission(
            repo,
            order_ref=order.order_ref,
            trade=trade,
            off_loop=to_thread,
        )

    def _verify_after_resolution() -> tuple[EffectOperationResource, OrderResource]:
        effect_after = repo.effect_operation(effect.effect_operation_id)
        if effect_after is None:
            raise ReconciliationInvariantError(
                f"effect {effect.effect_operation_id!r} disappeared during reconciliation"
            )
        order_after = repo.order(order.order_ref)
        if order_after is None:
            raise ReconciliationInvariantError(
                f"order {order.order_ref!r} disappeared during reconciliation"
            )
        return effect_after, order_after

    effect_after, order_after = await to_thread(_verify_after_resolution)
    if effect_after.state == "succeeded":
        outcome: ReconciliationOutcome = "RESOLVED_SUCCESS"
    elif effect_after.state in ("failed", "rejected"):
        # Ahead of the reached-the-broker heuristic below, not after it: since
        # #2006 an ENTER can be terminally ``failed`` *and* carry a
        # ``broker_order_id`` (the broker acknowledged it, then ended it
        # unfilled). Ordered the other way, that effect records
        # ``RESOLVED_SUCCESS`` for a failure — an audit row contradicting the
        # receipt beside it. Before #2006 a ``failed`` ENTER only came from the
        # submit-absence void, which has no broker id, so the old order was
        # correct then and is not now.
        outcome = "RESOLVED_FAILURE"
    elif (
        effect_after.kind in {"ENTER", "MANUAL_ORDER"}
        and order_after.broker_order_id is not None
    ):
        outcome = "RESOLVED_SUCCESS"
    else:
        outcome = "STILL_UNKNOWN"
    await _under_intake(
        intake,
        _record_reconciliation_attempt,
        repo,
        effect=effect_after,
        order_ref=order.order_ref,
        trigger=trigger,
        outcome=outcome,
    )
    return outcome


def _record_reconciliation_attempt(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    order_ref: str | None,
    trigger: Trigger,
    outcome: ReconciliationOutcome,
) -> None:
    facts = ReconciliationAttemptedFacts(
        trigger=trigger,
        outcome=outcome,
        why=f"reconciliation ({trigger}) resolved {effect.effect_operation_id!r} to {outcome}",
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            order_ref=order_ref,
            transition_kind="RECONCILIATION_ATTEMPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=effect.state,
            clerk_observed_at_ms=repo.clock(),
            summary_code="RECONCILIATION_ATTEMPTED",
            facts_json=facts.to_facts_json(),
        )
    )


def _sync_unexplained_order_hold(repo: ClerkSqliteRepository, foreign_orders: tuple[BrokerOrder, ...]) -> None:
    """Reconcile the active hold to this complete broker snapshot only.

    A reviewed external row remains durable audit evidence, but no longer
    contributes this particular unexplained-order hold.  Other currently
    unreviewed broker orders remain as independent evidence refs, so an
    acknowledgement cannot release their account-wide safety fence.
    """
    evidence_refs = sorted(
        order.order_id
        for order in foreign_orders
        if (
            observation := repo.external_order_by_broker_order_id(order.order_id)
        ) is None
        or observation.acknowledged_at_ms is None
    )
    if not evidence_refs:
        resolve_account_hold(
            repo,
            reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE,
            summary_code="ACCOUNT_HOLD_RESOLVED_BY_RECONCILIATION",
        )
        return

    raise_account_hold(
        repo,
        reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE,
        evidence_refs=evidence_refs,
    )


def _sync_position_drift(
    repo: ClerkSqliteRepository,
    *,
    drifted_symbols: tuple[str, ...],
    indeterminate_symbols: tuple[str, ...],
    broker_positions: list[BrokerPosition],
    attributed_positions: dict[str, float],
) -> None:
    """Fence account-wide new exposure on any unproven broker/attributed mismatch.

    #1655: a symbol whose mismatch is only explained by a still-working
    captured order (``indeterminate_symbols``) is not proof the position
    truly matches — the order and position reads can land on different
    points of the same fill's propagation. Such a symbol is folded into the
    same durable episode as a confirmed drift so a *first* indeterminate
    observation still authors a blocker instead of silently passing. The
    episode is resolved only when a later pass proves both sets empty —
    exact equality with no in-flight indeterminacy — never merely because a
    working order now "explains" a symbol that was drifted before.
    """
    mismatched_symbols = tuple(sorted({*drifted_symbols, *indeterminate_symbols}))
    if not mismatched_symbols:
        resolve_reconciliation_uncertainty(
            repo,
            reason_code=POSITION_DRIFT_REASON_CODE,
            evidence_refs=("fresh_position_snapshot",),
        )
        return
    symbols = ", ".join(mismatched_symbols)
    broker_by_symbol = _broker_quantity_by_symbol(broker_positions)
    cause = PositionDriftCause(
        positions=tuple(
            PositionDriftObservation(
                symbol=symbol,
                broker_qty=broker_by_symbol.get(symbol, 0.0),
                attributed_qty=attributed_positions.get(symbol, 0.0),
            )
            for symbol in mismatched_symbols
        )
    )
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=POSITION_DRIFT_REASON_CODE,
        headline="Account position doesn't match broker records",
        explanation=(
            f"The broker's reported position for {symbols} differs from the Clerk's "
            "attributed exposure outside the accepted tolerance, or remains unproven "
            "while a captured order for that symbol is still working at the broker."
        ),
        operator_impact=("New positions are paused account-wide. Recognized risk reduction remains available."),
        next_step="Reconcile now, then review the affected symbols before resuming.",
        evidence_refs=("fresh_position_snapshot",),
        cause_facts=cause.to_mapping(),
        refresh_unchanged=True,
    )


def _raise_stale_snapshot_uncertainty(repo: ClerkSqliteRepository, why: str) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        headline="Broker account truth is unavailable",
        explanation=why,
        operator_impact=(
            "New exposure and unproven reduction are paused account-wide until a fresh "
            "snapshot succeeds. Cancellation and reconciliation remain available."
        ),
        next_step="Reconcile now after broker connectivity is restored.",
        evidence_refs=(),
        cause_facts={"snapshot": "open_orders_and_positions"},
        severity="error",
    )


def _raise_incomplete_reconciliation_uncertainty(repo: ClerkSqliteRepository) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=RECONCILIATION_INCOMPLETE_REASON_CODE,
        headline="Account reconciliation did not complete",
        explanation=(
            "The latest reconciliation pass ended before final broker truth and local custody "
            "could be proven consistent."
        ),
        operator_impact=(
            "New exposure and unproven reduction are paused account-wide until a complete "
            "reconciliation succeeds. Cancellation and reconciliation remain available."
        ),
        next_step="Inspect the reconciliation failure, correct it, and reconcile again.",
        evidence_refs=(),
        cause_facts={"pass": "account_reconciliation"},
        severity="error",
        refresh_unchanged=True,
    )


async def _commit_incomplete_reconciliation_and_release(
    repo: ClerkSqliteRepository,
    *,
    intake: ReentrantAsyncLock,
) -> None:
    """Release the process fence only after its durable replacement exists."""
    await _under_intake(intake, _raise_incomplete_reconciliation_uncertainty, repo)
    await _under_intake(intake, repo.end_reconciliation)


def _resolve_flat_exit_fences(
    repo: ClerkSqliteRepository, instances: list[dict]
) -> None:
    for instance in instances:
        strategy_instance_id = instance["strategy_instance_id"]
        attributed = repo.attributed_positions_for_strategy(strategy_instance_id)
        if any(position_quantity_is_nonzero(quantity) for quantity in attributed.values()):
            continue
        resolve_exit_not_flat_uncertainty(
            repo,
            strategy_instance_id=strategy_instance_id,
            evidence_refs=("fresh_account_snapshot", "attributed_flat"),
        )
        # A stuck-EXIT escalation outlives its EXIT_NOT_FLAT origin; the same
        # attributed-flat proof must clear it, or the now-flat strategy stays
        # permanently barred from new exposure.
        resolve_exit_stuck_uncertainty(
            repo,
            strategy_instance_id=strategy_instance_id,
            evidence_refs=("fresh_account_snapshot", "attributed_flat"),
        )


@dataclass(frozen=True)
class AccountReconciliationResult:
    verdict: AccountVerdict
    resolved_count: int = 0
    foreign_order_count: int = 0
    drifted_symbols: tuple[str, ...] = field(default_factory=tuple)
    indeterminate_symbols: tuple[str, ...] = field(default_factory=tuple)
    receipt_id: str | None = None
    recorded_at_ms: int | None = None


async def read_account_open_work(
    read: BrokerReadPort,
) -> tuple[list[BrokerOrder], list[BrokerPosition]]:
    """The account's open orders and positions, as the broker reports them.

    The one read of whole-account broker truth: reconciliation folds it into
    custody and the lane-quiet observation (#2154) asks only whether it is
    empty. A ``BrokerError`` propagates, because what an unreadable broker
    means is the caller's to decide. The two lists are gathered concurrently
    with no consistency fence between them, so a caller that needs them to
    describe one instant owes its own re-read rule.
    """
    broker_orders, broker_positions = await asyncio.gather(
        read.list_orders(status="open", limit=MAX_OPEN_ORDER_SNAPSHOT),
        read.list_positions(),
    )
    return broker_orders, broker_positions


async def _read_account_snapshot(
    repo: ClerkSqliteRepository,
    read: BrokerReadPort,
    *,
    intake: ReentrantAsyncLock,
) -> tuple[list[BrokerOrder], list[BrokerPosition]] | None:
    try:
        broker_orders, broker_positions = await read_account_open_work(read)
    except BrokerError as exc:
        await _under_intake(intake, _raise_stale_snapshot_uncertainty, repo, str(exc))
        logger.warning(
            "alpaca sqlite reconciliation could not read fresh broker truth",
            extra={"action": "reconcile_account_stale", "account_id": repo.account_id},
        )
        return None
    if len(broker_orders) >= MAX_OPEN_ORDER_SNAPSHOT:
        await _under_intake(
            intake,
            _raise_stale_snapshot_uncertainty,
            repo,
            "The open-order snapshot reached the 500-row boundary; completeness cannot be proven.",
        )
        return None
    return broker_orders, broker_positions


async def _recover_operations(
    repo: ClerkSqliteRepository,
    *,
    trigger: Trigger,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
) -> int:
    resolved_count = 0
    effects = await to_thread(repo.reconcilable_effect_operations)
    for effect in effects:
        try:
            outcome = await _reconcile_effect(
                repo,
                effect=effect,
                trigger=trigger,
                trade=trade,
                intake=intake,
            )
        except ReconciliationInvariantError as exc:
            invariant_effect_id = effect.effect_operation_id
            linked_orders = await to_thread(
                lambda effect_id=invariant_effect_id: repo.orders_for_effect_operation(effect_id)
            )
            outcome = _invariant_failure_outcome(effect.kind)
            await _under_intake(
                intake,
                _record_reconciliation_attempt,
                repo,
                effect=effect,
                order_ref=linked_orders[0].order_ref if linked_orders else None,
                trigger=trigger,
                outcome=outcome,
            )
            logger.error(
                "alpaca sqlite reconciliation found incomplete local custody state",
                extra={
                    "action": "reconcile_effect_invariant_failed",
                    "account_id": repo.account_id,
                    "effect_operation_id": effect.effect_operation_id,
                    "reason": str(exc),
                },
            )
            if outcome == "STILL_UNKNOWN":
                raise
            resolved_count += 1
            continue
        except (OperationClaimError, AdmissionBlockedError):
            logger.info(
                "alpaca sqlite reconciliation deferred a contended or policy-blocked effect",
                extra={
                    "action": "reconcile_effect_deferred",
                    "account_id": repo.account_id,
                    "effect_operation_id": effect.effect_operation_id,
                },
            )
            continue
        if outcome != "STILL_UNKNOWN":
            resolved_count += 1
    return resolved_count


def _reconciliation_lock(repo: ClerkSqliteRepository) -> asyncio.Lock:
    """One account pass at a time, shared by automatic and operator callers."""
    with _RECONCILIATION_LOCKS_GUARD:
        lock = _RECONCILIATION_LOCKS.get(repo)
        if lock is None:
            lock = asyncio.Lock()
            _RECONCILIATION_LOCKS[repo] = lock
        return lock


def _direct_reconciliation_intake(
    repo: ClerkSqliteRepository,
    provided: ReentrantAsyncLock | None,
) -> ReentrantAsyncLock:
    """Return the authority fence, or a stable direct-call fence for tooling/tests."""
    if provided is not None:
        return provided
    with _RECONCILIATION_LOCKS_GUARD:
        intake = _DIRECT_RECONCILIATION_INTAKES.get(repo)
        if intake is None:
            intake = ReentrantAsyncLock()
            _DIRECT_RECONCILIATION_INTAKES[repo] = intake
        return intake


async def reconcile_account(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    trigger: Trigger = "AUTOMATIC",
    intake: ReentrantAsyncLock | None = None,
    pricing: RecoveryPricing = UNPRICEABLE_RECOVERY,
) -> AccountReconciliationResult:
    """Serialize snapshot-to-verdict passes for one live account authority.

    ``pricing`` is what the stuck-EXIT watchdog prices its extended-hours
    re-drive limits from (#2229); the degraded default defers rather than
    guessing a price.
    """
    intake = _direct_reconciliation_intake(repo, intake)
    if intake.held_by_current_task():
        raise ReconciliationLockOrderError(
            "SQLite reconciliation must acquire the reconciliation lock before intake"
        )
    async with _reconciliation_lock(repo):
        # ``began`` is set inside the worker, not after the await: cancellation
        # arriving mid-begin still drains the hop (off_loop waits the worker
        # out), so by the time CancelledError propagates the flag already says
        # whether the account gate was installed — and without the cleanup
        # below, a cancelled begin would leave every later pass failing
        # "reconciliation is already in progress" (#1993 review).
        began = False

        def _begin() -> None:
            nonlocal began
            repo.begin_reconciliation()
            began = True

        try:
            await _under_intake(intake, _begin)
            result = await _reconcile_account_serialized(
                repo,
                read=read,
                trade=trade,
                trigger=trigger,
                intake=intake,
                pricing=pricing,
            )
        except asyncio.CancelledError:
            if began:
                await asyncio.shield(
                    _commit_incomplete_reconciliation_and_release(repo, intake=intake)
                )
            raise
        except Exception:
            if began:
                await _commit_incomplete_reconciliation_and_release(repo, intake=intake)
            raise
        await _under_intake(intake, repo.end_reconciliation)
        return result


def _record_operator_reconciliation_receipt(
    repo: ClerkSqliteRepository,
    result: AccountReconciliationResult,
) -> tuple[str, int]:
    recorded_at_ms = repo.clock()
    facts = ReconciliationAttemptedFacts(
        trigger="OPERATOR_RECONCILE_NOW",
        outcome="RESOLVED_SUCCESS" if result.verdict == "clean" else "STILL_UNKNOWN",
        why=f"operator account reconciliation completed with verdict {result.verdict}",
    )
    committed = repo.append_transition(
        TransitionInput(
            transition_kind="RECONCILIATION_ATTEMPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=result.verdict,
            clerk_observed_at_ms=recorded_at_ms,
            summary_code="OPERATOR_RECONCILIATION_COMPLETED",
            facts_json=facts.to_facts_json(),
        )
    )
    return f"reconciliation:{committed.sequence}", recorded_at_ms


def _fold_snapshot_evidence(
    repo: ClerkSqliteRepository,
    broker_orders: list[BrokerOrder],
    *,
    simulated_authority: bool = False,
) -> None:
    for broker_order in broker_orders:
        if broker_order.client_order_id is None:
            continue
        local_order = repo.order(broker_order.client_order_id)
        if local_order is None:
            continue
        owner = repo.active_exit_for_order(local_order.order_ref) or repo.effect_operation(
            local_order.effect_operation_id
        )
        if owner is None:
            raise ReconciliationInvariantError(
                f"captured order {local_order.order_ref!r} has no owning effect"
            )
        fold_order_evidence(
            repo,
            effect_operation_id=owner.effect_operation_id,
            order=broker_order,
            simulated_authority=simulated_authority,
        )


async def _fold_snapshot_evidence_under_intake(
    repo: ClerkSqliteRepository,
    *,
    broker_orders: list[BrokerOrder],
    intake: ReentrantAsyncLock,
    simulated_authority: bool = False,
) -> None:
    """Fold each snapshot unit separately so websocket evidence can interleave."""
    for broker_order in broker_orders:
        await _under_intake(
            intake, _fold_one_snapshot_order, repo, broker_order,
            simulated_authority=simulated_authority,
        )
        await asyncio.sleep(0)


def _fold_one_snapshot_order(
    repo: ClerkSqliteRepository,
    broker_order: BrokerOrder,
    *,
    simulated_authority: bool = False,
) -> None:
    _fold_snapshot_evidence(repo, [broker_order], simulated_authority=simulated_authority)


async def _reconcile_account_serialized(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    trigger: Trigger,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing = UNPRICEABLE_RECOVERY,
) -> AccountReconciliationResult:
    """Fold fresh order truth, recover operations, then derive residual safety."""
    snapshot = await _read_account_snapshot(repo, read, intake=intake)
    if snapshot is None:
        return AccountReconciliationResult(verdict="stale")
    broker_orders, broker_positions = snapshot

    await _under_intake(
        intake,
        resolve_reconciliation_uncertainty,
        repo,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        evidence_refs=("fresh_open_orders", "fresh_positions"),
    )

    # Capture every local identity before recovery can terminalize an effect.
    # The trade port's capability — not the snapshot's spelling — decides
    # whether these aggregates are a no-submit adapter's own observations.
    simulated_authority = trade_port_folds_simulated_evidence(trade)
    await _fold_snapshot_evidence_under_intake(
        repo,
        broker_orders=broker_orders,
        intake=intake,
        simulated_authority=simulated_authority,
    )

    resolved_count = await _recover_operations(
        repo,
        trigger=trigger,
        trade=trade,
        intake=intake,
    )

    # The re-drive is sized from attribution, so it may only send what this
    # pass's broker snapshot agrees with, with nothing working on the symbol (#2343).
    await redrive_or_escalate_stale_exits(
        repo,
        trade=trade,
        intake=intake,
        broker_symbol=_broker_symbol_reader(
            repo, broker_orders=broker_orders, broker_positions=broker_positions
        ),
        pricing=pricing,
        off_loop=to_thread,
    )

    # A run whose runner stopped renewing its liveness lease is retired first
    # (#2369), so the step below also cancels the ENTERs of a runner that died
    # without committing RUN_STOPPED.
    await _under_intake(intake, retire_runs_whose_runner_is_gone, repo)

    # No ENTER may stay working once its run is no longer ACTIVE (#2362):
    # re-driven every pass, so a crash, a Stop that lost a claim race, or a
    # POST that landed after Stop is cancelled within one sweep.
    await cancel_entries_of_inactive_runs(repo, trade=trade, off_loop=to_thread)

    # Recovery can poll fills, cancel entries, or submit a reducing order.
    # Re-read broker truth and fold the newest open-order evidence before the
    # final broker-vs-attributed comparison.
    verdict_base_revision = await _under_intake(
        intake,
        _control_revision,
        repo,
    )
    for _attempt in range(2):
        final_snapshot = await _read_account_snapshot(repo, read, intake=intake)
        if final_snapshot is None:
            return AccountReconciliationResult(verdict="stale", resolved_count=resolved_count)
        broker_orders, broker_positions = final_snapshot
        finalized = await _under_intake(
            intake,
            _finalize_reconciliation_verdict,
            repo,
            broker_orders=broker_orders,
            broker_positions=broker_positions,
            resolved_count=resolved_count,
            trigger=trigger,
            expected_control_revision=verdict_base_revision,
            simulated_authority=simulated_authority,
        )
        if finalized is not None:
            return finalized
        verdict_base_revision = await _under_intake(intake, _control_revision, repo)

    await _under_intake(
        intake,
        _raise_stale_snapshot_uncertainty,
        repo,
        "The Clerk changed while final broker truth was observed.",
    )
    return AccountReconciliationResult(verdict="stale", resolved_count=resolved_count)


def _control_revision(repo: ClerkSqliteRepository) -> int:
    return repo.control_meta_snapshot().control_revision


def _finalize_reconciliation_verdict(
    repo: ClerkSqliteRepository,
    *,
    broker_orders: list[BrokerOrder],
    broker_positions: list[BrokerPosition],
    resolved_count: int,
    trigger: Trigger,
    expected_control_revision: int,
    simulated_authority: bool = False,
) -> AccountReconciliationResult | None:
    """Atomically bind a final broker snapshot, verdict, and operator receipt."""
    if repo.control_meta_snapshot().control_revision != expected_control_revision:
        return None
    _fold_snapshot_evidence(repo, broker_orders, simulated_authority=simulated_authority)
    instances = repo.strategy_instances()
    plan = plan_account_reconciliation(
        namespaces=frozenset(
            build_bot_order_namespace(instance["strategy_instance_id"]) for instance in instances
        ),
        broker_orders=broker_orders,
        broker_positions=broker_positions,
        attributed_positions=repo.attributed_positions_by_symbol(),
        known_order_refs=frozenset(repo.all_order_refs()),
    )
    for foreign_order in plan.foreign_orders:
        observe_external_order(repo, order=foreign_order)
    _sync_unexplained_order_hold(repo, plan.foreign_orders)
    _sync_position_drift(
        repo,
        drifted_symbols=plan.drifted_symbols,
        indeterminate_symbols=plan.indeterminate_symbols,
        broker_positions=broker_positions,
        attributed_positions=repo.attributed_positions_by_symbol(),
    )
    if plan.verdict == "clean":
        # #1655: verdict is only ever "clean" when both drifted_symbols and
        # indeterminate_symbols are empty (see plan_account_reconciliation),
        # so a non-flat instance here is a genuine flat-exit proof, never one
        # masked by an unproven in-flight mismatch.
        _resolve_flat_exit_fences(repo, instances)
    resolve_incomplete_reconciliation_uncertainty(
        repo,
        evidence_refs=("complete_account_reconciliation",),
    )
    result = AccountReconciliationResult(
        verdict=plan.verdict,
        resolved_count=resolved_count,
        foreign_order_count=len(plan.foreign_orders),
        drifted_symbols=plan.drifted_symbols,
        indeterminate_symbols=plan.indeterminate_symbols,
    )
    if trigger != "OPERATOR_RECONCILE_NOW":
        return result
    receipt_id, recorded_at_ms = _record_operator_reconciliation_receipt(repo, result)
    return replace(result, receipt_id=receipt_id, recorded_at_ms=recorded_at_ms)


__all__ = [
    "AccountReconciliationResult",
    "ReconcilePlan",
    "ReconciliationInvariantError",
    "ReconciliationLockOrderError",
    "plan_account_reconciliation",
    "read_account_open_work",
    "reconcile_account",
]

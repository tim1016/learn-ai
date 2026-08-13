"""Runtime facade for the activated SQLite Alpaca Account Clerk.

Strategy code speaks in semantic ENTER/EXIT decisions and lifecycle runs.  It
never receives the repository or a broker port, and therefore cannot mint an
order identity or bypass capture-before-contact custody.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ChannelHealth,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    EffectOperationReceipt,
    EffectOperationState,
    EffectPurpose,
    HoldState,
    InstanceCustodyProof,
    ReconciliationVerdict,
)
from app.broker.alpaca.clerk.sqlite.commands import (
    CommandSubmission,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import submit_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import cancel_and_prove_owned_entry
from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts, AccountHoldResolvedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.manual_order_runtime import (
    ManualOrderCapability,
    ManualOrderPreview,
    get_manual_ticket,
    manual_order_capability,
    preview_manual_order,
    submit_previewed_manual_order,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission
from app.broker.alpaca.clerk.sqlite.models import ManualOrderTicketResource, OrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.reconcile import (
    AccountReconciliationResult,
)
from app.broker.alpaca.clerk.sqlite.reconcile import (
    reconcile_account as reconcile_sqlite_account,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.stream_health import StreamHealthGate, stream_health_refusal
from app.broker.contract.models import BrokerOrderLeg, OrderSide
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.config import settings
from app.schemas.action_plan import ActionPlan, StockEntryLeg

if TYPE_CHECKING:
    from app.services.bot_binding_repository import BrokerBotBinding

_WORKING_ORDER_STATES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "accepted_for_bidding",
        "pending_cancel",
        "pending_replace",
        "stopped",
        "suspended",
        "calculated",
    }
)
_ENCODED_DECISION_PREFIX = "encoded-"
# Same wire value as the legacy Clerk's STREAM_HEALTH_HOLD_CODE (S4, #1262)
# so evidence surfaces that key off the reason code read identically across
# both authorities.
STREAM_HEALTH_REASON_CODE = "STREAM_HEALTH_HOLD"
logger = logging.getLogger(__name__)


class StrategyRegistrationConflictError(RuntimeError):
    """One strategy identity was reused with different immutable semantics."""


class MissingEntryCustodyError(RuntimeError):
    """An EXIT decision has no SQLite-owned entry identity to target."""


class ReentrantAsyncLock:
    """Task-reentrant intake fence shared by facade and evidence sink."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0

    async def __aenter__(self) -> ReentrantAsyncLock:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("SQLite Clerk intake requires an asyncio task")
        if self._owner is current:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = current
        self._depth = 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        current = asyncio.current_task()
        if self._owner is not current:
            raise RuntimeError("SQLite Clerk intake released by a non-owner task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


class SqliteAlpacaClerkFacade:
    """Narrow live-control surface backed by one account repository."""

    authority_kind = "sqlite"
    broker_id = "alpaca"

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        stream_health: StreamHealthGate | None = None,
    ) -> None:
        self._repo = repo
        self._read = read
        self._trade = trade
        self._stream_health = stream_health
        self._intake = ReentrantAsyncLock()
        self._effect_tasks: dict[tuple[str, str], asyncio.Task[EffectOperationReceipt]] = {}

    @property
    def account_id(self) -> str:
        return self._repo.account_id

    @property
    def repository(self) -> ClerkSqliteRepository:
        """Read-model integration seam; strategy callers never receive this."""
        return self._repo

    @property
    def intake(self) -> ReentrantAsyncLock:
        return self._intake

    def channel_health_snapshot(
        self,
        symbol: str | None = None,
    ) -> tuple[ChannelHealth, ChannelHealth] | None:
        """Expose the exact submission-gate facts to retained status surfaces."""
        if self._stream_health is None:
            return None
        return self._stream_health.snapshot(symbol)

    async def manual_order_capability(self) -> ManualOrderCapability:
        """Return the server-owned policy gate for the manual market tracer."""
        async with self._intake:
            return await manual_order_capability(
                read=self._read,
                stream_health=self._stream_health,
                manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
                control_secret=settings.DATA_PLANE_CONTROL_SECRET,
                allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
                account_id=self.account_id,
            )

    async def preview_manual_order(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        leg_id: str,
        leg: BrokerOrderLeg,
    ) -> ManualOrderPreview:
        """Bind one browser-stable ticket leg to current SQLite authority facts."""
        async with self._intake:
            return await preview_manual_order(
                repo=self._repo,
                read=self._read,
                stream_health=self._stream_health,
                manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
                control_secret=settings.DATA_PLANE_CONTROL_SECRET,
                allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
                account_id=self.account_id,
                operator_id=operator_id,
                ticket_id=ticket_id,
                leg_id=leg_id,
                leg=leg,
            )

    async def submit_manual_order(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        leg_id: str,
        leg: BrokerOrderLeg,
        preview_token: str,
    ) -> ManualOrderSubmission:
        """Accept and submit one previewed manual leg under the intake fence."""
        async with self._intake:
            return await submit_previewed_manual_order(
                repo=self._repo,
                read=self._read,
                trade=self._trade,
                stream_health=self._stream_health,
                manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
                control_secret=settings.DATA_PLANE_CONTROL_SECRET,
                allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
                account_id=self.account_id,
                operator_id=operator_id,
                ticket_id=ticket_id,
                leg_id=leg_id,
                leg=leg,
                preview_token=preview_token,
            )

    def manual_order_ticket(self, ticket_id: str) -> ManualOrderTicketResource | None:
        """Read one durable ticket without presenting repository mutation access."""
        return get_manual_ticket(self._repo, ticket_id)

    async def register_strategy_run(self, binding: BrokerBotBinding) -> None:
        """Durably register immutable strategy + run before order capability."""
        from app.services.bot_carryover import configuration_hash, immutable_configuration_payload

        async with self._intake:
            config_hash = configuration_hash(binding)
            config_json = canonicalize(immutable_configuration_payload(binding))
            display_name = _strategy_display_name(binding.strategy_key)
            existing = self._repo.strategy_instance(binding.strategy_instance_id)
            if existing is None:
                self._repo.register_strategy_instance(
                    strategy_instance_id=binding.strategy_instance_id,
                    symbol=binding.symbol,
                    config_hash=config_hash,
                    strategy_key=binding.strategy_key,
                    display_name=display_name,
                    config_json=config_json,
                )
            elif existing["symbol"].upper() != binding.symbol.upper() or existing["config_hash"] != config_hash:
                raise StrategyRegistrationConflictError(
                    f"strategy instance {binding.strategy_instance_id!r} conflicts with "
                    "its SQLite authority registration"
                )
            else:
                persisted_config = self._repo.bot_config(binding.strategy_instance_id)
                if (
                    persisted_config is None
                    or persisted_config.strategy_key != binding.strategy_key
                    or persisted_config.display_name != display_name
                    or persisted_config.config_json != config_json
                    or persisted_config.config_hash != config_hash
                ):
                    raise StrategyRegistrationConflictError(
                        f"strategy instance {binding.strategy_instance_id!r} conflicts with "
                        "its SQLite authority configuration"
                    )

            active = self._repo.active_run(binding.strategy_instance_id)
            if active is not None:
                if active.lifecycle_run_id == binding.run_id:
                    return
                raise StrategyRegistrationConflictError(
                    f"strategy instance {binding.strategy_instance_id!r} already has "
                    f"active lifecycle run {active.lifecycle_run_id!r}"
                )
            submission = submit_start_run(
                self._repo,
                account_id=self.account_id,
                strategy_instance_id=binding.strategy_instance_id,
                lifecycle_run_id=binding.run_id,
            )
            if submission.command.state != "succeeded":
                raise StrategyRegistrationConflictError(f"SQLite authority rejected lifecycle run {binding.run_id!r}")

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> CommandSubmission:
        """Commit STOP before the process owner cancels strategy evaluation."""
        async with self._intake:
            return submit_stop_run(
                self._repo,
                account_id=self.account_id,
                strategy_instance_id=strategy_instance_id,
                lifecycle_run_id=run_id,
                operator_reason=reason,
            )

    async def execute_for_instance(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
    ) -> EffectOperationReceipt:
        """Route one semantic decision through SQLite ENTER/EXIT custody.

        The task is shielded after creation. Cancellation of the strategy
        caller therefore cannot cancel a broker request whose durable command
        and effect were already accepted; the task finishes or the periodic
        reconciliation sweep recovers its exact identity.
        """
        key = (strategy_instance_id, decision_id)
        task = self._effect_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._execute_effect(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    decision_id=decision_id,
                    purpose=purpose,
                    action_plan=action_plan,
                    quantity=quantity,
                ),
                name=f"alpaca-sqlite-effect:{strategy_instance_id}:{decision_id}",
            )
            self._effect_tasks[key] = task
            task.add_done_callback(lambda completed: self._clear_effect_task(key, completed))
        return await asyncio.shield(task)

    def _clear_effect_task(
        self,
        key: tuple[str, str],
        completed: asyncio.Task[EffectOperationReceipt],
    ) -> None:
        if self._effect_tasks.get(key) is completed:
            self._effect_tasks.pop(key)
        if completed.cancelled():
            logger.error(
                "A shielded SQLite Clerk effect task was unexpectedly cancelled",
                extra={"strategy_instance_id": key[0], "decision_id": key[1]},
            )
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "A SQLite Clerk effect task finished with an error",
                extra={"strategy_instance_id": key[0], "decision_id": key[1]},
                exc_info=(type(error), error, error.__traceback__),
            )

    async def drain_effects(self) -> None:
        """Finish accepted broker custody before the repository is closed."""
        pending = tuple(self._effect_tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _execute_effect(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
    ) -> EffectOperationReceipt:
        async with self._intake:
            entry = _entry_leg(action_plan)
            durable_decision_id = _durable_decision_id(decision_id)
            operation_leg = BrokerOrderLeg(
                symbol=entry.instrument.underlying,
                side=(OrderSide.BUY if entry.position == "long" else OrderSide.SELL),
                quantity=float(quantity * entry.qty_ratio),
            )
            if purpose is EffectPurpose.ENTER:
                if (
                    _sync_stream_health_hold(
                        self._repo,
                        gate=self._stream_health,
                        symbol=entry.instrument.underlying,
                    )
                    is not None
                ):
                    # S4 parity (#1262): either channel unhealthy -> durable
                    # ACCOUNT_CLERK hold + rejected receipt, no broker
                    # contact. EXIT/cancel remain unaffected: exit.py never
                    # consults `active_holds_for_admission`.
                    return _effect_receipt(
                        strategy_instance_id=strategy_instance_id,
                        run_id=run_id,
                        decision_id=decision_id,
                        purpose=purpose,
                        action_plan=action_plan,
                        quantity=quantity,
                        state="rejected",
                        order_refs=(),
                    )
                submitted = await submit_enter(
                    self._repo,
                    account_id=self.account_id,
                    strategy_instance_id=strategy_instance_id,
                    decision_id=durable_decision_id,
                    lifecycle_run_id=run_id,
                    leg=operation_leg,
                    trade=self._trade,
                )
                order_refs = (submitted.order_ref,) if submitted.order_ref is not None else ()
                return _effect_receipt(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    decision_id=decision_id,
                    purpose=purpose,
                    action_plan=action_plan,
                    quantity=quantity,
                    state=submitted.command.state,
                    order_refs=order_refs,
                )

            candidates = [
                order
                for order in self._repo.entry_orders_for_strategy(strategy_instance_id)
                if _entry_symbol(self._repo, order.order_ref).upper() == entry.instrument.underlying.upper()
            ]
            if not candidates:
                raise MissingEntryCustodyError(
                    f"strategy instance {strategy_instance_id!r} has no SQLite-owned "
                    f"entry for {entry.instrument.underlying!r}"
                )
            submitted = await submit_exit(
                self._repo,
                account_id=self.account_id,
                strategy_instance_id=strategy_instance_id,
                decision_id=durable_decision_id,
                lifecycle_run_id=run_id,
                entry_order_ref=candidates[-1].order_ref,
                trade=self._trade,
            )
            order_refs = tuple(
                ref for ref in (submitted.entry_order_ref, submitted.reducing_order_ref) if ref is not None
            )
            return _effect_receipt(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_id=decision_id,
                purpose=purpose,
                action_plan=action_plan,
                quantity=quantity,
                state=submitted.command.state,
                order_refs=order_refs,
            )

    async def recover(self) -> None:
        """Retire pre-restart runs, then recover broker-facing operations."""
        async with self._intake:
            for instance in self._repo.strategy_instances():
                strategy_instance_id = instance["strategy_instance_id"]
                active = self._repo.active_run(strategy_instance_id)
                if active is not None:
                    submit_stop_run(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        lifecycle_run_id=active.lifecycle_run_id,
                        operator_reason="service_restart_recovery",
                    )
            result = await self._reconcile()
            if result.verdict == "stale":
                raise RuntimeError("SQLite Alpaca Clerk recovery could not obtain broker truth")

    async def reconcile_once(self) -> ReconciliationVerdict:
        return _legacy_verdict((await self._reconcile()).verdict)

    async def _reconcile(self) -> AccountReconciliationResult:
        async with self._intake:
            return await reconcile_sqlite_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger="AUTOMATIC",
            )

    async def reconcile_account(
        self,
        *,
        trigger: str,
    ) -> AccountReconciliationResult:
        """Run one full account reconciliation with caller-authored provenance."""
        async with self._intake:
            return await reconcile_sqlite_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger=trigger,
            )

    async def unresolved_effect_count(self) -> int:
        return len(self._repo.reconcilable_effect_operations())

    async def prove_instance_custody(self, strategy_instance_id: str) -> InstanceCustodyProof:
        result = await self._reconcile()
        return self._proof(strategy_instance_id, result)

    async def custody_snapshot(self, strategy_instance_id: str) -> ClerkCustodySnapshot:
        async with self._intake:
            result = await self._reconcile()
            proof = self._proof(strategy_instance_id, result)
            meta = self._repo.control_meta_snapshot()
            exposure = {
                symbol: qty
                for symbol, qty in proof.exposure.items()
                if position_quantity_is_nonzero(qty)
            }
            working = self._working_order_refs_for_proof(strategy_instance_id)
            unresolved = self._unresolved_order_refs(strategy_instance_id)
            trusted = proof.reconciliation_verdict == "clean"
            return ClerkCustodySnapshot(
                broker=self.broker_id,
                account_id=self.account_id,
                strategy_instance_id=strategy_instance_id,
                clerk_generation=(f"sqlite:{meta.authority_generation}:{meta.db_identity_token}"),
                journal_sequence=meta.control_revision,
                reconciliation_state=proof.reconciliation_verdict,
                reconciliation_fresh=proof.reconciliation_verdict != "stale",
                reconciled_at_ms=proof.observed_at_ms,
                exposure=(
                    CustodyExposureFact(
                        state="non_zero" if exposure else "zero",
                        positions=exposure,
                    )
                    if trusted
                    else CustodyExposureFact(state="unknown")
                ),
                working_orders=_count_fact(len(working), trusted=trusted),
                pending_orders=_count_fact(len(unresolved), trusted=trusted),
                terminal_orders=_count_fact(0, trusted=trusted),
                unresolved_effects=_count_fact(len(unresolved), trusted=trusted),
                hold=_hold_state(self._repo, strategy_instance_id),
                freeze=proof.freeze,
                reason_code=("CLERK_CUSTODY_PROVEN" if trusted else "CLERK_CUSTODY_UNPROVABLE"),
                evidence_refs=(
                    f"alpaca-clerk-sqlite:{self.account_id}:{meta.authority_generation}:{meta.control_revision}",
                ),
                next_step=(None if trusted else "Reconcile the SQLite Account Clerk before starting new exposure."),
                observed_at_ms=self._repo.clock(),
            )

    @asynccontextmanager
    async def start_admission_snapshot(self, strategy_instance_id: str) -> AsyncIterator[ClerkCustodySnapshot]:
        async with self._intake:
            snapshot = await self.custody_snapshot(strategy_instance_id)
            yield snapshot

    async def cancel_working_entries_for_instance(self, strategy_instance_id: str) -> tuple[OrderResource, ...]:
        order_refs = self._working_order_refs(strategy_instance_id)
        if not order_refs:
            return ()
        return await self.cancel_verified_working_orders(
            strategy_instance_id=strategy_instance_id,
            order_refs=order_refs,
        )

    async def cancel_verified_working_orders(
        self,
        *,
        strategy_instance_id: str | None,
        order_refs: tuple[str, ...],
    ) -> tuple[OrderResource, ...]:
        """Cancel and exact-poll only policy-authorized owned ENTRY refs."""
        if len(order_refs) != len(set(order_refs)):
            raise ValueError("order_refs must not contain duplicates")
        async with self._intake:
            requested: list[OrderResource] = []
            for order_ref in order_refs:
                order = self._repo.order(order_ref)
                if order is None or order.role != "ENTRY":
                    raise ValueError(f"{order_ref!r} is not an owned ENTRY order")
                owner = self._repo.effect_operation(order.effect_operation_id)
                if owner is None:
                    raise RuntimeError(f"{order_ref!r} has no durable owning effect")
                if strategy_instance_id is not None and owner.strategy_instance_id != strategy_instance_id:
                    raise ValueError(f"{order_ref!r} is not owned by strategy {strategy_instance_id!r}")
                requested.append(order)

            resolved: list[OrderResource] = []
            for order in requested:
                if _is_working_order(order):
                    order = await cancel_and_prove_owned_entry(
                        self._repo,
                        entry_order_ref=order.order_ref,
                        trade=self._trade,
                    )
                resolved.append(order)
            return tuple(resolved)

    def _proof(
        self,
        strategy_instance_id: str,
        result: AccountReconciliationResult,
    ) -> InstanceCustodyProof:
        verdict = _legacy_verdict(result.verdict)
        working = self._working_order_refs_for_proof(strategy_instance_id)
        unresolved = self._unresolved_order_refs(strategy_instance_id)
        freeze = _freeze_state(result, observed_at_ms=self._repo.clock())
        exposure = {
            symbol: quantity
            for symbol, quantity in self._repo.attributed_positions_for_strategy(
                strategy_instance_id
            ).items()
            if position_quantity_is_nonzero(quantity)
        }
        return InstanceCustodyProof(
            account_id=self.account_id,
            strategy_instance_id=strategy_instance_id,
            reconciliation_verdict=verdict,
            freeze=freeze,
            exposure=exposure,
            working_order_refs=working,
            unresolved_intent_refs=unresolved,
            observed_at_ms=self._repo.clock(),
        )

    def _working_order_refs(self, strategy_instance_id: str) -> tuple[str, ...]:
        """ENTRY-only: the exact cancel-target set for STOP (#1396 P1 —
        `cancel_verified_working_orders` rejects anything but an owned
        ENTRY). Custody-proof callers must use
        :meth:`_working_order_refs_for_proof` instead, which also counts a
        live EXIT's still-working REDUCING child.
        """
        return tuple(
            order.order_ref
            for order in self._repo.entry_orders_for_strategy(strategy_instance_id)
            if _is_working_order(order)
        )

    def _working_order_refs_for_proof(self, strategy_instance_id: str) -> tuple[str, ...]:
        """Every still-working order (ENTRY or REDUCING) for custody proof.

        A STOP proof must not report `clean` with an empty working set while
        a live EXIT's reducing order is still `new`/`partially_filled` —
        that order is not "uncertain" (its effect operation is progressing
        normally), so `_unresolved_order_refs` alone cannot catch it either.
        """
        return tuple(
            order.order_ref
            for order in self._repo.orders_for_strategy(strategy_instance_id)
            if _is_working_order(order)
        )

    def _unresolved_order_refs(self, strategy_instance_id: str) -> tuple[str, ...]:
        return tuple(
            order.order_ref
            for order in self._repo.uncertain_orders()
            if (
                (effect := self._repo.effect_operation(order.effect_operation_id)) is not None
                and effect.strategy_instance_id == strategy_instance_id
            )
        )


def _sync_stream_health_hold(
    repo: ClerkSqliteRepository,
    *,
    gate: StreamHealthGate | None,
    symbol: str | None = None,
) -> tuple[str, str] | None:
    """Raise/refresh or resolve the durable stream-health hold; return the
    refusal (reason, detail) when either channel is unhealthy, else None.

    Mirrors `reconcile.py`'s `_sync_unexplained_order_hold` evidence-driven
    shape: no gate installed ("gate is None" — production wiring always
    installs one) refuses nothing.
    """
    refusal = stream_health_refusal(gate, symbol=symbol)
    if refusal is None:
        facts = AccountHoldResolvedFacts(reason_code=STREAM_HEALTH_REASON_CODE, evidence_refs=[])
        repo.resolve_account_hold_if_active(
            reason_code=STREAM_HEALTH_REASON_CODE,
            build_transition=lambda: TransitionInput(
                transition_kind="ACCOUNT_HOLD_RESOLVED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                clerk_observed_at_ms=repo.clock(),
                summary_code="ACCOUNT_HOLD_RESOLVED_BY_STREAM_RECOVERY",
                facts_json=facts.to_facts_json(),
            ),
        )
        return None

    _reason, detail = refusal
    evidence_refs = [detail]
    facts = AccountHoldRaisedFacts(reason_code=STREAM_HEALTH_REASON_CODE, evidence_refs=evidence_refs)

    def transition(kind: str) -> TransitionInput:
        return TransitionInput(
            transition_kind=kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=kind,
            facts_json=facts.to_facts_json(),
        )

    repo.observe_account_hold(
        reason_code=STREAM_HEALTH_REASON_CODE,
        evidence_refs_json=canonicalize(evidence_refs),
        build_raise=lambda: transition("ACCOUNT_HOLD_RAISED"),
        build_refresh=lambda: transition("ACCOUNT_HOLD_REFRESHED"),
    )
    return refusal


def _entry_leg(action_plan: ActionPlan) -> StockEntryLeg:
    if len(action_plan.on_enter) != 1 or not isinstance(action_plan.on_enter[0], StockEntryLeg):
        raise ValueError("SQLite Alpaca effects require exactly one stock entry leg")
    return action_plan.on_enter[0]


def _durable_decision_id(decision_id: str) -> str:
    """Encode delimiter-bearing legacy decision IDs without losing identity."""
    if ":" not in decision_id and not decision_id.startswith(_ENCODED_DECISION_PREFIX):
        return decision_id
    encoded = base64.urlsafe_b64encode(decision_id.encode("utf-8")).rstrip(b"=")
    return f"{_ENCODED_DECISION_PREFIX}{encoded.decode('ascii')}"


def _is_working_order(order: OrderResource) -> bool:
    return (order.broker_state or "").lower() in _WORKING_ORDER_STATES


def _strategy_display_name(strategy_key: str) -> str:
    """Resolve the engine-owned immutable presentation label for one strategy."""
    from app.services.strategy_validation_manifest import strategy_registry_seeds

    for strategy in strategy_registry_seeds():
        if strategy.strategy_key == strategy_key:
            return strategy.display_name
    raise StrategyRegistrationConflictError(
        f"strategy {strategy_key!r} has no registered immutable display name"
    )


def _entry_symbol(repo: ClerkSqliteRepository, order_ref: str) -> str:
    from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol

    return entry_order_symbol(repo, order_ref)


def _effect_receipt(
    *,
    strategy_instance_id: str,
    run_id: str,
    decision_id: str,
    purpose: EffectPurpose,
    action_plan: ActionPlan,
    quantity: int,
    state: str,
    order_refs: tuple[str, ...],
) -> EffectOperationReceipt:
    from app.broker.alpaca.clerk.models import AlpacaEffectOperation

    mapped = {
        "reserved": EffectOperationState.ACCEPTED,
        "accepted": EffectOperationState.ACCEPTED,
        "in_progress": (
            EffectOperationState.SUBMITTED if purpose is EffectPurpose.ENTER else EffectOperationState.EXIT_PENDING
        ),
        "unknown": EffectOperationState.UNCERTAIN,
        "succeeded": (EffectOperationState.SUBMITTED if purpose is EffectPurpose.ENTER else EffectOperationState.FLAT),
        "failed": EffectOperationState.REJECTED,
        "rejected": EffectOperationState.REJECTED,
    }[state]
    return EffectOperationReceipt(
        operation=AlpacaEffectOperation(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
        ),
        state=mapped,
        explanation=f"The SQLite Account Clerk recorded {purpose.value} as {state}.",
        next_step=(
            "Await fresh broker evidence and automatic reconciliation."
            if mapped
            in {
                EffectOperationState.ACCEPTED,
                EffectOperationState.SUBMITTED,
                EffectOperationState.EXIT_PENDING,
                EffectOperationState.UNCERTAIN,
            }
            else None
        ),
        child_order_refs=order_refs,
    )


def _legacy_verdict(value: str) -> ReconciliationVerdict:
    if value == "position_drift":
        return "missing_intent"
    if value == "clean":
        return "clean"
    if value == "unexplained_order":
        return "unexplained_order"
    if value == "missing_intent":
        return "missing_intent"
    if value == "stale":
        return "stale"
    raise ValueError(f"unsupported SQLite reconciliation verdict {value!r}")


def _freeze_state(
    result: AccountReconciliationResult,
    *,
    observed_at_ms: int,
) -> AccountFreezeState:
    verdict = _legacy_verdict(result.verdict)
    if verdict == "clean":
        return AccountFreezeState()
    category = (
        "ACCOUNT_STATE_UNATTRIBUTABLE"
        if verdict in {"unexplained_order", "missing_intent"}
        else "ACCOUNT_STATE_UNPROVABLE"
    )
    return AccountFreezeState(
        active=True,
        category=category,
        explanation=(
            "The SQLite Account Clerk cannot attribute current broker state."
            if category == "ACCOUNT_STATE_UNATTRIBUTABLE"
            else "The SQLite Account Clerk could not obtain fresh broker proof."
        ),
        next_step="Reconcile the account before allowing new exposure.",
        observed_at_ms=observed_at_ms,
    )


def _hold_state(
    repo: ClerkSqliteRepository,
    strategy_instance_id: str,
) -> HoldState:
    holds = repo.active_holds_for_admission(strategy_instance_id=strategy_instance_id)
    if not holds:
        return HoldState(active=False)
    hold = max(holds, key=lambda row: row["opened_at_ms"])
    return HoldState(
        active=True,
        reason_code=hold["reason_code"],
        reason="The SQLite Account Clerk has an active safety hold.",
        since_ms=hold["opened_at_ms"],
    )


def _count_fact(count: int, *, trusted: bool) -> CustodyCountFact:
    if not trusted:
        return CustodyCountFact(state="unknown")
    return CustodyCountFact(state="non_zero" if count else "zero", count=count)


__all__ = [
    "MissingEntryCustodyError",
    "ReentrantAsyncLock",
    "SqliteAlpacaClerkFacade",
    "StrategyRegistrationConflictError",
]

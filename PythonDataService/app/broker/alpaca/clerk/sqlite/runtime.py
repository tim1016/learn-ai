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
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.account_authority import (
    require_real_paper_account_id,
    require_synthetic_account_id,
)
from app.broker.alpaca.clerk.active_protocol import ClerkAdmissionSnapshotStaleError
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
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
from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    GuardedBrokerTradePort,
    guard_broker_ports,
)
from app.broker.alpaca.clerk.sqlite.commands import (
    CommandSubmission,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.decision_receipts import AtomicDecisionReceipt
from app.broker.alpaca.clerk.sqlite.enter import accept_enter, submit_accepted_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_accepted_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import cancel_and_prove_owned_entry
from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts, AccountHoldResolvedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import (
    HistoricalExecutionRecoveryPlan,
    HistoricalExecutionRecoveryRefused,
    confirm_historical_execution_recovery,
    prepare_historical_execution_recovery,
    replay_historical_execution_recovery,
)
from app.broker.alpaca.clerk.sqlite.idempotency import UnknownEntryOrderError
from app.broker.alpaca.clerk.sqlite.intake_fence import (
    IntakeFenceYieldError,
    ReentrantAsyncLock,
)
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancellationSubmission,
    submit_manual_order_cancellation,
    submit_manual_ticket_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_order_runtime import (
    ManualOrderCapability,
    ManualOrderPreview,
    get_manual_ticket,
    manual_order_capability,
    preview_manual_order,
    submit_previewed_manual_order,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission, ManualTicketLeg
from app.broker.alpaca.clerk.sqlite.models import (
    ExecutionCoverageResolutionReceipt,
    ManualOrderTicketResource,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.reconcile import (
    AccountReconciliationResult,
)
from app.broker.alpaca.clerk.sqlite.reconcile import (
    reconcile_account as reconcile_sqlite_account,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryPolicyContext
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.alpaca.clerk.stream_health import StreamHealthGate, stream_health_refusal
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.config import settings
from app.schemas.action_plan import ActionPlan, StockEntryLeg
from app.services.broker_capability_service import extended_phase_proven_at_ms
from app.services.market_liveness import liveness_blocks_entry, market_liveness_fact

if TYPE_CHECKING:
    from app.services.bot_binding_repository import BrokerBotBinding
    from app.services.source_bar_ledger import RetainedSourceBar

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


class _DecisionBarBoundTradePort:
    """Bind each synthetic Clerk order to one immutable retained decision bar."""

    def __init__(self, inner: GuardedBrokerTradePort, retained_bar: RetainedSourceBar) -> None:
        self._inner = inner
        self._retained_bar = retained_bar

    @property
    def broker_id(self) -> str:
        return self._inner.broker_id

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self._inner.bind_evaluated_bar(client_order_id, self._retained_bar)
        return await self._inner.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        await self._inner.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return await self._inner.get_order_by_client_order_id(client_order_id)


class StrategyRegistrationConflictError(RuntimeError):
    """One strategy identity was reused with different immutable semantics."""


class SealedAccountMismatchError(RuntimeError):
    """A deployment seal names an account other than this Clerk repository."""

    reason_code = "SEALED_ACCOUNT_MISMATCH"


class StrategyAdmissionStaleError(ClerkAdmissionSnapshotStaleError):
    """A Start or Resume snapshot no longer matches SQLite Clerk authority."""


class MissingEntryCustodyError(RuntimeError):
    """An EXIT decision has no SQLite-owned entry identity to target."""


class SqliteAlpacaClerkFacade:
    """Narrow live-control surface backed by one account repository."""

    authority_kind: Literal["sqlite", "synthetic"]
    broker_id = "alpaca"
    supports_revision_bound_admission = True

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        stream_health: StreamHealthGate | None = None,
        intake: ReentrantAsyncLock | None = None,
        authority_kind: Literal["sqlite", "synthetic"] = "sqlite",
    ) -> None:
        if authority_kind == "synthetic":
            require_synthetic_account_id(repo.account_id)
        else:
            require_real_paper_account_id(repo.account_id)
        self._repo = repo
        self._intake = intake or ReentrantAsyncLock()
        self._read, self._trade = guard_broker_ports(read=read, trade=trade, intake=self._intake)
        self._stream_health = stream_health
        self.authority_kind = authority_kind
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
        return await manual_order_capability(
            read=self._read,
            stream_health=self._stream_health,
            manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
            account_id=self.account_id,
        )

    async def prepare_historical_execution_recovery(
        self,
        *,
        context: RecoveryPolicyContext,
        concurrency_token: str,
    ) -> HistoricalExecutionRecoveryPlan:
        """Read paper broker evidence outside the Clerk intake boundary."""
        return await prepare_historical_execution_recovery(
            repo=self._repo,
            read=self._read,
            context=context,
            concurrency_token=concurrency_token,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
        )

    async def confirm_historical_execution_recovery(
        self,
        *,
        plan: HistoricalExecutionRecoveryPlan,
        confirmation_token: str,
    ) -> ExecutionCoverageResolutionReceipt:
        """Append only the signed plan's exact evidence and closed resolution."""
        replay = await asyncio.to_thread(
            replay_historical_execution_recovery,
            repo=self._repo,
            plan=plan,
            confirmation_token=confirmation_token,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
        )
        if replay is not None:
            return replay
        try:
            account = await self._read.get_account()
        except BrokerError as exc:
            raise HistoricalExecutionRecoveryRefused(
                "HISTORICAL_EVIDENCE_UNAVAILABLE",
                "Alpaca account evidence is temporarily unavailable. Keep the exposure blocked and retry later.",
            ) from exc
        except Exception as exc:
            raise HistoricalExecutionRecoveryRefused(
                "HISTORICAL_EVIDENCE_UNAVAILABLE",
                "Alpaca account evidence is temporarily unavailable. Keep the exposure blocked and retry later.",
            ) from exc
        return await asyncio.to_thread(
            confirm_historical_execution_recovery,
            repo=self._repo,
            plan=plan,
            confirmation_token=confirmation_token,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
            observed_account=account,
        )

    async def preview_manual_order(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        legs: tuple[ManualTicketLeg, ...],
    ) -> ManualOrderPreview:
        """Bind one browser-stable ticket leg to current SQLite authority facts."""
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
            legs=legs,
        )

    async def submit_manual_order(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        legs: tuple[ManualTicketLeg, ...],
        preview_token: str,
        continuation: bool = False,
    ) -> ManualOrderSubmission:
        """Accept locally, then drive the previewed manual leg outside intake."""
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
            legs=legs,
            preview_token=preview_token,
            continuation=continuation,
        )

    def manual_order_ticket(self, ticket_id: str) -> ManualOrderTicketResource | None:
        """Read one durable ticket without presenting repository mutation access."""
        return get_manual_ticket(self._repo, ticket_id)

    async def cancel_manual_order(
        self,
        *,
        operator_id: str,
        order_ref: str,
        cancel_request_id: str,
    ) -> ManualOrderCancellationSubmission:
        """Accept and reconcile one durable manual-order cancellation."""
        return await submit_manual_order_cancellation(
            self._repo,
            account_id=self.account_id,
            operator_id=operator_id,
            order_ref=order_ref,
            cancel_request_id=cancel_request_id,
            trade=self._trade,
        )

    async def cancel_manual_ticket(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        cancel_request_id: str,
    ) -> ManualOrderTicketResource:
        """Cancel the ticket's owned working orders with one replayable request."""
        await submit_manual_ticket_cancellation(
            self._repo,
            account_id=self.account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            cancel_request_id=cancel_request_id,
            trade=self._trade,
        )
        ticket = get_manual_ticket(self._repo, ticket_id)
        if ticket is None:
            raise RuntimeError("manual ticket disappeared after cancellation")
        return ticket

    async def register_strategy_run(
        self,
        binding: BrokerBotBinding,
        *,
        admission_snapshot: ClerkCustodySnapshot | None = None,
    ) -> None:
        """Durably register immutable strategy + run before order capability."""
        from app.services.bot_carryover import configuration_hash, immutable_configuration_payload

        async with self._intake:
            if binding.sealed_account_id != self.account_id:
                raise SealedAccountMismatchError(
                    f"{SealedAccountMismatchError.reason_code}: sealed account "
                    f"{binding.sealed_account_id!r} does not match "
                    f"authority account {self.account_id!r}"
                )
            if admission_snapshot is not None:
                self._require_current_admission_snapshot(binding, admission_snapshot)
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

    def _require_current_admission_snapshot(
        self,
        binding: BrokerBotBinding,
        snapshot: ClerkCustodySnapshot,
    ) -> None:
        """Validate the explicit two-phase Start/Resume token at activation."""
        meta = self._repo.control_meta_snapshot()
        expected_generation = f"sqlite:{meta.authority_generation}:{meta.db_identity_token}"
        if (
            snapshot.account_id != self.account_id
            or snapshot.strategy_instance_id != binding.strategy_instance_id
            or snapshot.clerk_generation != expected_generation
            or snapshot.journal_sequence != meta.control_revision
            or not snapshot.reconciliation_fresh
            or snapshot.reconciliation_state != "clean"
        ):
            raise StrategyAdmissionStaleError(
                "SQLite Clerk admission changed after preparation; refresh Start or Resume before activation"
            )

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
        use_rth: bool = True,
        capability_account_id: str | None = None,
        retained_source_bar: RetainedSourceBar | None = None,
        decision_evidence: EffectDecisionEvidence | None = None,
    ) -> EffectOperationReceipt:
        """Route one semantic decision through SQLite ENTER/EXIT custody.

        The task is shielded after creation. Cancellation of the strategy
        caller therefore cannot cancel a broker request whose durable command
        and effect were already accepted; the task finishes or the periodic
        reconciliation sweep recovers its exact identity.
        """
        key = (strategy_instance_id, decision_id)
        if decision_evidence is not None and decision_evidence.evaluation_id != decision_id:
            raise ValueError("decision_id must equal the Signal Program evaluation_id")
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
                    use_rth=use_rth,
                    capability_account_id=capability_account_id,
                    retained_source_bar=retained_source_bar,
                    decision_evidence=decision_evidence,
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
        use_rth: bool = True,
        capability_account_id: str | None = None,
        retained_source_bar: RetainedSourceBar | None = None,
        decision_evidence: EffectDecisionEvidence | None = None,
    ) -> EffectOperationReceipt:
        def rejected(
            *,
            reason_code: str,
            explanation: str,
            next_step: str | None = None,
            order_refs: tuple[str, ...] = (),
        ) -> EffectOperationReceipt:
            if decision_evidence is not None:
                _append_pre_custody_refusal(
                    self._repo,
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    evidence=decision_evidence,
                    reason_code=reason_code,
                    explanation=explanation,
                )
            return _effect_receipt(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_id=decision_id,
                purpose=purpose,
                action_plan=action_plan,
                quantity=quantity,
                state="rejected",
                order_refs=order_refs,
                explanation=f"{reason_code}: {explanation}",
                next_step=next_step,
            )

        async with self._intake:
            if self.authority_kind == "synthetic" and retained_source_bar is None:
                return rejected(
                    reason_code="SIMULATED_SOURCE_BAR_UNPROVEN",
                    explanation=(
                        "Synthetic custody received no exact retained source bar for this decision."
                    ),
                    next_step="Replay or ingest the decision bar, then retry through the sealed program.",
                )
            entry = _entry_leg(action_plan)
            durable_decision_id = _durable_decision_id(decision_id)
            atomic_receipt = (
                None
                if decision_evidence is None
                else AtomicDecisionReceipt(
                    outcome=decision_evidence.outcome,
                    symbol=decision_evidence.symbol,
                    decision_id=decision_evidence.evaluation_id,
                    observed_at_ms=decision_evidence.observed_at_ms,
                    facts_json=canonicalize(
                        {
                            "bar_ref": decision_evidence.bar_ref,
                            "decision_id": decision_evidence.evaluation_id,
                            "evaluation_id": decision_evidence.evaluation_id,
                            "reason_code": decision_evidence.reason_code,
                            "trace_digest": decision_evidence.trace_digest,
                            "decision_bar_close_ms": decision_evidence.decision_bar_close_ms,
                        }
                    ),
                )
            )
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
                    return rejected(
                        reason_code=STREAM_HEALTH_REASON_CODE,
                        explanation="The required market-data or execution channel is unhealthy.",
                        next_step="Restore both channels and let the Clerk observe fresh health evidence.",
                    )
                # #1671: the caller's own liveness check happens before this
                # sole-writer boundary is even reached, racing whatever
                # queues ahead of it under `self._intake`. Recheck here,
                # immediately before the entry is accepted, so evidence that
                # went stale or turned HALTED while queued cannot still
                # reach the broker — the same shared predicate
                # bot_trade_strategy.py's own gate uses, so the two can
                # never silently diverge.
                if self.authority_kind == "sqlite":
                    liveness = market_liveness_fact(entry.instrument.underlying, self._repo.clock())
                    if liveness_blocks_entry(
                        liveness,
                        use_rth=use_rth,
                        # Must be the feed's capability account, not
                        # ``self.account_id`` (Alpaca execution custody) — that
                        # can never scope an IBKR market-data entitlement, so
                        # every extended-hours entry would be rejected here.
                        extended_phase_proven=lambda: extended_phase_proven_at_ms(
                            now_ms=self._repo.clock(),
                            symbol=entry.instrument.underlying,
                            account_id=capability_account_id,
                        ),
                    ):
                        return rejected(
                            reason_code="MARKET_LIVENESS_BLOCKED",
                            explanation="Current market-liveness evidence does not permit new exposure.",
                            next_step="Wait for fresh tradable-market evidence before retrying ENTER.",
                        )
                try:
                    accepted_enter = accept_enter(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=durable_decision_id,
                        lifecycle_run_id=run_id,
                        leg=operation_leg,
                        decision_receipt=atomic_receipt,
                    )
                except AdmissionBlockedError as exc:
                    return rejected(
                        reason_code=exc.decision.reason_code or "ENTER_ADMISSION_BLOCKED",
                        explanation=exc.decision.why or "The Clerk refused new exposure.",
                    )
            else:
                active_exit = self._repo.active_exit_for_strategy(strategy_instance_id)
                if active_exit is not None:
                    # FR-019/FR-023: this evaluation reached the custody seam
                    # and was answered by an effect that already exists. It is
                    # still a decision that happened, so it leaves durable
                    # evidence linked to the effect that resolved it -- without
                    # that link the causal read (FR-030) cannot later explain
                    # why this evaluation produced no effect of its own.
                    if atomic_receipt is not None:
                        self._repo.capture_decision_against_active_exit(
                            strategy_instance_id=strategy_instance_id,
                            run_id=run_id,
                            decision_receipt=atomic_receipt,
                        )
                    return _effect_receipt(
                        strategy_instance_id=strategy_instance_id,
                        run_id=run_id,
                        decision_id=decision_id,
                        purpose=purpose,
                        action_plan=action_plan,
                        quantity=quantity,
                        state=active_exit.state,
                        order_refs=tuple(
                            order.order_ref
                            for order in self._repo.orders_for_effect_operation(
                                active_exit.effect_operation_id
                            )
                        ),
                        explanation=(
                            f"EXIT_IN_PROGRESS: existing EXIT {active_exit.effect_operation_id} "
                            "already owns this strategy's reduction custody."
                        ),
                        next_step="Await the existing EXIT custody outcome; do not submit another EXIT.",
                    )
                candidates = [
                    order
                    for order in self._repo.entry_orders_for_strategy(strategy_instance_id)
                    if _entry_symbol(self._repo, order.order_ref).upper() == entry.instrument.underlying.upper()
                ]
                if not candidates:
                    return rejected(
                        reason_code="EXIT_CUSTODY_UNPROVEN",
                        explanation=(
                            f"No SQLite-owned entry exists for {entry.instrument.underlying!r}; "
                            "the Clerk cannot prove a safe reduction target."
                        ),
                        next_step="Reconcile the instance custody before attempting another EXIT.",
                    )
                try:
                    accepted_exit = accept_exit(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=durable_decision_id,
                        lifecycle_run_id=run_id,
                        entry_order_ref=candidates[-1].order_ref,
                        decision_receipt=atomic_receipt,
                    )
                except UnknownEntryOrderError:
                    # The lookup and accept both occur under the Clerk intake
                    # lock.  If the target vanished despite that, return a
                    # closed custody result rather than ending the bot task
                    # while economic exposure may still exist.
                    return rejected(
                        reason_code="EXIT_CUSTODY_UNPROVEN",
                        explanation="The Clerk could not prove that the selected entry still belongs to this EXIT.",
                        next_step="Reconcile the instance custody before attempting another EXIT.",
                    )

        trade: BrokerTradePort
        if self.authority_kind == "synthetic":
            assert retained_source_bar is not None
            trade = _DecisionBarBoundTradePort(self._trade, retained_source_bar)
        else:
            trade = self._trade

        if purpose is EffectPurpose.ENTER:
            submitted_enter = await submit_accepted_enter(
                self._repo,
                accepted=accepted_enter,
                leg=operation_leg,
                trade=trade,
            )
            order_refs = (
                (submitted_enter.order_ref,) if submitted_enter.order_ref is not None else ()
            )
            return _effect_receipt(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_id=decision_id,
                purpose=purpose,
                action_plan=action_plan,
                quantity=quantity,
                state=submitted_enter.command.state,
                order_refs=order_refs,
            )

        submitted_exit = await resolve_accepted_exit(
            self._repo,
            accepted=accepted_exit,
            trade=trade,
        )
        order_refs = tuple(
            ref
            for ref in (submitted_exit.entry_order_ref, submitted_exit.reducing_order_ref)
            if ref is not None
        )
        return _effect_receipt(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
            state=submitted_exit.command.state,
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
        return await reconcile_sqlite_account(
            self._repo,
            read=self._read,
            trade=self._trade,
            trigger="AUTOMATIC",
            intake=self._intake,
        )

    async def reconcile_account(
        self,
        *,
        trigger: str,
    ) -> AccountReconciliationResult:
        """Run one full account reconciliation with caller-authored provenance."""
        return await reconcile_sqlite_account(
            self._repo,
            read=self._read,
            trade=self._trade,
            trigger=trigger,
            intake=self._intake,
        )

    async def unresolved_effect_count(self) -> int:
        return len(self._repo.reconcilable_effect_operations())

    async def prove_instance_custody(self, strategy_instance_id: str) -> InstanceCustodyProof:
        result = await self._reconcile()
        return self._proof(strategy_instance_id, result)

    async def custody_snapshot(self, strategy_instance_id: str) -> ClerkCustodySnapshot:
        result = await self._reconcile()
        async with self._intake:
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


def _append_pre_custody_refusal(
    repository: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    run_id: str,
    evidence: EffectDecisionEvidence,
    reason_code: str,
    explanation: str,
) -> None:
    """Durably close one evaluation before returning a custody refusal."""
    repository.append_decision_receipt(
        strategy_instance_id=strategy_instance_id,
        outcome="blocked",
        symbol=evidence.symbol,
        intent_id=evidence.evaluation_id,
        order_ref=None,
        observed_at_ms=evidence.observed_at_ms,
        facts_json=canonicalize(
            {
                "bar_ref": evidence.bar_ref,
                "decision_id": evidence.evaluation_id,
                "evaluation_id": evidence.evaluation_id,
                "run_id": run_id,
                "reason_code": reason_code,
                "refusal_reason": explanation,
                "retention_class": "protected_refusal",
                "trace_digest": evidence.trace_digest,
                "decision_bar_close_ms": evidence.decision_bar_close_ms,
            }
        ),
    )


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
    explanation: str | None = None,
    next_step: str | None = None,
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
        explanation=explanation or f"The SQLite Account Clerk recorded {purpose.value} as {state}.",
        next_step=next_step
        if next_step is not None
        else (
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
    "IntakeFenceYieldError",
    "MissingEntryCustodyError",
    "ReentrantAsyncLock",
    "SqliteAlpacaClerkFacade",
    "StrategyRegistrationConflictError",
]

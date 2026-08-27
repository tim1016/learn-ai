"""MarketDataFeed and Alpaca-Clerk test doubles shared by the bot_runner
test package and outside suites that exercise ``BotTaskRegistry`` /
``run_trade_bot`` end to end.

Split out of ``tests/services/bot_runner/conftest.py`` per issue #1810 --
see that module's sibling ``custody.py``/``market.py``/``ema_parity.py``
for the other extracted themes.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from app.broker.alpaca.clerk.models import (
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    EffectOperationReceipt,
    EffectPurpose,
    HoldState,
    InstanceCustodyProof,
    ReconciliationVerdict,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.schemas.action_plan import ActionPlan
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_lifecycle_projection import AlpacaLifecycleAuthoritySnapshot
from app.utils.timestamps import now_ms_utc

from .custody import _T0


class _FakeFeed:
    """MarketDataFeed test double.

    ``mode``:
    - ``finite``  — yield the given bars, then end (BAR_STREAM_ENDED path).
    - ``hold``    — yield the bars, then wait forever (stop/cancel paths).
    - ``crash``   — yield the bars, then raise ``error``.
    """

    feed_id = "fake"

    def __init__(
        self,
        bars: list[MarketDataBar],
        *,
        mode: str = "hold",
        error: Exception | None = None,
        observed_at_ms: int | None = None,
    ) -> None:
        self._bars = bars
        self._mode = mode
        self._error = error
        self.bars_consumed = 0
        self._observed_at_ms = observed_at_ms

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        if self._mode == "crash":
            assert self._error is not None
            raise self._error
        if self._mode == "hold":
            await asyncio.Event().wait()

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        return []

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._bars[-1].start_ms if self._bars else None,
            reason="",
            active_subscription_count=0,
            observed_at_ms=self._observed_at_ms or now_ms_utc(),
        )


class _TestDecisionReceiptRepository:
    """Minimal receipt store for active-SQLite Clerk doubles in runner tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], DecisionReceiptResource] = {}

    def append_decision_receipt(
        self,
        *,
        strategy_instance_id: str,
        outcome: str,
        symbol: str | None,
        intent_id: str | None,
        order_ref: str | None,
        observed_at_ms: int,
        facts_json: str,
    ) -> DecisionReceiptResource:
        facts = json.loads(facts_json)
        bar_ref = facts["bar_ref"]
        row = DecisionReceiptResource(
            strategy_instance_id=strategy_instance_id,
            seq=len(self._rows) + 1,
            outcome=outcome,
            symbol=symbol,
            intent_id=intent_id,
            order_ref=order_ref,
            observed_at_ms=observed_at_ms,
            facts_json=facts_json,
        )
        self._rows[(strategy_instance_id, bar_ref)] = row
        return row

    def update_decision_receipt_for_bar(
        self,
        *,
        strategy_instance_id: str,
        bar_ref: str,
        outcome: str,
        order_ref: str | None,
        facts_json: str,
    ) -> DecisionReceiptResource:
        key = (strategy_instance_id, bar_ref)
        updated = self._rows[key].model_copy(
            update={
                "outcome": outcome,
                "order_ref": order_ref,
                "facts_json": facts_json,
            }
        )
        self._rows[key] = updated
        return updated

    def decision_receipt_tail(
        self,
        *,
        strategy_instance_id: str,
        limit: int,
    ) -> list[DecisionReceiptResource]:
        """FR-016: warmup replay reads this to reapply known dispositions.

        Dict insertion order matches ascending ``seq`` order here (``seq``
        is assigned once at append; ``update_decision_receipt_for_bar``
        replaces a row in place without reordering it), mirroring the real
        repository's "bounded newest suffix in ascending sequence order".
        """
        matching = [
            row for (sid, _bar_ref), row in self._rows.items() if sid == strategy_instance_id
        ]
        matching.sort(key=lambda row: row.seq)
        return matching[-limit:] if limit > 0 else []


class _CustodyClerk:
    authority_kind = "sqlite"
    broker_id = "alpaca"
    account_id = "PA-TEST"

    def __init__(self, proof: InstanceCustodyProof) -> None:
        self.proof = proof
        self.repository = _TestDecisionReceiptRepository()
        self.cancel_calls: list[str] = []
        self.registered_runs: list[str] = []
        self.stopped_runs: list[str] = []
        self.active_runs: dict[str, str] = {}
        self.known_runs: set[tuple[str, str]] = set()

    async def register_strategy_run(self, binding: BrokerBotBinding) -> None:
        self.registered_runs.append(binding.run_id)
        self.active_runs[binding.strategy_instance_id] = binding.run_id
        self.known_runs.add((binding.strategy_instance_id, binding.run_id))

    async def recover(self) -> None:
        return

    async def unresolved_effect_count(self, *, subject_id: str | None = None) -> int:
        del subject_id
        return 0

    async def reconcile_once(self) -> ReconciliationVerdict:
        return self.proof.reconciliation_verdict

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
        decision_evidence=None,
    ) -> EffectOperationReceipt:
        del strategy_instance_id, run_id, decision_id, purpose, action_plan, quantity, use_rth, capability_account_id, decision_evidence
        raise AssertionError("custody-only test Clerk cannot execute effects")

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> None:
        del reason
        self.stopped_runs.append(run_id)
        if self.active_runs.get(strategy_instance_id) == run_id:
            self.active_runs.pop(strategy_instance_id)

    def lifecycle_snapshot(
        self,
        strategy_instance_id: str,
        expected_run_id: str | None,
    ) -> AlpacaLifecycleAuthoritySnapshot:
        active_run_id = self.active_runs.get(strategy_instance_id)
        return AlpacaLifecycleAuthoritySnapshot(
            strategy_instance_exists=(
                active_run_id is not None
                or any(candidate[0] == strategy_instance_id for candidate in self.known_runs)
            ),
            active_run_id=active_run_id,
            retired_at_ms=None,
            expected_run_state=(
                None
                if expected_run_id is None
                else (
                    "ACTIVE"
                    if active_run_id == expected_run_id
                    else (
                        "STOPPED"
                        if (strategy_instance_id, expected_run_id) in self.known_runs
                        else "MISSING"
                    )
                )
            ),
            control_revision=len(self.known_runs) + len(self.stopped_runs),
        )

    def lifecycle_recovery_candidates(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.active_runs.items()))

    async def cancel_working_entries_for_instance(self, sid: str) -> tuple:
        self.cancel_calls.append(sid)
        return ()

    async def prove_instance_custody(self, sid: str) -> InstanceCustodyProof:
        assert sid == self.proof.strategy_instance_id
        return self.proof

    @asynccontextmanager
    async def start_admission_snapshot(self, sid: str):
        assert sid == self.proof.strategy_instance_id
        zero = CustodyCountFact(state="zero", count=0)
        exposure = (
            CustodyExposureFact(state="non_zero", positions=self.proof.exposure)
            if self.proof.exposure
            else CustodyExposureFact(state="zero", positions={})
        )
        yield ClerkCustodySnapshot(
            broker="alpaca",
            account_id=self.proof.account_id,
            strategy_instance_id=sid,
            clerk_generation="test-clerk",
            journal_sequence=1,
            reconciliation_state=self.proof.reconciliation_verdict,
            reconciliation_fresh=self.proof.reconciliation_verdict == "clean",
            reconciled_at_ms=now_ms_utc(),
            exposure=exposure,
            working_orders=zero,
            pending_orders=zero,
            terminal_orders=zero,
            unresolved_effects=zero,
            hold=HoldState(active=False),
            freeze=self.proof.freeze,
            reason_code=(
                "CLERK_CUSTODY_PROVEN"
                if self.proof.reconciliation_verdict == "clean"
                else "CLERK_CUSTODY_UNPROVABLE"
            ),
            evidence_refs=("test-clerk:1",),
            observed_at_ms=now_ms_utc(),
        )


class _SqliteRuntimeBroker:
    broker_id = "alpaca"

    def __init__(self) -> None:
        self.orders: dict[str, BrokerOrder] = {}
        self.cancellations: list[str] = []

    async def list_orders(self, **_kwargs) -> list[BrokerOrder]:
        return list(self.orders.values())

    async def list_positions(self) -> list:
        return []

    async def submit(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
    ) -> BrokerOrder:
        order = BrokerOrder(
            broker="alpaca",
            order_id=f"broker-{len(self.orders) + 1}",
            client_order_id=client_order_id,
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side,
            order_type="market",
            time_in_force="day",
            quantity=leg.quantity,
            filled_quantity=0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=_T0,
            created_at_ms=_T0,
            updated_at_ms=_T0,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=_T0,
        )
        self.orders[client_order_id] = order
        return order

    async def cancel(self, order_id: str) -> None:
        self.cancellations.append(order_id)
        for client_order_id, order in self.orders.items():
            if order.order_id == order_id:
                self.orders[client_order_id] = order.model_copy(
                    update={
                        "status": "canceled",
                        "updated_at_ms": _T0 + 1,
                        "canceled_at_ms": _T0 + 1,
                    }
                )
                return

    async def get_order_by_client_order_id(
        self,
        client_order_id: str,
    ) -> BrokerOrder | None:
        return self.orders.get(client_order_id)


class _FakeEffectResult:
    """Minimal Clerk-authored effect receipt used by the strategy adapter.

    Kept here rather than beside its sole direct test-file importer
    (``test_trade_bot_exit_rollback_and_warmup.py``): ``_FakeClerk`` below
    constructs real instances of it at runtime, so the class has to live
    wherever ``_FakeClerk`` lives -- moving it to the test module would make
    this shared doubles module import from a leaf test file (issue #1810
    deviation; see the PR description for the full rationale).
    """

    def __init__(self, order_ref: str, *, state: str = "submitted") -> None:
        self.state = type("EffectState", (), {"value": state})()
        self.child_order_refs = (order_ref,)


class _FakeClerk:
    """Minimal active-SQLite Clerk double capturing semantic operations."""

    authority_kind = "sqlite"
    account_id = "PA-TEST"

    def __init__(
        self,
        *,
        should_raise: Exception | None = None,
        effect_state: str = "submitted",
        repository: ClerkSqliteRepository | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.stop_cancellations: list[str] = []
        self.registered_runs: list[str] = []
        self.stopped_runs: list[str] = []
        self._should_raise = should_raise
        self._effect_state = effect_state
        self.repository = repository or _TestDecisionReceiptRepository()
        self.active_runs: dict[str, str] = {}
        self.known_runs: set[tuple[str, str]] = set()

    async def register_strategy_run(self, binding: BrokerBotBinding) -> None:
        self.registered_runs.append(binding.run_id)
        self.active_runs[binding.strategy_instance_id] = binding.run_id
        self.known_runs.add((binding.strategy_instance_id, binding.run_id))
        if isinstance(self.repository, ClerkSqliteRepository):
            submit_start_run(
                self.repository,
                account_id="PA-TEST",
                strategy_instance_id=binding.strategy_instance_id,
                lifecycle_run_id=binding.run_id,
            )

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> None:
        del reason
        self.stopped_runs.append(run_id)
        if self.active_runs.get(strategy_instance_id) == run_id:
            self.active_runs.pop(strategy_instance_id)
        if isinstance(self.repository, ClerkSqliteRepository):
            submit_stop_run(
                self.repository,
                account_id="PA-TEST",
                strategy_instance_id=strategy_instance_id,
                lifecycle_run_id=run_id,
            )

    def lifecycle_snapshot(
        self,
        strategy_instance_id: str,
        expected_run_id: str | None,
    ) -> AlpacaLifecycleAuthoritySnapshot:
        active_run_id = self.active_runs.get(strategy_instance_id)
        return AlpacaLifecycleAuthoritySnapshot(
            strategy_instance_exists=(
                active_run_id is not None
                or any(candidate[0] == strategy_instance_id for candidate in self.known_runs)
            ),
            active_run_id=active_run_id,
            retired_at_ms=None,
            expected_run_state=(
                None
                if expected_run_id is None
                else (
                    "ACTIVE"
                    if active_run_id == expected_run_id
                    else (
                        "STOPPED"
                        if (strategy_instance_id, expected_run_id) in self.known_runs
                        else "MISSING"
                    )
                )
            ),
            control_revision=len(self.known_runs) + len(self.stopped_runs),
        )

    def lifecycle_recovery_candidates(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.active_runs.items()))

    async def cancel_working_entries_for_instance(self, strategy_instance_id: str) -> tuple[()]:
        """Test-double boundary for Clerk-owned STOP custody."""
        self.stop_cancellations.append(strategy_instance_id)
        return ()

    async def execute_for_instance(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose,
        action_plan,
        quantity: int,
        use_rth: bool = True,
        capability_account_id: str | None = None,
        decision_evidence=None,
    ) -> _FakeEffectResult:
        del use_rth, capability_account_id
        if self._should_raise is not None:
            raise self._should_raise

        call = {
            "strategy_instance_id": strategy_instance_id,
            "run_id": run_id,
            "decision_id": decision_id,
            "purpose": purpose.value,
            "quantity": quantity,
            "action_plan": action_plan,
        }
        self.calls.append(call)
        if decision_evidence is not None and isinstance(
            self.repository, ClerkSqliteRepository
        ):
            rejected = self._effect_state == "rejected"
            facts = {
                "bar_ref": decision_evidence.bar_ref,
                "decision_id": decision_evidence.evaluation_id,
                "evaluation_id": decision_evidence.evaluation_id,
                "run_id": run_id,
                "reason_code": (
                    "CLERK_ADMISSION_REJECTED"
                    if rejected
                    else decision_evidence.reason_code
                ),
                "retention_class": (
                    "protected_refusal" if rejected else "protected_effect"
                ),
            }
            if rejected:
                facts["refusal_reason"] = "The test Clerk rejected this strategy submission."
            self.repository.append_decision_receipt(
                strategy_instance_id=strategy_instance_id,
                outcome=("blocked" if rejected else decision_evidence.outcome),
                symbol=decision_evidence.symbol,
                intent_id=decision_evidence.evaluation_id,
                order_ref=None,
                observed_at_ms=decision_evidence.observed_at_ms,
                facts_json=json.dumps(facts, sort_keys=True, separators=(",", ":")),
            )
        return _FakeEffectResult(
            order_ref=f"learn-ai/{strategy_instance_id}/v1:fake{len(self.calls):02d}",
            state=self._effect_state,
        )

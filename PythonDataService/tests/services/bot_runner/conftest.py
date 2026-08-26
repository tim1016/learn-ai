"""Shared fixtures, test doubles, and constants for the bot_runner test
package (split of the former ``tests/services/test_bot_runner.py`` per
issue #1737 -- see that issue for the seam rationale).

Originally: tests for app.services.bot_runner -- the in-container bot task
registry. Covers issue #1260 acceptance criteria:
- deploy -> running asyncio task + durable ON_DUTY evidence readable without
  the runner (raw artifact files).
- stop -> durable STOPPED desired-state, clean task exit, OFF_DUTY evidence.
- simulated crash -> typed durable crash evidence distinct from a clean stop;
  the registry reaps and never renders the bot healthy.
- daemon-free by construction (no daemon-client / subprocess imports).
- container-side artifact paths only (everything under the tmp_path root).
- broker-tagged bindings.
- restart-intensity guard reusing the canonical policy semantics.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_runner as bot_runner
import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
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
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderLeg,
)
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.schemas.action_plan import ActionPlan
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.bot_binding_repository import (
    BrokerBotBinding,
)
from app.services.bot_lifecycle_projection import AlpacaLifecycleAuthoritySnapshot
from app.services.bot_runner import (
    BotTaskRegistry,
)
from app.services.market_liveness import compose_market_liveness
from app.utils.timestamps import now_ms_utc

_SID = "alpaca-skeleton-1"
_T0 = 1_700_000_000_000
_RTH_MS = 1_700_060_400_000
_CLOSED_MS = 1_700_096_400_000


def _tradable_market_liveness(symbol: str, observed_at_ms: int):
    return compose_market_liveness(
        symbol,
        now_ms=observed_at_ms,
        market_clock=MarketClockLivenessEvidence(
            state="OPEN",
            source="test.clock",
            observed_at_ms=observed_at_ms,
            vendor_timestamp_ms=observed_at_ms,
        ),
        connected=True,
        connection_changed_at_ms=observed_at_ms,
        symbol_status=SymbolTradingStatusEvidence(
            symbol=symbol,
            state="TRADABLE",
            source="test.symbol-status",
            observed_at_ms=observed_at_ms,
            source_timestamp_ms=observed_at_ms,
        ),
    )


def _verified_validation_fact(_binding: object, observed_at_ms: int) -> StrategyValidationAdmissionFact:
    """Keep runner tests focused on task/custody behavior, not manifest fixtures."""
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="deployment_validation",
        evidence_status="accepted",
        event_id="test-validation-event",
        evidence_snapshot_sha256="a" * 64,
        verified_at_ms=observed_at_ms,
        explanation="Test validation evidence is current.",
    )


@pytest.fixture(autouse=True)
def _fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_runner, "market_liveness_fact", _tradable_market_liveness)
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        _tradable_market_liveness,
    )
    # #1671: the Clerk's own submission-boundary recheck (runtime.py) reads
    # this module's import of the same name — a separate binding from
    # bot_trade_strategy's, so it needs its own patch or it falls through to
    # the real (unconfigured, fail-closed) store and every ENTER is rejected.
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _tradable_market_liveness)
    monkeypatch.setattr(bot_runner, "current_strategy_validation_fact", _verified_validation_fact)


def _bar(start_ms: int, symbol: str = "SPY") -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal("400.5"),
        volume=100,
        fetched_at_ms=start_ms + 500,
        feed_id="ibkr",
        session_phase="RTH",
    )


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


class _OrderingClerk(_CustodyClerk):
    def __init__(self, proof: InstanceCustodyProof) -> None:
        super().__init__(proof)
        self.registration_saw_bot_task = False
        self.stop_committed = asyncio.Event()
        self.fail_stop = False

    async def register_strategy_run(self, binding: BrokerBotBinding) -> None:
        self.registration_saw_bot_task = any(
            task.get_name() == f"bot:{binding.strategy_instance_id}"
            for task in asyncio.all_tasks()
        )
        await super().register_strategy_run(binding)

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> None:
        if self.fail_stop:
            raise RuntimeError("durable STOP failed")
        await super().stop_strategy_run(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            reason=reason,
        )
        self.stop_committed.set()


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


def _custody_proof(
    *,
    exposure: dict[str, float],
    verdict: str = "clean",
    freeze: AccountFreezeState | None = None,
) -> InstanceCustodyProof:
    return InstanceCustodyProof(
        account_id="paper-account",
        strategy_instance_id=_SID,
        reconciliation_verdict=verdict,  # type: ignore[arg-type]
        freeze=freeze or AccountFreezeState(),
        exposure=exposure,
        observed_at_ms=_T0,
    )


@pytest.fixture(autouse=True)
def _default_lifecycle_clerk() -> None:
    """Give runner unit tests a local duty-authority Adapter by default."""

    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
    yield
    set_alpaca_clerk(None)


def _flat_custody_snapshot(
    sid: str,
    *,
    observed_at_ms: int | None = None,
) -> ClerkCustodySnapshot:
    observed_at_ms = observed_at_ms or now_ms_utc()
    zero = CustodyCountFact(state="zero", count=0)
    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id="paper-account",
        strategy_instance_id=sid,
        clerk_generation="test-clerk",
        journal_sequence=0,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=observed_at_ms,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=zero,
        pending_orders=zero,
        terminal_orders=zero,
        unresolved_effects=zero,
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=("test-clerk:0",),
        observed_at_ms=observed_at_ms,
    )


@asynccontextmanager
async def _flat_start_guard(sid: str):
    yield _flat_custody_snapshot(sid)


def _registry(
    tmp_path: Path,
    feed: _FakeFeed | None,
    *,
    policy: RestartIntensityPolicy | None = None,
    now_ms: Callable[[], int] = now_ms_utc,
    start_custody_guard: (
        Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]] | None
    ) = None,
) -> BotTaskRegistry:
    return BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=policy or RestartIntensityPolicy(threshold=100),
        # Boot recovery has its own suite (test_boot_recovery.py).
        boot_recovery_required=False,
        start_custody_guard=start_custody_guard or _flat_start_guard,
        now_ms=now_ms,
        market_liveness=_tradable_market_liveness,
    )


def _lifecycle_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "lifecycle_state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _desired_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "desired_state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _strategy_instance_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "strategy_instance.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _current_run_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "current_run.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _run_json(tmp_path: Path, run_id: str, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "runs" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


# 2024-01-02 is a regular NYSE trading day (Tuesday after New Year's).
# All bar timestamps below are int64 ms UTC (temporal-rigor rule).
#
# ET = EST on 2024-01-02 (UTC-5):
#   session_open  = 09:30 ET = 14:30 UTC = 1_704_205_800_000 ms
#   session_close = 16:00 ET = 21:00 UTC = 1_704_229_200_000 ms
#   window_start  = open  + 15min = 1_704_206_700_000 ms  (09:45 ET)
#   window_end    = close - 15min = 1_704_228_300_000 ms  (15:45 ET)
#
# Verified against the canonical calendar module (session_window_for_date).
# bar.end_ms is the bar-close boundary per MarketDataBar semantics.

_SESSION_OPEN_MS = 1_704_205_800_000  # 2024-01-02 09:30 ET (EST = UTC-5)
_SESSION_CLOSE_MS = 1_704_229_200_000  # 2024-01-02 16:00 ET
_WIN_START_MS = _SESSION_OPEN_MS + 15 * 60 * 1_000  # 09:45 ET = 1_704_206_700_000
_WIN_END_MS = _SESSION_CLOSE_MS - 15 * 60 * 1_000  # 15:45 ET = 1_704_228_300_000


def _trade_bar(
    end_ms: int,
    *,
    open_price: str = "400.00",
    close_price: str = "401.00",
    symbol: str = "SPY",
) -> MarketDataBar:
    """A single 1-minute bar whose end_ms (bar-close) falls at a specific instant."""
    return MarketDataBar(
        symbol=symbol,
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=Decimal(open_price),
        high=Decimal(close_price),
        low=Decimal(open_price),
        close=Decimal(close_price),
        volume=500,
        fetched_at_ms=end_ms + 100,
        feed_id="fake",
        session_phase="RTH",
    )


def _red_bar(end_ms: int, symbol: str = "SPY") -> MarketDataBar:
    """A bar where close < open (red candle — no green streak contribution)."""
    return _trade_bar(end_ms, open_price="401.00", close_price="400.00", symbol=symbol)


def _green_bar(end_ms: int, symbol: str = "SPY") -> MarketDataBar:
    """A bar where close > open (green candle)."""
    return _trade_bar(end_ms, open_price="400.00", close_price="401.00", symbol=symbol)


class _FakeEffectResult:
    """Minimal Clerk-authored effect receipt used by the strategy adapter."""

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


def _install_fake_clerk(monkeypatch: pytest.MonkeyPatch, clerk: _FakeClerk) -> None:
    """Patch the process-level Alpaca clerk for the duration of a test."""
    del monkeypatch
    set_alpaca_clerk(clerk)


_EMA_FIRST_ENTER_MS = 1_770_389_100_000
_EMA_FIRST_EXIT_MS = 1_770_393_600_000


def _ema_parity_bars_through_first_exit() -> list[MarketDataBar]:
    """Load the retained LEAN input stream through its first EMA round-trip."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures/golden/cross-engine-studies/cells"
        / "SPY_W3mo_2026-02-02_to_2026-04-30/lean/observations.csv"
    )
    bars: list[MarketDataBar] = []
    with fixture.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                MarketDataBar(
                    symbol="SPY",
                    start_ms=end_ms - 60_000,
                    end_ms=end_ms,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(Decimal(row["volume"])),
                    fetched_at_ms=end_ms + 100,
                    feed_id="lean-golden",
                    session_phase="RTH",
                )
            )
            if end_ms > _EMA_FIRST_EXIT_MS:
                break
    return bars


def _ema_signal_evaluation_id(bar_close_ms: int, *, symbol: str = "SPY") -> str:
    """Independently recompute the Signal Program's documented evaluation
    identity (see the Formula note in ``app/engine/strategy/signal_program.py``:
    SHA-256 of the canonical JSON of program version, settings, and bar-close
    clock) from the real registered strategy -- not a hand-typed guess at the
    hash bytes. Proves ``decision_id`` really is the deterministic per-bar
    Signal Program identity the PRD requires (``decision_id = evaluation_id``,
    issue #1728 / PRD section 16), rather than merely echoing whatever the
    current build happens to emit.
    """
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_factory is not None
    params = registration.param_schema(symbol=symbol)
    program = registration.signal_program_factory(params)
    payload = {
        "program_key": program.session.program_key,
        "program_version": program.session.program_version,
        "settings": program.strategy.signal_program_settings(),
        "bar_close_ms": bar_close_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

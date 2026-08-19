"""Tests for app.services.bot_runner — the in-container bot task registry.

Covers issue #1260 acceptance criteria:
- deploy → running asyncio task + durable ON_DUTY evidence readable without
  the runner (raw artifact files).
- stop → durable STOPPED desired-state, clean task exit, OFF_DUTY evidence.
- simulated crash → typed durable crash evidence distinct from a clean stop;
  the registry reaps and never renders the bot healthy.
- daemon-free by construction (no daemon-client / subprocess imports).
- container-side artifact paths only (everything under the tmp_path root).
- broker-tagged bindings.
- restart-intensity guard reusing the canonical policy semantics.
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_runner as bot_runner
import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.active_protocol import ClerkAdmissionSnapshotStaleError
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
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderLeg,
)
from app.engine.execution.portfolio import Portfolio
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.engine.live.bot_lifecycle_state import BotDutyOutcome, BotLifecyclePhase
from app.engine.live.desired_state import DesiredState
from app.engine.strategy.base import StrategyContext
from app.marketdata.feed import FeedHealth, MarketDataBar, MarketDataFeedError
from app.schemas.action_plan import ActionPlan
from app.schemas.broker_bots import AlpacaPaperStrategyKey, BotProcessFact
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    RunOutcomeConflictError,
    alpaca_v1_action_plan,
)
from app.services.bot_clerk_lifecycle import commit_stop_before_task_cancel
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_lifecycle_projection import AlpacaLifecycleAuthoritySnapshot
from app.services.bot_registry_projection import read_dry_run_activity
from app.services.bot_run_evidence import PROVISIONAL_STOP_REASON_CODE
from app.services.bot_runner import (
    BootRecoveryIncompleteError,
    BotAlreadyRunningError,
    BotTaskRegistry,
    CarryoverPolicyRefusedError,
    InvalidStrategyInstanceIdError,
    MarketDataFeedUnavailableError,
    RecoveryUncertainError,
    RestartIntensityRefusedError,
    RunAdmissionRefusedError,
    UnknownBotError,
)
from app.services.bot_runner_errors import InvalidRunHistoryCursorError
from app.services.bot_runtime import PauseAwareFeed
from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys
from app.services.market_liveness import compose_market_liveness, unknown_market_liveness
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


def test_every_admitted_alpaca_paper_strategy_has_a_runtime() -> None:
    assert supported_alpaca_paper_strategy_keys() == frozenset(AlpacaPaperStrategyKey)


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


def test_live_market_bar_translates_to_numeric_engine_timestamps() -> None:
    from app.services.bot_trade_strategy import _engine_bar

    source = _bar(_RTH_MS)
    engine_bar = _engine_bar(source)

    assert (engine_bar.start_ms, engine_bar.end_ms) == (source.start_ms, source.end_ms)
    assert not any(isinstance(value, datetime) for value in vars(engine_bar).values())


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

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._bars[-1].start_ms if self._bars else None,
            reason="",
            active_subscription_count=0,
            observed_at_ms=self._observed_at_ms or now_ms_utc(),
        )


class _StaleFeed(_FakeFeed):
    def health(self, symbol: str | None = None) -> FeedHealth:
        return super().health(symbol).model_copy(update={"stale": True, "reason": "No recent closed bar."})


class _ActiveStaleFeed(_StaleFeed):
    def health(self, symbol: str | None = None) -> FeedHealth:
        return super().health(symbol).model_copy(update={"active_subscription_count": 1})


class _CancellationSuppressingFeed(_FakeFeed):
    """Simulates a strategy coroutine whose task never terminates on cancel.

    ``stream_bars`` swallows exactly one ``CancelledError`` at its
    ``await`` suspension point and keeps looping, so ``task.cancel()``
    never actually finishes the task — reproducing the P0-3 audit
    finding without a real external dependency.
    """

    def __init__(self, bars: list[MarketDataBar]) -> None:
        super().__init__(bars, mode="hold")
        self.cancellation_suppressed = False

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.cancellation_suppressed:
                    raise
                self.cancellation_suppressed = True


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

    async def unresolved_effect_count(self) -> int:
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
    ) -> EffectOperationReceipt:
        del strategy_instance_id, run_id, decision_id, purpose, action_plan, quantity, use_rth
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


class _StopOrderingFeed(_FakeFeed):
    def __init__(self, clerk: _OrderingClerk) -> None:
        super().__init__([], mode="hold")
        self._clerk = clerk
        self.cancelled_after_stop = False

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        del symbol, use_rth
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled_after_stop = self._clerk.stop_committed.is_set()
            raise
        if False:
            yield


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


@asynccontextmanager
async def _fixed_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_T0)


@asynccontextmanager
async def _rth_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_RTH_MS)


@asynccontextmanager
async def _closed_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_CLOSED_MS)


@asynccontextmanager
async def _changing_start_guard(_sid: str):
    raise ClerkAdmissionSnapshotStaleError("test evidence race")
    yield  # pragma: no cover - required to type this as an async context manager


def _registry(
    tmp_path: Path,
    feed: _FakeFeed | None,
    *,
    policy: RestartIntensityPolicy | None = None,
    carryover_allowed: bool = False,
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
        carryover_allowed=carryover_allowed,
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


# ── deploy: running task + durable ON_DUTY evidence ───────────────────


@pytest.mark.asyncio
async def test_deploy_produces_running_task_and_durable_on_duty_evidence(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)

    view = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert view.running is True
    assert view.phase == "ON_DUTY"
    assert view.desired_state == "RUNNING"
    assert view.broker == "alpaca"
    assert view.active_run_id is not None

    # Durable evidence readable WITHOUT the runner (raw files).
    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["phase"] == "ON_DUTY"
    assert lifecycle["active_run_id"] == view.active_run_id
    desired = _desired_json(tmp_path)
    assert desired["desired_state"] == "RUNNING"
    instance = _strategy_instance_json(tmp_path)
    assert instance["broker"] == "alpaca"
    assert instance["symbol"] == "SPY"
    assert instance["mode"] == "log_only"
    assert instance["quantity"] == 1
    current = _current_run_json(tmp_path)
    assert current["run_id"] == view.active_run_id
    assert isinstance(_run_json(tmp_path, current["run_id"])["started_at_ms"], int)

    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_process_fact_requires_current_registry_liveness_proof(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    view = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    running = registry.process_fact("alpaca", _SID)

    assert running.strategy_instance_id == _SID
    assert running.run_id == view.active_run_id
    assert running.process_identity == f"in-process-task:{view.active_run_id}"
    assert running.state == "RUNNING"
    assert running.registry_generation
    assert running.observed_at_ms > 0

    replacement_registry = _registry(tmp_path, feed)
    unknown = replacement_registry.process_fact("alpaca", _SID)

    assert unknown.run_id == view.active_run_id
    assert unknown.process_identity is None
    assert unknown.state == "UNKNOWN"
    assert unknown.registry_generation != running.registry_generation

    await registry.stop("alpaca", _SID)
    exited = replacement_registry.process_fact("alpaca", _SID)
    assert exited.run_id == view.active_run_id
    assert exited.process_identity is None
    assert exited.state == "EXITED"


def test_process_fact_rejects_unemittable_starting_state() -> None:
    with pytest.raises(ValidationError):
        BotProcessFact(
            strategy_instance_id=_SID,
            run_id="run-1",
            process_identity="in-process-task:run-1",
            state="STARTING",
            registry_generation="registry-1",
            observed_at_ms=_T0,
        )


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_the_same_admission_policy(
    tmp_path: Path,
) -> None:
    feed = _ActiveStaleFeed([], mode="hold", observed_at_ms=_RTH_MS)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: _RTH_MS,
        boot_recovery_required=False,
        start_custody_guard=_rth_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "MARKET_DATA_STALE"
    with pytest.raises(MarketDataFeedUnavailableError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code
    assert not (tmp_path / "live_state" / _SID / "broker_binding.json").exists()


@pytest.mark.asyncio
async def test_start_allows_idle_connected_feed_to_establish_subscription(
    tmp_path: Path,
) -> None:
    feed = _StaleFeed([], mode="hold", observed_at_ms=_RTH_MS)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: _RTH_MS,
        boot_recovery_required=False,
        start_custody_guard=_rth_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is True
    started = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    assert started.running is True
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_start_does_not_call_expected_rth_silence_a_stalled_feed(
    tmp_path: Path,
) -> None:
    feed = _ActiveStaleFeed([], mode="hold", observed_at_ms=_CLOSED_MS)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: _CLOSED_MS,
        boot_recovery_required=False,
        start_custody_guard=_closed_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is True


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_boot_recovery_refusal(
    tmp_path: Path,
) -> None:
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _FakeFeed([], mode="hold"),
        restart_policy=RestartIntensityPolicy(threshold=100),
        start_custody_guard=_flat_start_guard,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "BOOT_RECOVERY_INCOMPLETE"
    with pytest.raises(BootRecoveryIncompleteError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_unresolved_recovery_refusal(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    async def no_op() -> None:
        return None

    async def one_unresolved_intent() -> int:
        return 1

    await registry.run_boot_recovery(
        recover=no_op,
        reconcile=no_op,
        unresolved_intents_probe=one_unresolved_intent,
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "RECOVERY_UNCERTAIN"
    with pytest.raises(RecoveryUncertainError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code


@pytest.mark.asyncio
async def test_start_preview_and_execution_share_restart_intensity_refusal(
    tmp_path: Path,
) -> None:
    registry = _registry(
        tmp_path,
        _FakeFeed([], mode="hold"),
        policy=RestartIntensityPolicy(threshold=1, window_ms=300_000),
    )

    preview = await registry.preview_start_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert preview.allowed is False
    assert preview.reason_code == "RESTART_INTENSITY_EXCEEDED"
    with pytest.raises(RestartIntensityRefusedError) as refused:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
        )
    assert refused.value.admission_decision is not None
    assert refused.value.admission_decision.reason_code == preview.reason_code


@pytest.mark.asyncio
async def test_start_refuses_cleanly_when_clerk_evidence_never_stabilizes(
    tmp_path: Path,
) -> None:
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _FakeFeed([], mode="hold"),
        restart_policy=RestartIntensityPolicy(threshold=100),
        boot_recovery_required=False,
        start_custody_guard=_changing_start_guard,
    )

    with pytest.raises(RunAdmissionRefusedError, match="stable Clerk custody"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert not (tmp_path / "live_state" / _SID / "broker_binding.json").exists()


@pytest.mark.asyncio
async def test_start_timestamps_activation_after_custody_reconciliation(
    tmp_path: Path,
) -> None:
    clock = {"now": _T0}

    @asynccontextmanager
    async def delayed_custody_guard(sid: str):
        clock["now"] = _T0 + 10_000
        yield _flat_custody_snapshot(sid, observed_at_ms=clock["now"])

    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _FakeFeed([], mode="hold", observed_at_ms=_T0 + 10_000),
        restart_policy=RestartIntensityPolicy(threshold=100),
        now_ms=lambda: clock["now"],
        boot_recovery_required=False,
        start_custody_guard=delayed_custody_guard,
    )

    started = await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    assert started.admission.evaluated_at_ms == _T0 + 10_000
    run_id = _current_run_json(tmp_path)["run_id"]
    assert _run_json(tmp_path, run_id)["started_at_ms"] == started.admission.evaluated_at_ms
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_deployed_bot_consumes_bars_and_logs_decisions(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="hold")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runner"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await _wait_for(lambda: feed.bars_consumed == 2)
        await registry.stop("alpaca", _SID)

    decisions = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert len(decisions) == 2
    assert decisions[0].decision == "HOLD"
    assert decisions[0].bar_start_ms == _T0


@pytest.mark.asyncio
async def test_deploy_while_running_is_refused(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    with pytest.raises(BotAlreadyRunningError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_deploy_after_stop_with_changed_configuration_is_refused(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    original_binding_bytes = (
        tmp_path / "live_state" / _SID / "strategy_instance.json"
    ).read_bytes()

    await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    with pytest.raises(RunAdmissionRefusedError) as excinfo:
        await registry.deploy_with_admission(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="ema_crossover_signal",
            symbol="QQQ",
        )
    assert excinfo.value.admission_decision.reason_code == "STRATEGY_INSTANCE_ALREADY_EXISTS"

    unchanged_binding_bytes = (
        tmp_path / "live_state" / _SID / "strategy_instance.json"
    ).read_bytes()
    assert unchanged_binding_bytes == original_binding_bytes


@pytest.mark.asyncio
async def test_deploy_without_feed_is_typed_503(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)

    with pytest.raises(MarketDataFeedUnavailableError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


@pytest.mark.asyncio
async def test_deploy_rejects_unsafe_strategy_instance_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(InvalidStrategyInstanceIdError):
        await registry.deploy(broker="alpaca", strategy_instance_id="../escape", symbol="SPY")


# ── stop: Button-Rule exit with durable intent first ──────────────────


@pytest.mark.asyncio
async def test_stop_writes_durable_intent_and_off_duty_evidence(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    view = await registry.stop("alpaca", _SID, reason="drill")

    assert view.running is False
    assert view.phase == "OFF_DUTY"
    assert view.desired_state == "STOPPED"
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "STOPPED"
    assert view.duty_outcome.reason_code == "OPERATOR_STOP"

    # Raw artifacts agree without the runner.
    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"
    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["phase"] == "OFF_DUTY"
    assert lifecycle["active_run_id"] is None
    assert lifecycle["duty_outcome"]["kind"] == "STOPPED"


@pytest.mark.asyncio
async def test_stop_of_unknown_bot_is_404(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(UnknownBotError):
        await registry.stop("alpaca", "never-deployed")


@pytest.mark.asyncio
async def test_stop_does_not_finalize_or_reap_a_task_that_survives_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.bot_runner._STOP_TIMEOUT_S", 0.05)
    feed = _CancellationSuppressingFeed(bars=[])
    registry = _registry(tmp_path, feed=feed)
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )

    status = await registry.stop(broker="alpaca", strategy_instance_id=_SID)

    assert feed.cancellation_suppressed is True
    # The task must still be tracked — not reaped — while it's alive.
    assert _SID in registry._bots
    assert registry._bots[_SID].task.done() is False
    # status() must honestly report the bot as still running, since the
    # task is still alive; desired_state carries the STOPPED intent.
    assert status.running is True
    assert registry.desired_state(_SID) is DesiredState.STOPPED


# ── desired_state: public accessor for durable operator intent ────────


@pytest.mark.asyncio
async def test_desired_state_reports_durable_intent(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy_with_admission(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        symbol="SPY",
        mode="log_only",
    )
    assert registry.desired_state(_SID) is DesiredState.RUNNING

    await registry.stop(broker="alpaca", strategy_instance_id=_SID)
    assert registry.desired_state(_SID) is DesiredState.STOPPED


# ── crash: typed durable evidence distinct from a clean stop ──────────


@pytest.mark.asyncio
async def test_crash_records_typed_evidence_and_reaps(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=RuntimeError("boom"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.running is False  # reaped — never rendered healthy
    assert view.phase == "OFF_DUTY"
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "RuntimeError"
    # A terminal crash is fail-closed.  Leaving RUNNING behind strands the
    # off-duty bot because the panel's proof-gated Start path requires STOPPED.
    assert view.desired_state == "STOPPED"
    assert _desired_json(tmp_path)["desired_state"] == "STOPPED"

    lifecycle = _lifecycle_json(tmp_path)
    assert lifecycle["duty_outcome"]["kind"] == "CRASHED"


@pytest.mark.asyncio
async def test_feed_death_records_feed_death_crash(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="crash", error=MarketDataFeedError("gateway lost"))
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "FEED_DEATH"
    assert view.desired_state == "STOPPED"


@pytest.mark.asyncio
async def test_kill_without_stop_intent_is_exited_unverified(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    managed_task = registry._bots[_SID].task
    managed_task.cancel()  # a kill: no stop intent recorded
    await asyncio.wait({managed_task})
    await _wait_for(lambda: _SID not in registry._bots)

    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
    assert view.duty_outcome.reason_code == "CANCELLED_WITHOUT_STOP_INTENT"
    assert view.desired_state == "STOPPED"


@pytest.mark.asyncio
async def test_bar_stream_end_is_exited_unverified(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="finite")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
    assert view.duty_outcome.reason_code == "BAR_STREAM_ENDED"
    assert view.desired_state == "STOPPED"


# ── restart intensity (canonical policy semantics, per bot) ───────────


@pytest.mark.asyncio
async def test_restart_intensity_refuses_thresholdth_start(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    policy = RestartIntensityPolicy(threshold=3, window_ms=300_000)
    registry = _registry(tmp_path, feed, policy=policy)
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))

    try:
        # Starts 1 and 2 pass (projected 1, 2 < 3); start 3 projects to the
        # threshold and is refused — mirrors project_restart_intensity_gate.
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await registry.stop("alpaca", _SID)
        await registry.resume_existing("alpaca", _SID)
        await registry.stop("alpaca", _SID)

        with pytest.raises(RestartIntensityRefusedError):
            await registry.resume_existing("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_restart_intensity_window_expiry_allows_restart(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold", observed_at_ms=_T0)
    policy = RestartIntensityPolicy(threshold=2, window_ms=1_000)
    clock = {"now": _T0}
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=policy,
        now_ms=lambda: clock["now"],
        boot_recovery_required=False,
        start_custody_guard=_fixed_start_guard,
    )
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))

    try:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await registry.stop("alpaca", _SID)
        with pytest.raises(RestartIntensityRefusedError):
            await registry.resume_existing("alpaca", _SID)

        clock["now"] = _T0 + 2_000  # window has passed
        view = await registry.resume_existing("alpaca", _SID)
        assert view.running is True
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


# ── listing and broker tags ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_bots_filters_by_broker_tag(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert [v.strategy_instance_id for v in registry.list_bots("alpaca")] == [_SID]
    assert registry.list_bots("ibkr") == []

    await registry.stop("alpaca", _SID)
    # Stopped bots remain on the roster (artifact-derived), just not running.
    listed = registry.list_bots("alpaca")
    assert len(listed) == 1
    assert listed[0].running is False


@pytest.mark.asyncio
async def test_runner_refuses_ibkr_binding_before_any_duty_artifact(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(RunAdmissionRefusedError, match="Alpaca"):
        await registry.deploy(
            broker="ibkr",
            strategy_instance_id=_SID,
            symbol="SPY",
        )

    assert not (tmp_path / "live_state" / _SID).exists()


@pytest.mark.asyncio
async def test_version_one_alpaca_binding_is_read_without_rewriting_audit_artifact(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)

    binding_path = tmp_path / "live_state" / _SID / "broker_binding.json"
    legacy = registry.binding_for_control("alpaca", _SID).model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy.pop("action_plan")
    original = json.dumps(legacy, separators=(",", ":"), sort_keys=True)
    instance_dir = tmp_path / "live_state" / _SID
    run_id = _current_run_json(tmp_path)["run_id"]
    (instance_dir / "strategy_instance.json").unlink()
    (instance_dir / "current_run.json").unlink()
    (instance_dir / "runs" / f"{run_id}.json").unlink()
    (instance_dir / "runs").rmdir()
    binding_path.write_text(original, encoding="utf-8")

    restarted = _registry(tmp_path, feed)
    listed = restarted.list_bots("alpaca")
    migrated = restarted.binding_for_control("alpaca", _SID)

    assert [view.strategy_instance_id for view in listed] == [_SID]
    assert migrated.schema_version == 2
    assert migrated.action_plan.on_enter[0].instrument.underlying == "SPY"
    assert migrated.action_plan.on_exit[0].entry_leg_id == "primary"
    assert binding_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_status_for_wrong_broker_is_404(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    try:
        with pytest.raises(UnknownBotError):
            registry.status("ibkr", _SID)
    finally:
        await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_resume_existing_creates_new_run_and_preserves_action_plan(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
        quantity=3,
    )
    original = registry.binding_for_control("alpaca", _SID)
    original_run_path = (
        tmp_path / "live_state" / _SID / "runs" / f"{original.run_id}.json"
    )
    original_run_bytes = original_run_path.read_bytes()
    await registry.stop("alpaca", _SID)
    resumed = await registry.resume_existing("alpaca", _SID)
    rebound = registry.binding_for_control("alpaca", _SID)
    current = registry.current_run("alpaca", _SID)
    history = registry.run_history("alpaca", _SID, cursor=None, limit=1)

    assert resumed.running is True
    assert resumed.active_run_id != deployed.active_run_id
    assert rebound.run_id == resumed.active_run_id
    assert rebound.mode == "log_only"
    assert rebound.quantity == 3
    assert rebound.action_plan == original.action_plan
    assert original_run_path.read_bytes() == original_run_bytes
    assert sorted(
        path.stem for path in (tmp_path / "live_state" / _SID / "runs").glob("*.json")
    ) == sorted([original.run_id, rebound.run_id])
    assert current.run_id == rebound.run_id
    assert current.is_current is True
    assert current.process is not None
    assert current.process.state == "RUNNING"
    assert [run.run_id for run in history.runs] == [original.run_id]
    assert history.runs[0].is_current is False
    assert history.runs[0].process is None
    assert history.runs[0].terminal_outcome is not None
    assert history.runs[0].terminal_outcome.kind == "STOPPED"
    assert history.next_cursor is None
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_resume_preserves_prior_outcome_before_current_pointer_advances(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    prior_run_id = registry.binding_for_control("alpaca", _SID).run_id
    await registry.stop("alpaca", _SID)

    original_record_launch = registry._bindings.record_launch

    def crash_after_current_pointer_write(*args: object, **kwargs: object) -> None:
        original_record_launch(*args, **kwargs)
        raise RuntimeError("injected crash after current run pointer write")

    monkeypatch.setattr(registry._bindings, "record_launch", crash_after_current_pointer_write)
    with pytest.raises(RuntimeError, match="injected crash"):
        await registry.resume_existing("alpaca", _SID)

    restarted_registry = _registry(tmp_path, feed)
    history = restarted_registry.run_history("alpaca", _SID, cursor=None, limit=1)

    assert history.runs[0].run_id == prior_run_id
    assert history.runs[0].terminal_outcome is not None
    assert history.runs[0].terminal_outcome.reason_code == "OPERATOR_STOP"


@pytest.mark.asyncio
async def test_resume_does_not_preserve_provisional_stop_outcome(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    binding = registry.binding_for_control("alpaca", _SID)
    managed = registry._bots[_SID]
    managed.finalized = True
    managed.task.cancel()
    await asyncio.wait({managed.task})
    registry._terminal.reap(_SID, binding.run_id)
    registry._desired_repo(_SID).set(
        DesiredState.STOPPED,
        updated_by="test",
        now_ms=_T0,
        reason="operator_stop",
    )
    await commit_stop_before_task_cancel(binding, reason="operator_stop")
    registry._run_evidence.record_terminal(
        _SID,
        BotDutyOutcome(
            kind="STOPPED",
            reason_code=PROVISIONAL_STOP_REASON_CODE,
            recorded_at_ms=_T0,
            run_id=binding.run_id,
        ),
        updated_by="test",
        reason=PROVISIONAL_STOP_REASON_CODE,
        expected_active_run_id=binding.run_id,
        persist_receipt=False,
    )

    await registry.resume_existing("alpaca", _SID)
    history = registry.run_history("alpaca", _SID, cursor=None, limit=1)

    assert history.runs[0].run_id == binding.run_id
    assert history.runs[0].terminal_outcome is None
    assert not (
        tmp_path / "live_state" / _SID / "run_outcomes" / f"{binding.run_id}.json"
    ).exists()
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_superseded_terminal_projection_keeps_the_run_receipt(tmp_path: Path) -> None:
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    binding = registry.binding_for_control("alpaca", _SID)
    clerk.active_runs[_SID] = "run-new"
    clerk.known_runs.add((_SID, "run-new"))
    outcome = BotDutyOutcome(
        kind="CRASHED",
        reason_code="PROCESS_CRASHED",
        recorded_at_ms=_T0 + 2,
        run_id=binding.run_id,
    )

    result = registry._run_evidence.record_terminal(
        _SID,
        outcome,
        updated_by="test",
        reason="process.crashed",
        expected_active_run_id=binding.run_id,
    )

    assert result.status == "AUTHORITY_EXPECTATION_SUPERSEDED"
    assert registry._bindings.read_outcome(_SID, binding.run_id) is not None
    lifecycle = registry._lifecycle_repo(_SID).read()
    assert lifecycle is not None
    assert lifecycle.phase is BotLifecyclePhase.ON_DUTY
    assert lifecycle.active_run_id == "run-new"
    managed = registry._bots[_SID]
    managed.finalized = True
    managed.task.cancel()
    await asyncio.wait({managed.task})
    set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_conflicting_terminal_outcome_does_not_mutate_lifecycle(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    binding = registry.binding_for_control("alpaca", _SID)
    managed = registry._bots[_SID]
    managed.finalized = True
    managed.task.cancel()
    await asyncio.wait({managed.task})
    registry._terminal.reap(_SID, binding.run_id)
    recorded = BotDutyOutcome(
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=_T0,
        run_id=binding.run_id,
    )
    registry._run_evidence.record_terminal(
        _SID,
        recorded,
        updated_by="test",
        reason="OPERATOR_STOP",
    )
    lifecycle_before = _lifecycle_json(tmp_path)

    with pytest.raises(RunOutcomeConflictError):
        registry._run_evidence.record_terminal(
            _SID,
            recorded.model_copy(update={"kind": "CRASHED", "reason_code": "RuntimeError"}),
            updated_by="test",
            reason="RuntimeError",
        )

    assert _lifecycle_json(tmp_path) == lifecycle_before


@pytest.mark.asyncio
async def test_run_history_pages_previous_runs_without_changing_current_target(
    tmp_path: Path,
) -> None:
    ticks = iter(range(10_000))
    registry = _registry(
        tmp_path,
        _FakeFeed([], mode="hold"),
        now_ms=lambda: now_ms_utc() + next(ticks),
    )
    first = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    await registry.stop("alpaca", _SID)
    second = await registry.resume_existing("alpaca", _SID)
    await registry.stop("alpaca", _SID)
    third = await registry.resume_existing("alpaca", _SID)

    first_page = registry.run_history("alpaca", _SID, cursor=None, limit=1)
    second_page = registry.run_history(
        "alpaca",
        _SID,
        cursor=first_page.next_cursor,
        limit=1,
    )

    assert [run.run_id for run in first_page.runs] == [second.active_run_id]
    assert first_page.next_cursor == second.active_run_id
    assert [run.run_id for run in second_page.runs] == [first.active_run_id]
    assert second_page.next_cursor is None
    assert registry.current_run("alpaca", _SID).run_id == third.active_run_id
    other_sid = "alpaca-skeleton-2"
    await registry.deploy(broker="alpaca", strategy_instance_id=other_sid, symbol="SPY")
    await registry.stop("alpaca", other_sid)
    await registry.resume_existing("alpaca", other_sid)
    await registry.stop("alpaca", other_sid)
    await registry.resume_existing("alpaca", other_sid)
    foreign_page = registry.run_history("alpaca", other_sid, cursor=None, limit=1)

    assert foreign_page.next_cursor is not None
    with pytest.raises(InvalidRunHistoryCursorError):
        registry.run_history("alpaca", _SID, cursor=foreign_page.next_cursor, limit=1)
    await registry.stop("alpaca", other_sid)
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_pause_and_continue_keep_the_same_live_run_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    paused = await registry.pause("alpaca", _SID)
    continued = await registry.continue_paused("alpaca", _SID)

    assert paused.running is True
    assert paused.desired_state == "PAUSED"
    assert paused.active_run_id == deployed.active_run_id
    assert continued.running is True
    assert continued.desired_state == "RUNNING"
    assert continued.active_run_id == deployed.active_run_id
    assert registry.current_run("alpaca", _SID).run_id == deployed.active_run_id
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_continue_refuses_a_live_run_that_is_not_paused(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    with pytest.raises(RunAdmissionRefusedError, match=r"requires.*paused run"):
        await registry.continue_paused("alpaca", _SID)

    assert registry.status("alpaca", _SID).active_run_id == deployed.active_run_id
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_pause_aware_feed_discards_bars_buffered_during_pause() -> None:
    class _QueueFeed:
        feed_id = "queue"

        def __init__(self) -> None:
            self.queue: asyncio.Queue[MarketDataBar] = asyncio.Queue()

        async def stream_bars(self, _symbol: str, *, use_rth: bool = True):
            while True:
                yield await self.queue.get()

    source = _QueueFeed()
    gate = asyncio.Event()
    gate.set()
    clock = [100]
    feed = PauseAwareFeed(source, gate, now_ms=lambda: clock[0])
    stream = feed.stream_bars("SPY")

    await source.queue.put(_bar(0))
    assert (await anext(stream)).end_ms == 60_000

    gate.clear()
    await source.queue.put(_bar(100))
    next_bar = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not next_bar.done()

    clock[0] = 200_000
    gate.set()
    await source.queue.put(_bar(300_000))
    assert (await next_bar).end_ms == 360_000


def test_dry_run_activity_projection_excludes_prior_run_rows(tmp_path: Path) -> None:
    journal = DryRunActivityJournal(tmp_path)
    for run_id, seq in (("run-prior", 1), ("run-current", 2)):
        journal.append(
            DryRunActivity(
                seq=seq,
                strategy_instance_id=_SID,
                run_id=run_id,
                recorded_at_ms=seq * 1_000,
                bar_ref=f"SPY@{seq * 1_000}",
                intent="ENTER",
                order_ref=f"simulated:{run_id}",
                symbol="SPY",
                side="buy",
                quantity=1,
                fill_price=400,
            )
        )
    binding = BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="dry_run",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )

    activity = read_dry_run_activity(binding, tmp_path, limit=8)

    assert [row.run_id for row in activity] == ["run-current"]


@pytest.mark.asyncio
async def test_approved_carryover_resumes_for_the_explicitly_supported_strategy(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    registry = _registry(
        tmp_path,
        feed,
        carryover_allowed=True,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        deployed = await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
            carryover_policy="ALLOW",
        )
        clerk.proof = _custody_proof(exposure={"SPY": 1.0})
        stopped = await registry.stop("alpaca", _SID)

        assert stopped.duty_outcome is not None
        assert stopped.duty_outcome.reason_code == "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE"
        assert stopped.carryover_checkpoint_exposure == {"SPY": 1.0}
        assert stopped.carryover_checkpoint_config_matches is True
        assert _lifecycle_json(tmp_path)["carryover_policy"] == "ALLOW"
        assert clerk.cancel_calls == [_SID]

        resumed = await registry.resume_existing("alpaca", _SID)
        assert resumed.running is True
        assert resumed.active_run_id != deployed.active_run_id
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_ema_resume_refuses_carried_exposure_without_restorable_exit_state(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    registry = _registry(
        tmp_path,
        feed,
        carryover_allowed=True,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
            mode="trade",
            carryover_policy="ALLOW",
        )
        clerk.proof = _custody_proof(exposure={"SPY": 1.0})
        await registry.stop("alpaca", _SID)

        with pytest.raises(RecoveryUncertainError, match="cannot safely restore"):
            await registry.resume_existing("alpaca", _SID)

        assert registry.status("alpaca", _SID).running is False
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_ema_resume_allows_freshly_proven_flat_custody(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    registry = _registry(
        tmp_path,
        feed,
        carryover_allowed=True,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        deployed = await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
            mode="trade",
            carryover_policy="ALLOW",
        )
        clerk.proof = _custody_proof(exposure={"SPY": 1.0})
        await registry.stop("alpaca", _SID)
        clerk.proof = _custody_proof(exposure={})

        resumed = await registry.resume_existing("alpaca", _SID)

        assert resumed.running is True
        assert resumed.active_run_id != deployed.active_run_id
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_carryover_resume_refuses_quantity_mismatch_without_side_effect(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    registry = _registry(
        tmp_path,
        feed,
        carryover_allowed=True,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
            carryover_policy="ALLOW",
        )
        clerk.proof = _custody_proof(exposure={"SPY": 1.0})
        await registry.stop("alpaca", _SID)
        clerk.proof = _custody_proof(exposure={"SPY": 2.0})

        with pytest.raises(RecoveryUncertainError, match="custody proof changed"):
            await registry.resume_existing("alpaca", _SID)

        assert registry.status("alpaca", _SID).running is False
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_forbidden_carryover_requires_flatten_before_resume(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )
        clerk.proof = _custody_proof(exposure={"SPY": 1.0})
        stopped = await registry.stop("alpaca", _SID)

        assert stopped.duty_outcome is not None
        assert stopped.duty_outcome.reason_code == "STOP_REQUIRES_FLATTEN"
        with pytest.raises(CarryoverPolicyRefusedError, match="not approved"):
            await registry.resume_existing("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_account_policy_refuses_carryover_before_artifact_write(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(CarryoverPolicyRefusedError):
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            carryover_policy="ALLOW",
        )

    assert not (tmp_path / "live_state" / _SID).exists()


@pytest.mark.asyncio
async def test_account_policy_must_remain_enabled_for_carryover_resume(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    enabled_registry = _registry(
        tmp_path,
        feed,
        carryover_allowed=True,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await enabled_registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
            carryover_policy="ALLOW",
        )
        clerk.proof = _custody_proof(exposure={"SPY": 1.0})
        await enabled_registry.stop("alpaca", _SID)

        disabled_registry = _registry(
            tmp_path,
            feed,
            carryover_allowed=False,
            start_custody_guard=clerk.start_admission_snapshot,
        )
        with pytest.raises(CarryoverPolicyRefusedError):
            await disabled_registry.resume_existing("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)

    assert disabled_registry.status("alpaca", _SID).running is False
    assert disabled_registry.status("alpaca", _SID).carryover_account_policy_enabled is False


# ── shutdown ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_run_registration_precedes_order_capable_task_creation(
    tmp_path: Path,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        deployed = await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        assert clerk.registered_runs == [deployed.active_run_id]
        assert not clerk.registration_saw_bot_task
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_commits_clerk_stop_before_task_cancellation(tmp_path: Path) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        deployed = await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        await registry.stop("alpaca", _SID)

        assert clerk.stopped_runs == [deployed.active_run_id]
        assert feed.cancelled_after_stop
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_quiesce_after_clerk_stop_does_not_commit_a_second_stop(
    tmp_path: Path,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        # Model the recovery flow: the SQLite authority already committed the
        # durable STOP before entering the registry. The registry must reap the
        # task *after* that commit, without authoring a second STOP.
        clerk.stop_committed.set()

        await registry.stop_after_durable_clerk_stop(
            "alpaca",
            _SID,
            updated_by="operator_recovery",
            reason="sqlite_recovery_stop_bot_decisions",
        )

        assert clerk.stopped_runs == []
        assert feed.cancelled_after_stop
        assert registry.status("alpaca", _SID).running is False
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_failed_clerk_stop_closes_run_gate_without_cancelling_task(
    tmp_path: Path,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )
        clerk.fail_stop = True

        with pytest.raises(RuntimeError, match="durable STOP failed"):
            await registry.stop("alpaca", _SID)

        managed = registry._bots[_SID]
        assert not managed.run_gate.is_set()
        assert not managed.task.done()
        assert not feed.cancelled_after_stop

        clerk.fail_stop = False
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_all_commits_each_trade_run_before_task_cancellation(
    tmp_path: Path,
) -> None:
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    feed = _StopOrderingFeed(clerk)
    registry = _registry(
        tmp_path,
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        deployed = await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )

        await registry.stop_all()

        assert clerk.stopped_runs == [deployed.active_run_id]
        assert feed.cancelled_after_stop
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_all_preserves_operator_intent(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    await registry.stop_all()
    await _wait_for(lambda: _SID not in registry._bots)

    view = registry.status("alpaca", _SID)
    assert view.running is False
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "STOPPED"
    assert view.duty_outcome.reason_code == "SERVICE_SHUTDOWN"
    # Operator intent untouched: the bot still WANTS to run after a restart.
    assert view.desired_state == "RUNNING"


# ── daemon-free and container-side by construction ────────────────────


def test_runner_is_daemon_free_by_construction() -> None:
    """P10 / L1: no host daemon, host socket, or subprocess in the runner path.

    Asserted against the actual import graph (AST), not raw text, so docs
    may name the banned machinery without tripping the guard.
    """
    import ast

    import app.routers.broker_bots as router_mod
    import app.services.bot_runner as runner_mod

    banned = ("host_daemon", "daemon_client", "daemon_transport", "subprocess", "multiprocessing")
    for mod in (runner_mod, router_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        for name in imported:
            assert not any(term in name for term in banned), f"{mod.__name__} imports banned module {name!r}"


@pytest.mark.asyncio
async def test_all_artifacts_are_written_under_the_container_root(tmp_path: Path) -> None:
    feed = _FakeFeed([_bar(_T0)], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)

    written = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        # .lock sidecars belong to the canonical repos' advisory-lock protocol.
        if p.is_file() and p.suffix != ".lock"
    )
    assert written == [
        f"live_state/{_SID}/current_run.json",
        f"live_state/{_SID}/desired_state.json",
        f"live_state/{_SID}/lifecycle_state.json",
        f"live_state/{_SID}/run_outcomes/{registry.binding_for_control('alpaca', _SID).run_id}.json",
        f"live_state/{_SID}/runs/{registry.binding_for_control('alpaca', _SID).run_id}.json",
        f"live_state/{_SID}/strategy_instance.json",
    ]


# ── trade bot ─────────────────────────────────────────────────────────────────
#
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


@pytest.mark.asyncio
async def test_real_trade_runner_routes_enter_and_exit_through_sqlite_facade(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path / "clerk",
    )
    broker = _SqliteRuntimeBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    base = _WIN_START_MS + 60_000
    feed = _FakeFeed(
        [
            _green_bar(base),
            _green_bar(base + 60_000),
            _red_bar(base + 120_000),
            _red_bar(base + 180_000),
            _red_bar(base + 240_000),
        ],
        mode="hold",
    )
    registry = _registry(
        tmp_path / "runner",
        feed,
        start_custody_guard=clerk.start_admission_snapshot,
    )
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="trade",
        )
        await _wait_for(lambda: bool(broker.cancellations))

        transition_kinds = {
            transition["transition_kind"]
            for transition in repo.custody_transitions()
        }
        assert "ENTER_ACCEPTED" in transition_kinds
        assert "EXIT_ACCEPTED" in transition_kinds
        assert broker.orders
        assert all(order.client_order_id in repo.all_order_refs() for order in broker.orders.values())
        await registry.stop("alpaca", _SID)
        assert repo.active_run(_SID) is None
    finally:
        set_alpaca_clerk(None)
        repo.close()


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
    ) -> _FakeEffectResult:
        del use_rth
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
        Path(__file__).resolve().parents[1]
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


@pytest.mark.asyncio
async def test_ema_trade_bot_matches_first_lean_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    bars = _ema_parity_bars_through_first_exit()
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="trade",
        quantity=3,
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: len(clerk.calls) >= 2)
    await registry.stop("alpaca", _SID)

    assert [(call["decision_id"], call["purpose"], call["quantity"]) for call in clerk.calls[:2]] == [
        (f"{_EMA_FIRST_ENTER_MS}:ENTER", "ENTER", 3),
        (f"{_EMA_FIRST_EXIT_MS}:EXIT", "EXIT", 3),
    ]


@pytest.mark.asyncio
async def test_sqlite_trade_bot_records_every_evaluated_bar_for_panel_health(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    transitions_before_decisions = repo.custody_transitions()
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    feed = _FakeFeed(
        [_bar(_RTH_MS + offset * 60_000) for offset in range(3)],
        mode="hold",
    )
    registry = _registry(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: feed.bars_consumed == 3)
        await _wait_for(lambda: len(repo.decision_receipt_tail(strategy_instance_id=_SID, limit=3)) == 3)
        await registry.stop("alpaca", _SID)

        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=3)
        facts = [json.loads(decision.facts_json) for decision in decisions]
        assert [decision.outcome for decision in decisions] == [
            "no_action",
            "enter_intent",
            "no_action",
        ]
        assert [fact["bar_ref"] for fact in facts] == [
            f"SPY@{_RTH_MS + offset * 60_000 + 60_000}" for offset in range(3)
        ]
        assert decisions[1].intent_id.endswith(":ENTER")
        assert decisions[1].order_ref is None
        # Decision receipts are product evidence, not custody. The only new
        # authority transitions are the run's required duty boundaries.
        transition_kinds = [
            row["transition_kind"]
            for row in repo.custody_transitions()[len(transitions_before_decisions) :]
        ]
        assert transition_kinds == ["RUN_STARTED", "RUN_STOPPED"]
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_unknown_liveness_blocks_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        lambda symbol, observed_at_ms: unknown_market_liveness(
            symbol,
            observed_at_ms=observed_at_ms,
        ),
    )
    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),  # ENTER is refused by liveness.
        _red_bar(_WIN_START_MS + 180_000),
        _red_bar(_WIN_START_MS + 240_000),
        _red_bar(_WIN_START_MS + 300_000),
    ]
    registry = _registry(tmp_path, _FakeFeed(bars, mode="hold"))
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(repo.decision_receipt_tail(strategy_instance_id=_SID, limit=10)) >= 5)
        await registry.stop("alpaca", _SID)

        # Rolled back (#1671 AC6): no real entry means no phantom EXIT either.
        assert clerk.calls == []
        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=5)
        blocked = next(decision for decision in decisions if decision.outcome == "blocked")
        blocked_facts = json.loads(blocked.facts_json)
        assert blocked_facts["reason_code"] == "MARKET_LIVENESS_UNAVAILABLE"
        assert blocked_facts["market_liveness"]["state"] == "UNKNOWN"
        assert all(decision.outcome in {"blocked", "no_action"} for decision in decisions)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_halted_liveness_blocks_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1671 AC4: a market-wide OPEN clock with a HALTED symbol must never
    claim tradability at the actual submit-blocking layer (not just at the
    compose/display layers, which have their own dedicated tests)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        lambda symbol, observed_at_ms: compose_market_liveness(
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
                state="HALTED",
                source="test.symbol-status",
                observed_at_ms=observed_at_ms,
                source_timestamp_ms=observed_at_ms,
            ),
        ),
    )
    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),  # ENTER is refused by liveness.
        _red_bar(_WIN_START_MS + 180_000),
        _red_bar(_WIN_START_MS + 240_000),
        _red_bar(_WIN_START_MS + 300_000),
    ]
    registry = _registry(tmp_path, _FakeFeed(bars, mode="hold"))
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(repo.decision_receipt_tail(strategy_instance_id=_SID, limit=10)) >= 5)
        await registry.stop("alpaca", _SID)

        # No real entry was ever accepted, so no phantom EXIT reaches the
        # Clerk either — the blocked ENTER's state was rolled back (#1671
        # AC6); see test_blocked_entry_is_rolled_back_and_can_re_enter for
        # the regression this rollback specifically targets.
        assert clerk.calls == []
        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=5)
        blocked = next(decision for decision in decisions if decision.outcome == "blocked")
        blocked_facts = json.loads(blocked.facts_json)
        assert blocked_facts["reason_code"] == "SYMBOL_HALTED"
        assert blocked_facts["market_liveness"]["state"] == "HALTED"
        assert all(decision.outcome in {"blocked", "no_action"} for decision in decisions)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_blocked_entry_is_rolled_back_and_can_re_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1671 AC6 regression: before the rollback fix, a blocked ENTER left
    ``DeploymentValidationDecisionKernel._cycle_active`` set, so the *next*
    green-bar pair was consumed by the stale exit countdown instead of
    starting a fresh entry attempt — and the phantom EXIT it eventually
    emitted had no real custody to close, crashing the bot run with
    ``MissingEntryCustodyError``. With the rollback, the blocked bar leaves
    no trace: the following two green bars start a clean entry, and the
    three red bars after that close it out normally."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    # market_liveness_fact is only ever queried on an ENTER intent (#1671
    # AC3), so the Nth call corresponds exactly to the Nth ENTER attempt —
    # a reliable way to block just the first one. Bar-relative fixture
    # constants can't be compared against `observed_at_ms`: the real gate
    # passes it `now_ms_utc()`, actual wall-clock time, not the bar's.
    entry_attempts = {"count": 0}

    def liveness(symbol: str, observed_at_ms: int):
        entry_attempts["count"] += 1
        if entry_attempts["count"] == 1:
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
                    state="HALTED",
                    source="test.symbol-status",
                    observed_at_ms=observed_at_ms,
                    source_timestamp_ms=observed_at_ms,
                ),
            )
        return _tradable_market_liveness(symbol, observed_at_ms)

    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),  # ENTER attempt #1 — blocked, rolled back.
        _green_bar(_WIN_START_MS + 180_000),
        _green_bar(_WIN_START_MS + 240_000),  # ENTER attempt #2 — fresh streak, TRADABLE.
        _red_bar(_WIN_START_MS + 300_000),
        _red_bar(_WIN_START_MS + 360_000),
        _red_bar(_WIN_START_MS + 420_000),  # EXIT for the real entry.
    ]
    registry = _registry(tmp_path, _FakeFeed(bars, mode="hold"))
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(clerk.calls) == 2)
        await registry.stop("alpaca", _SID)

        assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]
    finally:
        set_alpaca_clerk(None)
        repo.close()


def test_closed_liveness_with_extended_phase_proven_does_not_block_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alpaca's clock is RTH-only (#1671): a non-RTH binding whose account
    and instrument have a fresh, proven extended-session capability must not
    be blocked just because the RTH-only clock reports CLOSED."""
    from types import SimpleNamespace

    liveness = compose_market_liveness(
        "SPY",
        now_ms=1_700_000_000_000,
        market_clock=MarketClockLivenessEvidence(
            state="CLOSED",
            source="test.clock",
            observed_at_ms=1_700_000_000_000,
            vendor_timestamp_ms=1_700_000_000_000,
        ),
        connected=True,
        connection_changed_at_ms=1_700_000_000_000,
        symbol_status=None,
    )
    monkeypatch.setattr(bot_trade_strategy, "extended_phase_proven_at_ms", lambda **_kwargs: True)
    binding = SimpleNamespace(use_rth=False, symbol="SPY")

    assert bot_trade_strategy._liveness_blocks_entry(binding, "PA-TEST", liveness) is False


def test_closed_liveness_without_extended_phase_proven_still_blocks_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a fresh, matching capability the calendar can prove only
    RTH/CLOSED — CLOSED must still block a non-RTH binding's entry."""
    from types import SimpleNamespace

    liveness = compose_market_liveness(
        "SPY",
        now_ms=1_700_000_000_000,
        market_clock=MarketClockLivenessEvidence(
            state="CLOSED",
            source="test.clock",
            observed_at_ms=1_700_000_000_000,
            vendor_timestamp_ms=1_700_000_000_000,
        ),
        connected=True,
        connection_changed_at_ms=1_700_000_000_000,
        symbol_status=None,
    )
    monkeypatch.setattr(bot_trade_strategy, "extended_phase_proven_at_ms", lambda **_kwargs: False)
    binding = SimpleNamespace(use_rth=False, symbol="SPY")

    assert bot_trade_strategy._liveness_blocks_entry(binding, "PA-TEST", liveness) is True


def test_closed_liveness_always_blocks_entry_for_an_rth_only_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An RTH-only binding never consults extended-phase capability — CLOSED
    blocks unconditionally, matching the previous, unambiguous behavior."""
    from types import SimpleNamespace

    liveness = compose_market_liveness(
        "SPY",
        now_ms=1_700_000_000_000,
        market_clock=MarketClockLivenessEvidence(
            state="CLOSED",
            source="test.clock",
            observed_at_ms=1_700_000_000_000,
            vendor_timestamp_ms=1_700_000_000_000,
        ),
        connected=True,
        connection_changed_at_ms=1_700_000_000_000,
        symbol_status=None,
    )
    binding = SimpleNamespace(use_rth=True, symbol="SPY")

    assert bot_trade_strategy._liveness_blocks_entry(binding, "PA-TEST", liveness) is True


@pytest.mark.asyncio
async def test_sqlite_trade_bot_does_not_label_an_uncertain_effect_as_entered(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(effect_state="uncertain", repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    feed = _FakeFeed(
        [_bar(_RTH_MS + offset * 60_000) for offset in range(2)],
        mode="hold",
    )
    registry = _registry(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(clerk.calls) == 1)
        await registry.stop("alpaca", _SID)

        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=2)
        assert decisions[-1].outcome == "enter_intent"
        assert json.loads(decisions[-1].facts_json)["reason_code"] == "STRATEGY_ENTER"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_sqlite_trade_bot_records_a_rejected_enter_as_a_blocked_decision(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(effect_state="rejected", repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    feed = _FakeFeed(
        [_bar(_RTH_MS + offset * 60_000) for offset in range(2)],
        mode="hold",
    )
    registry = _registry(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(clerk.calls) == 1)
        await registry.stop("alpaca", _SID)

        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=2)
        assert decisions[-1].outcome == "blocked"
        facts = json.loads(decisions[-1].facts_json)
        assert facts["reason_code"] == "CLERK_ADMISSION_REJECTED"
        assert "refusal_reason" in facts
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_decision_receipt_failure_prevents_the_broker_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    original = SqliteDecisionReceipts.append

    def fail_enter_receipt(
        self: SqliteDecisionReceipts,
        **fields: Any,
    ) -> DecisionReceiptResource:
        if fields["outcome"] == "enter_intent":
            raise OSError("injected decision receipt failure")
        return original(self, **fields)

    monkeypatch.setattr(SqliteDecisionReceipts, "append", fail_enter_receipt)
    feed = _FakeFeed(
        [_bar(_RTH_MS + offset * 60_000) for offset in range(2)],
        mode="hold",
    )
    registry = _registry(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: not registry.status("alpaca", _SID).running)

        assert clerk.calls == []
        outcome = registry.status("alpaca", _SID).duty_outcome
        assert outcome is not None
        assert outcome.kind == "CRASHED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_dry_run_records_simulated_round_trip_with_zero_broker_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    bars = _ema_parity_bars_through_first_exit()
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="dry_run",
        quantity=3,
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: len(registry.dry_run_activity("alpaca", _SID)) >= 2)

    activity = registry.dry_run_activity("alpaca", _SID)
    assert deployed.mode == "dry_run"
    assert clerk.calls == []
    assert [(row.intent, row.side, row.quantity, row.simulated) for row in activity[:2]] == [
        ("ENTER", "buy", 3.0, True),
        ("EXIT", "sell", 3.0, True),
    ]
    assert all(row.order_ref.startswith("simulated:") for row in activity)
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_ema_trade_bot_releases_backtest_chart_bars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import bot_trade_strategy

    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    contexts: list[StrategyContext] = []
    context_factory = bot_trade_strategy.StrategyContext

    def capture_context(*, portfolio: Portfolio) -> StrategyContext:
        context = context_factory(portfolio=portfolio)
        contexts.append(context)
        return context

    monkeypatch.setattr(bot_trade_strategy, "StrategyContext", capture_context)
    bars = _ema_parity_bars_through_first_exit()
    feed = _FakeFeed(bars, mode="finite")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="trade",
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    assert len(contexts) == 1
    assert contexts[0].consolidated_bars == []
    assert isinstance(contexts[0].current_time_ms, int)
    assert not any(isinstance(value, datetime) for value in vars(contexts[0]).values())


# ── entry after exactly 2 green bars in-window ────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_enters_after_two_green_bars_in_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    # One red bar then two green bars inside the detection window, then hold.
    bars = [
        _red_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),
        _green_bar(_WIN_START_MS + 180_000),
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=2)
    await _wait_for(lambda: feed.bars_consumed == 3)
    await registry.stop("alpaca", _SID)

    # One semantic ENTER after the second consecutive green bar.  The runtime
    # never supplies a broker side; the Clerk derives that from the plan.
    assert len(clerk.calls) == 1
    assert clerk.calls[0]["purpose"] == "ENTER"
    assert clerk.calls[0]["quantity"] == 2
    assert clerk.calls[0]["strategy_instance_id"] == _SID


# ── no entry before detection window ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_no_entry_before_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    # Two green bars strictly before the 09:45 ET window start.
    pre_window_1 = _SESSION_OPEN_MS + 60_000  # 09:31 ET
    pre_window_2 = _SESSION_OPEN_MS + 120_000  # 09:32 ET
    bars = [
        _green_bar(pre_window_1),
        _green_bar(pre_window_2),
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade")
    await _wait_for(lambda: feed.bars_consumed == 2)
    await registry.stop("alpaca", _SID)

    assert clerk.calls == []


# ── exit 3 bars after entry ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_exits_three_bars_after_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),  # entry triggered after this bar
        _red_bar(base + 120_000),  # in-position bar 1
        _red_bar(base + 180_000),  # in-position bar 2
        _green_bar(base + 240_000),  # in-position bar 3 → exit
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=3)
    await _wait_for(lambda: feed.bars_consumed == 5)
    await registry.stop("alpaca", _SID)

    assert len(clerk.calls) == 2
    assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]
    assert clerk.calls[1]["quantity"] == 3


# ── window-end flatten when holding ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_flattens_at_window_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),  # triggers BUY
        _red_bar(base + 120_000),  # bar 1 in position
        _green_bar(_WIN_END_MS + 1),  # past window end → FLATTEN
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=1)
    await _wait_for(lambda: feed.bars_consumed == 4)
    await registry.stop("alpaca", _SID)

    assert len(clerk.calls) == 2
    assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]


# ── quantity plumbed through correctly ───────────────────────────────────────


@pytest.mark.asyncio
async def test_trade_bot_quantity_plumbed_from_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [_green_bar(base), _green_bar(base + 60_000)]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=7)
    await _wait_for(lambda: feed.bars_consumed == 2)
    await registry.stop("alpaca", _SID)

    assert clerk.calls[0]["quantity"] == 7

    # Immutable instance artifact carries deployment semantics.
    instance = _strategy_instance_json(tmp_path)
    assert instance["quantity"] == 7
    assert instance["mode"] == "trade"
    assert instance["action_plan"]["on_enter"][0]["position"] == "long"


# ── submit exception → task errors (no silent handler) ───────────────────────


@pytest.mark.asyncio
async def test_trade_bot_submit_exception_crashes_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.contract.errors import BrokerError

    error = BrokerError("forced failure", detail="test")
    clerk = _FakeClerk(should_raise=error)
    _install_fake_clerk(monkeypatch, clerk)

    base = _WIN_START_MS + 60_000
    bars = [_green_bar(base), _green_bar(base + 60_000)]
    feed = _FakeFeed(bars, mode="finite")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade")
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "BrokerError"


# ── log_only behavior unchanged ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_only_bot_unchanged_after_trade_mode_added(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Regression: adding trade mode must not alter log_only behavior."""
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="hold")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runner"):
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="log_only",
        )
        await _wait_for(lambda: feed.bars_consumed == 2)
        await registry.stop("alpaca", _SID)

    decisions = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert len(decisions) == 2
    assert all(d.decision == "HOLD" for d in decisions)

    instance = _strategy_instance_json(tmp_path)
    assert instance["mode"] == "log_only"

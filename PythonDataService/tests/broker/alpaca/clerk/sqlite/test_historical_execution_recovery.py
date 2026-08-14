"""Regression coverage for exact activity recovery of pre-quarantine conflicts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.facts import (
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import (
    HistoricalExecutionRecoveryRefused,
    confirm_historical_execution_recovery,
    prepare_historical_execution_recovery,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projection_errors import InvalidTimelineCursor
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerOrder,
    BrokerOrderLeg,
)

ACCOUNT_ID = "PA-HISTORICAL-RECOVERY"
SID = "historical-recovery-bot"
RUN_ID = "historical-recovery-run"
NOW_MS = 1_786_368_000_900
EXECUTION_ID = "historical-execution-5"


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Read:
    broker_id = "alpaca"

    def __init__(
        self,
        *,
        activities: list[BrokerActivity],
        account_mode: Literal["paper", "live"] = "paper",
        error: Exception | None = None,
    ) -> None:
        self._activities = activities
        self._account_mode = account_mode
        self._error = error

    async def get_account(self) -> BrokerAccountSnapshot:
        if self._error is not None:
            raise self._error
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=ACCOUNT_ID,
            account_mode=self._account_mode,
            account_status="ACTIVE",
            currency="USD",
            cash=1_000.0,
            equity=1_000.0,
            buying_power=2_000.0,
            portfolio_value=1_000.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=None,
            observed_at_ms=NOW_MS,
        )

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100):
        if self._error is not None:
            raise self._error
        return self._activities[:limit]


def _activity(
    *,
    quantity: float = 5.0,
    price: float = 100.0,
    native_order_id: str = "alpaca-order-5",
    symbol: str = "SPY",
) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca",
        activity_id=EXECUTION_ID,
        native_order_id=native_order_id,
        activity_type="FILL",
        category="trade_activity",
        symbol=symbol,
        side="buy",
        quantity=quantity,
        price=price,
        net_amount=None,
        occurred_at_ms=NOW_MS - 100,
        observed_at_ms=NOW_MS,
    )


def _seed_historical_conflict(tmp_path: Path) -> tuple[ClerkSqliteRepository, str]:
    clock = _Clock(NOW_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID,
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="historical-recovery",
        lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=5),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    fold_order_evidence(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order=BrokerOrder(
            broker="alpaca",
            order_id="alpaca-order-5",
            client_order_id=accepted.order_ref,
            symbol="SPY",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=5.0,
            filled_quantity=5.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=100.0,
            status="filled",
            submitted_at_ms=NOW_MS - 500,
            created_at_ms=NOW_MS - 500,
            updated_at_ms=NOW_MS - 300,
            filled_at_ms=NOW_MS - 300,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=NOW_MS - 300,
        ),
    )
    conflict = UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
        headline="Exact execution overlaps aggregate recovery evidence",
        explanation="Historical authority retained the execution ID but not its exact economics.",
        operator_impact="New exposure is blocked until exact coverage is recovered.",
        next_step="Recover the retained exact broker execution.",
        evidence_refs=[EXECUTION_ID],
        cause_facts=ExecutionCoverageConflictCause(
            order_ref=accepted.order_ref,
            execution_id=EXECUTION_ID,
        ).to_mapping(),
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=SID,
            run_id=accepted.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="UNCERTAINTY_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=clock(),
            summary_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
            facts_json=conflict.to_facts_json(),
        )
    )
    uncertainty = repo.active_uncertainties_for_admission(strategy_instance_id=SID)
    assert len(uncertainty) == 1
    return repo, uncertainty[0]["uncertainty_id"]


def _recovery_action(repo: ClerkSqliteRepository):
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    assert context is not None
    action = next(
        item
        for item in build_recovery_catalog(context)
        if item.action_id == "recover_exact_execution_evidence"
    )
    assert action.available is True
    return context, action


def test_timeline_filters_bind_one_historical_conflict_and_cursor_scope(tmp_path: Path) -> None:
    """Every evidence identity finds its immutable transition, never another page."""
    repo, uncertainty_id = _seed_historical_conflict(tmp_path)
    try:
        row = repo._conn.execute(
            "SELECT sequence, order_ref, effect_operation_id FROM custody_transitions "
            "WHERE transition_kind = 'UNCERTAINTY_RAISED'"
        ).fetchone()
        assert row is not None
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            by_bot = reader.timeline_page(strategy_instance_id=SID)
            by_order = reader.timeline_page(order_ref=row["order_ref"], page_size=1)
            by_effect = reader.timeline_page(effect_operation_id=row["effect_operation_id"])
            by_uncertainty = reader.timeline_page(uncertainty_id=uncertainty_id)
            by_execution = reader.timeline_page(execution_id=EXECUTION_ID)
            by_kind = reader.timeline_page(transition_kind="UNCERTAINTY_RAISED")
            by_sequence = reader.timeline_page(sequence=row["sequence"])
            assert by_order.next_cursor is not None
            with pytest.raises(InvalidTimelineCursor):
                reader.timeline_page(
                    order_ref=row["order_ref"],
                    execution_id=EXECUTION_ID,
                    cursor=by_order.next_cursor,
                )
        finally:
            reader.close()
    finally:
        repo.close()

    assert row["sequence"] in {entry.sequence for entry in by_bot.entries}
    assert row["sequence"] in {entry.sequence for entry in by_effect.entries}
    assert {entry.effect_operation_id for entry in by_effect.entries} == {
        row["effect_operation_id"]
    }
    for page in (by_uncertainty, by_execution, by_kind, by_sequence):
        assert [entry.sequence for entry in page.entries] == [row["sequence"]]
    assert by_order.anchor_sequence >= row["sequence"]


@pytest.mark.asyncio
async def test_historical_exact_execution_recovery_prepares_confirms_and_replays(
    tmp_path: Path,
) -> None:
    repo, uncertainty_id = _seed_historical_conflict(tmp_path)
    try:
        context, action = _recovery_action(repo)
        before = repo.control_meta_snapshot().control_revision
        read = _Read(activities=[_activity()])
        plan = await prepare_historical_execution_recovery(
            repo=repo,
            read=read,
            context=context,
            concurrency_token=action.concurrency_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
        )

        assert plan.uncertainty_id == uncertainty_id
        assert plan.exact_symbol == "SPY"
        assert plan.exact_quantity == 5.0
        assert repo.control_meta_snapshot().control_revision == before

        receipt = confirm_historical_execution_recovery(
            repo=repo,
            plan=plan,
            confirmation_token=plan.confirmation_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
            observed_account=await read.get_account(),
        )
        replay = confirm_historical_execution_recovery(
            repo=repo,
            plan=plan,
            confirmation_token=plan.confirmation_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
            observed_account=await read.get_account(),
        )

        assert receipt.applied is True
        assert replay == replace(receipt, applied=False)
        assert repo.active_uncertainties_for_admission(strategy_instance_id=SID) == []
        fills = repo.fills_for_order(plan.order_ref)
        assert [(item["execution_id"], item["evidence_source"]) for item in fills] == [
            (EXECUTION_ID, "activity_recovery"),
        ]
        assert repo.position(SID, "SPY") == pytest.approx(5.0, abs=1e-9, rel=0)
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            refreshed = reader.recovery_context(strategy_instance_id=SID)
        finally:
            reader.close()
        assert refreshed is not None
        assert refreshed.execution_coverage_conflicts == ()
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_historical_exact_execution_recovery_resumes_after_quarantine_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after evidence retention must replay through the existing resolver."""
    repo, _ = _seed_historical_conflict(tmp_path)
    try:
        context, action = _recovery_action(repo)
        read = _Read(activities=[_activity()])
        plan = await prepare_historical_execution_recovery(
            repo=repo,
            read=read,
            context=context,
            concurrency_token=action.concurrency_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
        )
        original_resolve = repo.resolve_execution_coverage_conflict

        def _interrupted_resolution(**_kwargs):
            raise RuntimeError("simulated process interruption after quarantine")

        monkeypatch.setattr(repo, "resolve_execution_coverage_conflict", _interrupted_resolution)
        with pytest.raises(RuntimeError, match="simulated process interruption"):
            confirm_historical_execution_recovery(
                repo=repo,
                plan=plan,
                confirmation_token=plan.confirmation_token,
                control_secret="test-control-secret",
                allow_unauthenticated_control=False,
                observed_account=await read.get_account(),
            )
        monkeypatch.setattr(repo, "resolve_execution_coverage_conflict", original_resolve)

        receipt = confirm_historical_execution_recovery(
            repo=repo,
            plan=plan,
            confirmation_token=plan.confirmation_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
            observed_account=await read.get_account(),
        )

        assert receipt.applied is True
        assert repo.active_uncertainties_for_admission(strategy_instance_id=SID) == []
        assert len(
            [
                transition
                for transition in repo.transitions_for_order(plan.order_ref)
                if transition["transition_kind"] == "EXECUTION_COVERAGE_QUARANTINED"
            ]
        ) == 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_historical_exact_execution_recovery_rechecks_paper_mode_on_confirm(
    tmp_path: Path,
) -> None:
    repo, _ = _seed_historical_conflict(tmp_path)
    try:
        context, action = _recovery_action(repo)
        paper = _Read(activities=[_activity()])
        plan = await prepare_historical_execution_recovery(
            repo=repo,
            read=paper,
            context=context,
            concurrency_token=action.concurrency_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
        )

        with pytest.raises(HistoricalExecutionRecoveryRefused) as captured:
            confirm_historical_execution_recovery(
                repo=repo,
                plan=plan,
                confirmation_token=plan.confirmation_token,
                control_secret="test-control-secret",
                allow_unauthenticated_control=False,
                observed_account=await _Read(
                    activities=[_activity()],
                    account_mode="live",
                ).get_account(),
            )

        assert captured.value.reason == "LIVE_ACCOUNT_REFUSED"
        assert repo.active_uncertainties_for_admission(strategy_instance_id=SID)
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_historical_exact_execution_recovery_rejects_a_stale_clerk_plan(
    tmp_path: Path,
) -> None:
    repo, _ = _seed_historical_conflict(tmp_path)
    try:
        context, action = _recovery_action(repo)
        read = _Read(activities=[_activity()])
        plan = await prepare_historical_execution_recovery(
            repo=repo,
            read=read,
            context=context,
            concurrency_token=action.concurrency_token,
            control_secret="test-control-secret",
            allow_unauthenticated_control=False,
        )
        repo.register_strategy_instance(
            strategy_instance_id="unrelated-new-bot",
            symbol="QQQ",
            config_hash="h2",
        )

        with pytest.raises(HistoricalExecutionRecoveryRefused) as captured:
            confirm_historical_execution_recovery(
                repo=repo,
                plan=plan,
                confirmation_token=plan.confirmation_token,
                control_secret="test-control-secret",
                allow_unauthenticated_control=False,
                observed_account=await read.get_account(),
            )

        assert captured.value.reason == "RECOVERY_PLAN_STALE"
        assert repo.active_uncertainties_for_admission(strategy_instance_id=SID)
    finally:
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "reason"),
    [
        (_Read(activities=[],), "EXECUTION_ACTIVITY_NOT_FOUND"),
        (_Read(activities=[_activity(), _activity()]), "EXECUTION_ACTIVITY_AMBIGUOUS"),
        (_Read(activities=[_activity(native_order_id="other-order")]), "EXECUTION_ORDER_MISMATCH"),
        (_Read(activities=[_activity(symbol="QQQ")]), "EXECUTION_SYMBOL_MISMATCH"),
        (_Read(activities=[_activity(price=101.0)]), "EXECUTION_ECONOMICS_MISMATCH"),
        (_Read(activities=[_activity()], account_mode="live"), "LIVE_ACCOUNT_REFUSED"),
        (
            _Read(activities=[], error=BrokerUnavailable("unavailable", broker="alpaca")),
            "HISTORICAL_EVIDENCE_UNAVAILABLE",
        ),
    ],
)
async def test_historical_exact_execution_recovery_fails_closed_for_unproven_evidence(
    tmp_path: Path,
    read: _Read,
    reason: str,
) -> None:
    repo, _ = _seed_historical_conflict(tmp_path)
    try:
        context, action = _recovery_action(repo)
        with pytest.raises(HistoricalExecutionRecoveryRefused) as captured:
            await prepare_historical_execution_recovery(
                repo=repo,
                read=read,
                context=context,
                concurrency_token=action.concurrency_token,
                control_secret="test-control-secret",
                allow_unauthenticated_control=False,
            )
        assert captured.value.reason == reason
        assert repo.active_uncertainties_for_admission(strategy_instance_id=SID)
    finally:
        repo.close()

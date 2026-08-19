"""Tests for app.services.bot_resume_admission.BotResumeAdmission."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.market_liveness import MarketClockLivenessEvidence, MarketLivenessFact
from app.schemas.run_admission import RunProcessAdmissionFact, StartRuntimeAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_resume_admission import BotResumeAdmission
from app.services.market_liveness import compose_market_liveness

_SID = "alpaca-resume-1"


def _count(state: str = "zero") -> CustodyCountFact:
    return CustodyCountFact(state=state, count=0 if state == "zero" else None)


def _clerk(observed_at_ms: int) -> ClerkCustodySnapshot:
    """Flat, clean custody — isolates the admission-timing check."""
    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id="paper-account",
        strategy_instance_id=_SID,
        clerk_generation="clerk-1",
        journal_sequence=7,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=observed_at_ms,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=_count(),
        pending_orders=_count(),
        terminal_orders=_count(),
        unresolved_effects=_count(),
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=("clerk:paper-account:7",),
        observed_at_ms=observed_at_ms,
    )


def _prior() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-prior",
        created_at_ms=500,
    )


def _status() -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=_SID,
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        running=False,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id=None,
        duty_outcome=None,
        binding_created_at_ms=500,
        last_transition_at_ms=None,
    )


@pytest.mark.asyncio
async def test_resume_admission_evaluates_liveness_with_a_post_await_timestamp() -> None:
    """Regression: ``observed_at_ms`` was captured before awaiting the
    runtime-fact resolver. A market-clock refresh landing during that await
    produces evidence newer than that stale snapshot, so
    ``compose_market_liveness`` sees ``now_ms < observed_at_ms`` and refuses
    Resume outright. The timestamp passed to ``market_data_admission_fact``
    and ``market_liveness`` must be captured *after* the await, not before."""
    clock_values = iter([1_000, 5_000, 9_000, 13_000])

    def now_ms() -> int:
        return next(clock_values)

    async def runtime_fact(strategy_instance_id: str, observed_at_ms: int) -> StartRuntimeAdmissionFact:
        del strategy_instance_id
        return StartRuntimeAdmissionFact(state="READY", observed_at_ms=observed_at_ms, explanation="ready")

    def process_fact(binding: object, observed_at_ms: int) -> RunProcessAdmissionFact:
        del binding
        return RunProcessAdmissionFact(
            state="ABSENT",
            run_id=None,
            process_identity=None,
            registry_generation="registry-1",
            observed_at_ms=observed_at_ms,
        )

    @asynccontextmanager
    async def custody_guard(strategy_instance_id: str):
        del strategy_instance_id
        yield _clerk(observed_at_ms=1_000)

    async def activate(*args: object, **kwargs: object) -> None:
        raise AssertionError("activate must not be called by preview()")

    seen_observed_at_ms: list[int] = []

    def market_liveness(symbol: str, observed_at_ms: int) -> MarketLivenessFact:
        seen_observed_at_ms.append(observed_at_ms)
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
            symbol_status=None,
        )

    admission = BotResumeAdmission(
        now_ms=now_ms,
        feed_resolver=lambda: None,
        custody_guard=custody_guard,
        process_fact=process_fact,
        runtime_fact=runtime_fact,
        checkpoint=lambda binding: None,
        activate=activate,
        carryover_account_policy_enabled=False,
        session_capability=lambda symbol, account_id: None,
        market_liveness=market_liveness,
    )

    await admission.preview(_prior(), _status())

    # 1_000 minted the proposed run binding; 5_000 was the pre-await
    # snapshot; market_liveness must see 9_000 (the post-await recapture),
    # never 5_000.
    assert seen_observed_at_ms == [9_000]

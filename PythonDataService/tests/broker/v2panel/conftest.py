"""Shared Alpaca paper-deploy HTTP test harness (fixtures.py is journal builders only).

``deploy_app`` and its supporting fakes back every test exercising
``/api/brokers/{broker}/accounts/{account_id}/bots*`` — the deploy route,
the admission-preview route, and the pure strategy-projection helpers that
drive them. Centralized here so it is auto-discovered by every test module
in this package without a cross-file import.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.models import ChannelHealth, ClerkStatus, HoldState
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.schemas.operator_blocker import AccountOperatorPosture
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.strategy_validation import StrategyValidationEntry, StrategyValidationFlagRequest
from app.services.bot_runner import AdmittedBotStart, set_bot_task_registry
from app.services.broker_account_snapshot import (
    clear_broker_account_snapshot_cache_for_testing,
)
from app.services.broker_v2_panel import panel_data_source
from app.services.strategy_validation_manifest import (
    append_strategy_validation_flag_event,
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from app.utils.timestamps import now_ms_utc
from tests.broker.v2panel.fixtures import ACCT, SID

_T0 = 1_700_000_000_000
_HEALTHY_POSTURE = AccountOperatorPosture(
    condition=None,
    account_desk=None,
    fleet_roster=None,
    status_headline="Account Clerk custody is healthy",
    status_detail=None,
)


class _FakeAccount:
    account_id = ACCT
    account_mode = "paper"
    account_status = "ACTIVE"
    trading_blocked = False
    account_blocked = False


class _FakeReadPort:
    broker_id = "alpaca"

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


class _FakeDeployRegistry:
    def __init__(self) -> None:
        self.deploy_calls: list[dict] = []

    def _decision(self, kwargs: dict) -> RunAdmissionDecision:
        return RunAdmissionDecision(
            operation="START",
            allowed=True,
            reason_code="START_ADMITTED",
            explanation="The Clerk and bot registry admit Start.",
            next_step=None,
            strategy_instance_id=kwargs["strategy_instance_id"],
            proposed_run_id="run-test",
            configuration_hash="a" * 64,
            account_id=ACCT,
            evaluated_at_ms=_T0,
            fact_ages_ms={
                "program_build": 0,
                "runtime": 0,
                "process": 0,
                "market_data": 0,
                "market_liveness": 0,
                "clerk": 0,
            },
            evidence_refs=("test-admission",),
        )

    async def deploy_with_admission(self, **kwargs: object) -> AdmittedBotStart:
        self.deploy_calls.append(kwargs)
        bot = BotStatusView(
            strategy_instance_id=kwargs["strategy_instance_id"],
            strategy_key=kwargs["strategy_key"],
            broker=kwargs["broker"],
            symbol=kwargs["symbol"],
            mode=kwargs["mode"],
            quantity=kwargs["quantity"],
            running=True,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id="run-test",
            duty_outcome=None,
            binding_created_at_ms=_T0,
            last_transition_at_ms=None,
        )
        return AdmittedBotStart(bot=bot, admission=self._decision(kwargs))

    async def preview_start_admission(self, **kwargs: object) -> RunAdmissionDecision:
        return self._decision(kwargs)


@pytest.fixture()
def deploy_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[FastAPI, _FakeDeployRegistry]]:
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    registry_seeds = strategy_registry_seeds()
    flag_events_path = tmp_path / "strategy-validation" / "flag-events.json"
    for strategy_key in ("rsi_mean_reversion", "sma_crossover"):
        append_strategy_validation_flag_event(
            strategy_key,
            StrategyValidationFlagRequest(
                flag="validated",
                reason="Test-only human validation without accepted equivalence evidence.",
            ),
            registry_seeds,
            flag_events_path=flag_events_path,
            flagged_by="test:deploy-route",
            now_ms=_T0,
        )
    validation_entries = [
        entry
        for entry in load_strategy_validation_entries(
            registry_seeds,
            flag_events_path=flag_events_path,
        )
        if entry.strategy_key in {
            "ema_crossover_signal",
            "rsi_mean_reversion",
            "sma_crossover",
        }
    ]
    monkeypatch.setattr(
        panel_data_source,
        "load_strategy_validation_entries",
        lambda _registry: validation_entries,
    )
    clear_broker_account_snapshot_cache_for_testing()
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())  # type: ignore[arg-type]
    registry = _FakeDeployRegistry()
    set_bot_task_registry(registry)  # type: ignore[arg-type]

    async def clerk_status(*, symbol: str | None = None) -> ClerkStatus:
        observed_at_ms = now_ms_utc()
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=observed_at_ms,
            channel_healths=[
                ChannelHealth(stream="market_data", healthy=True, connected=True, observed_at_ms=observed_at_ms),
                ChannelHealth(stream="execution", healthy=True, connected=True, observed_at_ms=observed_at_ms),
            ],
            operator_posture=_HEALTHY_POSTURE,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", clerk_status)

    fast_app = FastAPI()
    fast_app.include_router(router)

    try:
        yield fast_app, registry
    finally:
        set_bot_task_registry(None)
        clear_broker_account_snapshot_cache_for_testing()
        reset_broker_registry_for_testing()


_BODY = {
    "strategy_instance_id": SID,
    # ema_crossover_signal, not deployment_validation: #1672 deliberately
    # changed deployment_validation's session-boundary literals (see
    # docs/references/deployment-validation-consecutive-green.md), which
    # invalidates its manifest-pinned evidence hashes until a fresh QC
    # Cloud reconciliation is run. This file exercises the deploy route's
    # own orchestration, not evidence-hash integrity — that's covered by
    # tests/routers/test_strategy_validation.py — so its default fixture
    # strategy needs to be one with currently-matching evidence.
    "strategy_key": "ema_crossover_signal",
    "symbol": "SPY",
    "sizing": {"preset": "custom", "quantity": 2},
}


def _accepted_deploy_entry() -> StrategyValidationEntry:
    return next(
        entry
        for entry in load_strategy_validation_entries(strategy_registry_seeds())
        if entry.strategy_key == "ema_crossover_signal"
    )

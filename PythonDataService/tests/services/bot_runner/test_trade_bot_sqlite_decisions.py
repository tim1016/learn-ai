"""SQLite decision-receipt recording: the live trade bot's per-bar
decisions, routed through the real Clerk SQLite repository.

Split from ``tests/services/test_bot_runner.py`` (issue #1737, seam 2).
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import EvaluationMode, Settlement
from tests._helpers.bot_runner.custody import _SID, _registry
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed, _SqliteRuntimeBroker
from tests._helpers.canary_admission import admit_canary_pairing

from ._support import _RTH_MS, _WIN_START_MS, _bar, _green_bar, _red_bar, _wait_for


@pytest.mark.asyncio
async def test_real_trade_runner_routes_enter_and_exit_through_sqlite_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admit_canary_pairing(monkeypatch, "deployment_validation", "PA-TEST")
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


@pytest.mark.asyncio
async def test_sqlite_trade_bot_records_every_evaluated_bar_for_panel_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
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
        # Slice 2 (#1728): bar_ref is now "decision-bar:{feed_id}:{symbol}:
        # {bar_close_ms}", not the old "SYMBOL@ms" string -- assert the new
        # shape/meaning directly (feed_id + symbol + the closed bar's own
        # close timestamp), not a value copied from the current build's
        # output.
        assert [fact["bar_ref"] for fact in facts] == [
            f"decision-bar:ibkr:SPY:{_RTH_MS + offset * 60_000 + 60_000}" for offset in range(3)
        ]
        # decision_id/intent_id are now evaluation_id (decision_id ==
        # evaluation_id, PRD section 16) -- a content-addressed SHA-256, not
        # a "{ms}:{KIND}" string. "deployment_validation" is now a
        # registered Signal Program (issue #1730 Slice 5), so its
        # evaluation_id comes from the real SignalSession's own trace. The
        # `_generic_evaluation_id` compatibility formula it used to fall back
        # to is gone: the live adapter builds only registered programs, so
        # there is no evaluator left to need it. Recompute it by replaying
        # the exact same three bars through a fresh instance of the same
        # registered program, so this proves the identity really is the
        # deterministic per-bar SignalSession evaluation id, not just
        # whatever hash the current build happens to emit.
        expected_registration = _STRATEGY_REGISTRY["deployment_validation"]
        assert expected_registration.signal_program_factory is not None
        expected_program = expected_registration.signal_program_factory(
            expected_registration.param_schema(symbol="SPY")
        )
        expected_strategy = expected_program.strategy
        expected_context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
        expected_strategy.ctx = expected_context
        expected_strategy.initialize()
        expected_ids = []
        for offset in range(3):
            engine_bar = bot_trade_strategy._engine_bar(_bar(_RTH_MS + offset * 60_000))
            expected_context.current_time_ms = engine_bar.end_ms
            expected_context.portfolio.update_reference_price(engine_bar.symbol, engine_bar.close)
            stage = expected_program.session.advance(engine_bar, mode=EvaluationMode.DECIDE)
            expected_program.session.settle(Settlement.COMMIT)
            expected_ids.append(stage.trace.evaluation_id)
        for evaluation_id in expected_ids:
            assert re.fullmatch(r"[0-9a-f]{64}", evaluation_id)
        assert [fact["decision_id"] for fact in facts] == expected_ids
        assert decisions[1].intent_id == expected_ids[1]
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
async def test_sqlite_trade_bot_does_not_label_an_uncertain_effect_as_entered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(effect_state="uncertain", repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(effect_state="rejected", repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
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
    """Slice 2 (#1728) moved decision-receipt capture off the old
    provisional/final two-step pattern (a ``SqliteDecisionReceipts.append``
    call bracketing the broker request) onto one atomic write:
    ``append_atomic_decision_receipt_row``, committed by
    ``ClerkSqliteRepository._commit_transition_row`` inside the SAME SQLite
    transaction as the ENTER/EXIT custody transition, before any broker
    contact (PRD section 16; FR-018). This test used to fault-inject
    ``SqliteDecisionReceipts.append`` -- a boundary the new atomic path never
    calls for an effect-bearing decision, so the injected fault was inert and
    the test only ever timed out. Retargeting the fault at the real boundary
    re-proves the original R1 invariant: if the atomic decision receipt
    cannot be durably written, the whole transaction (including the custody
    transition) rolls back and the broker is never contacted -- using the
    real ``SqliteAlpacaClerkFacade``/``ClerkSqliteRepository``, not the
    ``_FakeClerk`` double (which never exercises the atomic write path at
    all)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    broker = _SqliteRuntimeBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    admit_canary_pairing(monkeypatch, "deployment_validation", "PA-TEST")

    def raise_atomic_receipt_failure(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("injected atomic decision receipt failure")

    monkeypatch.setattr(
        "app.broker.alpaca.clerk.sqlite.repository.append_atomic_decision_receipt_row",
        raise_atomic_receipt_failure,
    )
    feed = _FakeFeed(
        [_green_bar(_WIN_START_MS + 60_000), _green_bar(_WIN_START_MS + 120_000)],
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
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: not registry.status("alpaca", _SID).running)

        # No broker contact: R1 held even after the fault moved to the new
        # atomic boundary.
        assert broker.orders == {}
        # No orphaned custody effect: ENTER_ACCEPTED, its fold, and the
        # atomic receipt are one SQLite transaction -- the failed receipt
        # write rolled the whole thing back, not just itself.
        assert not any(row["transition_kind"] == "ENTER_ACCEPTED" for row in repo.custody_transitions())
        recorded_outcomes = [
            receipt.outcome
            for receipt in repo.decision_receipt_tail(strategy_instance_id=_SID, limit=10)
        ]
        assert "enter_intent" not in recorded_outcomes
        outcome = registry.status("alpaca", _SID).duty_outcome
        assert outcome is not None
        assert outcome.kind == "CRASHED"
        # Pin the crash to the injected OSError specifically, not to some
        # unrelated failure that would make the assertions above vacuous.
        assert outcome.reason_code == "OSError"
    finally:
        set_alpaca_clerk(None)
        repo.close()

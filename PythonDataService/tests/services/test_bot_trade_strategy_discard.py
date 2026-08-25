"""The live adapter's decision cycle is always present, so refusing is total.

Before the staged Signal Session protocol (#1730) a strategy mutated its own
position state at signal *emission*, so a refused candidate needed a
compensating rollback and the adapter carried ``rollback_blocked_entry`` /
``rollback_blocked_exit`` callables to apply it. Position custody now moves only
inside ``commit_signal_decision``, which a session runs only on
``Settlement.COMMIT``, so a refusal has nothing to unwind and DISCARD is the
whole disposition.

That leaves one invariant holding the simplification up: every evaluation the
adapter yields owns a decision cycle to settle. It holds because
``_build_signal_strategy`` refuses to construct a runtime for a strategy with no
registered Signal Program -- without a session there is no stage, so such a
runtime could only stream bars and decide nothing, which is the failure shape
this module works hardest to make impossible. These tests pin both halves: the
refusal is forwarded as DISCARD, and the program-less runtime is unconstructable
rather than merely unused.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
)
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import Settlement
from app.marketdata.feed import MarketDataBar
from app.services import bot_trade_strategy as bts
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy import (
    StrategyEvaluation,
    _build_signal_strategy,
    _discard_evaluation,
    supported_alpaca_paper_strategy_keys,
)

_BAR_END_MS = 1_711_641_600_000


def _evaluation(settle_stage: object) -> StrategyEvaluation:
    return StrategyEvaluation(
        bar=MarketDataBar(
            symbol="SPY",
            start_ms=_BAR_END_MS - 60_000,
            end_ms=_BAR_END_MS,
            open=Decimal("500.00"),
            high=Decimal("500.50"),
            low=Decimal("499.50"),
            close=Decimal("500.25"),
            volume=1_000,
            fetched_at_ms=_BAR_END_MS,
            feed_id="test",
        ),
        evaluation_id="evaluation-under-test",
        decision_bar_close_ms=_BAR_END_MS,
        intents=(
            SignalIntent(
                kind=SignalIntentKind.ENTER,
                bar_close_ms=_BAR_END_MS,
                intended_price=Decimal("500.25"),
            ),
        ),
        settle_stage=settle_stage,  # type: ignore[arg-type]
    )


def test_discard_settles_the_staged_candidate_as_discarded() -> None:
    settled: list[Settlement] = []

    _discard_evaluation(_evaluation(settled.append))

    assert settled == [Settlement.DISCARD]


def _key_without_a_signal_program() -> str:
    """Derive a program-less key from the registry rather than naming one.

    Hand-naming a strategy here would make this test quietly stop covering
    anything the day that strategy is promoted to a Signal Program.
    """
    live = supported_alpaca_paper_strategy_keys()
    return next(key for key in sorted(_STRATEGY_REGISTRY) if key not in live)


def test_building_a_live_runtime_refuses_a_strategy_with_no_signal_program() -> None:
    key = _key_without_a_signal_program()

    with pytest.raises(ValueError, match="not live-executable"):
        _build_signal_strategy(key, "SPY", None)


# ── F19 boundary: runner EXIT path honors the refusal taxonomy ───────────────


def _binding() -> BrokerBotBinding:
    # Same factory as tests/broker/alpaca/clerk/sqlite/test_runtime.py:107-121.
    return BrokerBotBinding(
        strategy_instance_id="spy-bot",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id="PA-TEST",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=1,
    )


@dataclass
class _StubBar:
    feed_id: str = "test-feed"


class _StubEvaluation:
    evaluation_id = "eval-refusal-1"
    decision_bar_close_ms = 1_700_000_000_000
    bar = _StubBar()
    # Matches StrategyEvaluation.trace (default None): _append_decision_receipt
    # reads it to capture the per-bucket trace digest (Direction 2).
    trace = None

    def __init__(self) -> None:
        self.settlements: list[object] = []

    def settle_stage(self, settlement: object) -> None:
        self.settlements.append(settlement)


class _RecorderReceipts:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **kwargs: object) -> None:
        self.rows.append(kwargs)


def _refusal(reason_code: str) -> AdmissionBlockedError:
    return AdmissionBlockedError(
        CapabilityDecision(
            allowed=False,
            capability=Capability.REDUCE,
            reason_code=reason_code,
            why="test refusal",
        )
    )


def test_dispose_transient_exit_refusal_discards_and_records_blocked_receipt() -> None:
    receipts = _RecorderReceipts()
    evaluation = _StubEvaluation()

    bts._dispose_transient_exit_refusal(
        receipts,
        binding=_binding(),
        evaluation=evaluation,
        exc=_refusal(BROKER_SNAPSHOT_STALE_REASON_CODE),
    )

    assert evaluation.settlements == [bts.Settlement.DISCARD]
    assert len(receipts.rows) == 1
    assert receipts.rows[0]["outcome"] == "blocked"
    assert receipts.rows[0]["facts"]["reason_code"] == BROKER_SNAPSHOT_STALE_REASON_CODE


def test_dispose_transient_exit_refusal_reraises_terminal_refusals() -> None:
    receipts = _RecorderReceipts()
    evaluation = _StubEvaluation()

    with pytest.raises(AdmissionBlockedError):
        bts._dispose_transient_exit_refusal(
            receipts,
            binding=_binding(),
            evaluation=evaluation,
            exc=_refusal("UNKNOWN_FUTURE_CODE"),
        )

    assert evaluation.settlements == []
    assert receipts.rows == []

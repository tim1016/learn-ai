"""Live-time capture: decision receipts carry the canonical per-bucket trace digest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import _append_pre_custody_refusal
from app.engine.strategy.signal_program import EvaluationMode, EvaluationTrace, trace_root
from app.marketdata.feed import MarketDataBar
from app.services.bot_trade_strategy import StrategyEvaluation, _append_decision_receipt
from tests.services.test_candidate_uncaptured_at_crash import _binding

_T0 = 1_700_000_000_000
_EVAL_ID = "ab" * 32


def _trace() -> EvaluationTrace:
    return EvaluationTrace(
        program_key="ema_crossover_signal",
        program_version="v1",
        evaluation_id=_EVAL_ID,
        bar_close_ms=_T0 + 900_000,
        bar_qualified=True,
        bucket_closed=True,
        ready=True,
        relation_facts={},
        signal_facts={},
        staged_candidate=None,
        reason_evidence={},
        action_plan_request=None,
        evaluation_mode=EvaluationMode.DECIDE,
    )


def _evaluation(trace: EvaluationTrace | None) -> StrategyEvaluation:
    bar = MarketDataBar(
        symbol="SPY", start_ms=_T0, end_ms=_T0 + 60_000,
        open=Decimal("400"), high=Decimal("401"), low=Decimal("399"), close=Decimal("400.5"),
        volume=100, fetched_at_ms=_T0 + 60_500, feed_id="fake-phase", session_phase="RTH",
    )
    return StrategyEvaluation(
        bar=bar,
        evaluation_id=_EVAL_ID,
        decision_bar_close_ms=_T0 + 900_000,
        intents=(),
        settle_stage=lambda _settlement: None,
        trace=trace,
    )


class _CapturingReceipts:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    def append(self, **kwargs) -> None:
        self.appended.append(kwargs)


def test_append_decision_receipt_captures_trace_digest_and_bucket_close() -> None:
    receipts = _CapturingReceipts()
    trace = _trace()

    _append_decision_receipt(
        receipts,  # type: ignore[arg-type] -- duck-typed capture double
        binding=_binding(run_id="run-1"),
        evaluation=_evaluation(trace),
        outcome="no_action",
        reason_code="NO_ACTION",
    )

    facts = receipts.appended[0]["facts"]
    assert facts["trace_digest"] == trace_root([trace])
    assert facts["decision_bar_close_ms"] == _T0 + 900_000


def test_append_decision_receipt_omits_digest_for_a_traceless_evaluation() -> None:
    receipts = _CapturingReceipts()

    _append_decision_receipt(
        receipts,  # type: ignore[arg-type]
        binding=_binding(run_id="run-1"),
        evaluation=_evaluation(None),
        outcome="no_action",
        reason_code="NO_ACTION",
    )

    assert "trace_digest" not in receipts.appended[0]["facts"]


def test_pre_custody_refusal_receipt_carries_the_evidence_digest(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-CAP", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id="bot-a", symbol="SPY", config_hash="c1")

    _append_pre_custody_refusal(
        repo,
        strategy_instance_id="bot-a",
        run_id="run-1",
        evidence=EffectDecisionEvidence(
            evaluation_id=_EVAL_ID,
            bar_ref="decision-bar:fake-phase:SPY:1700000900000",
            symbol="SPY",
            outcome="enter_intent",
            observed_at_ms=_T0,
            trace_digest="cd" * 32,
            decision_bar_close_ms=_T0 + 900_000,
        ),
        reason_code="MARKET_LIVENESS_BLOCKED",
        explanation="stale evidence at intake",
    )

    rows = SqliteDecisionReceipts(repo, strategy_instance_id="bot-a").retained_window()
    facts = json.loads(rows[-1].facts_json)
    assert facts["trace_digest"] == "cd" * 32
    assert facts["decision_bar_close_ms"] == _T0 + 900_000

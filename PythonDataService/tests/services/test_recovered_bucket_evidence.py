"""A recovered bucket executes against the last minute it holds (#2909).

A bucket whose closing minute never arrives still decides — LEAN's
consolidator stamps it at its period boundary — so its decision close names
a minute that no bar ever closed at. Both runners resolve their decision
evidence through ``_decision_bar_evidence`` and their strike anchor through
``_the_money``; without a fallback those exact-close lookups miss, and the
Clerk refuses the effect as ``SIMULATED_SOURCE_BAR_UNPROVEN`` even though
the strategy priced the bucket from a real, retained minute. The evidence
for a recovered bucket is that minute: the newest retained close at or
before the boundary, which a fired bucket guarantees to be one of its own
minutes (a bucket only exists once a bar arrived inside it).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import EvaluationMode, EvaluationTrace
from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy import (
    StrategyEvaluation,
    _decision_bar_evidence,
    _the_money,
)
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

#: A 64-hex evaluation id, as ``EffectDecisionEvidence`` requires.
_EVALUATION_ID = "a" * 64

#: A 15-minute bucket 10:00–10:15 ET inside an ordinary session. Minutes are
#: appended by their closing instants; ``_LAST_MINUTE_END_MS`` is the 10:13
#: minute's close — the last one that arrives.
_BUCKET_START_MS = 1_711_643_600_000  # 10:01 ET close of the first minute
_BUCKET_CLOSE_MS = _BUCKET_START_MS + 15 * 60_000
_LAST_MINUTE_END_MS = _BUCKET_CLOSE_MS - 60_000


def _minute(end_ms: int, *, close: str = "500.10", volume: int = 900) -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=Decimal("500.00"),
        high=Decimal("500.50"),
        low=Decimal("499.50"),
        close=Decimal(close),
        volume=volume,
        fetched_at_ms=end_ms,
        feed_id="fake",
    )


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS,
        strategy_instance_id="sid-recovered",
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id="PA-TEST",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-recovered",
        created_at_ms=_BUCKET_START_MS,
    )


def _recovered_evaluation(decision_close_ms: int) -> StrategyEvaluation:
    """An evaluation whose decision fired on the next bucket's first bar.

    ``bar`` is the firing bar (the minute that arrived after the bucket), as
    the runner builds it; the decision close is the recovered bucket's
    boundary.
    """
    return StrategyEvaluation(
        bar=_minute(decision_close_ms + 60_000),
        evaluation_id=_EVALUATION_ID,
        decision_bar_close_ms=decision_close_ms,
        intents=(
            SignalIntent(
                kind=SignalIntentKind.ENTER,
                bar_close_ms=decision_close_ms,
                intended_price=Decimal("500.10"),
            ),
        ),
        settle_stage=lambda _settlement: None,
        evaluation_mode=EvaluationMode.DECIDE,
        trace=EvaluationTrace(
            program_key="ema_crossover_signal",
            program_version="ema-crossover-signal/v1",
            evaluation_id=_EVALUATION_ID,
            bar_close_ms=decision_close_ms,
            bar_qualified=True,
            bucket_closed=True,
            ready=True,
            relation_facts={},
            signal_facts={},
            staged_candidate="enter",
            reason_evidence={},
            action_plan_request=None,
            evaluation_mode=EvaluationMode.DECIDE,
        ),
    )


def _ledger(tmp_path: Path, bars: list[MarketDataBar]) -> SourceBarLedger:
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-TEST")
    for bar in bars:
        ledger.append(bar, run_id="run-recovered")
    return ledger


def test_a_recovered_buckets_evidence_is_its_last_arrived_minute(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [_minute(_BUCKET_START_MS + (i + 1) * 60_000) for i in range(14)])
    evaluation = _recovered_evaluation(_BUCKET_CLOSE_MS)
    binding = _binding()

    retained, evidence = _decision_bar_evidence(
        binding, evaluation, evaluation.intents[0], source_bars=ledger, continuity=None
    )

    # The bucket's own last minute — not None, and not the firing bar either.
    assert retained is not None
    assert retained.end_ms == _LAST_MINUTE_END_MS
    assert evidence.bar_ref == retained.bar_ref
    assert evidence.decision_bar_close_ms == _BUCKET_CLOSE_MS


def test_a_complete_buckets_evidence_is_its_closing_minute(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [_minute(_BUCKET_START_MS + (i + 1) * 60_000) for i in range(15)])
    evaluation = _recovered_evaluation(_BUCKET_CLOSE_MS)
    binding = _binding()

    retained, _ = _decision_bar_evidence(
        binding, evaluation, evaluation.intents[0], source_bars=ledger, continuity=None
    )

    assert retained is not None
    assert retained.end_ms == _BUCKET_CLOSE_MS


def test_the_money_counts_from_the_recovered_minute(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [_minute(_BUCKET_START_MS + (i + 1) * 60_000, close="501.30") for i in range(14)])
    evaluation = _recovered_evaluation(_BUCKET_CLOSE_MS)
    binding = _binding()

    price = _the_money(
        binding,
        evaluation,
        evaluation.intents[0],
        priced=None,  # type: ignore[arg-type]  -- the own-symbol path skips it
        pricing=None,
        source_bars=ledger,
    )

    assert price == Decimal("501.30")


def test_the_money_refuses_a_recovered_minute_with_no_trade(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [_minute(_BUCKET_START_MS + (i + 1) * 60_000, volume=0) for i in range(14)])
    evaluation = _recovered_evaluation(_BUCKET_CLOSE_MS)
    binding = _binding()

    price = _the_money(
        binding,
        evaluation,
        evaluation.intents[0],
        priced=None,  # type: ignore[arg-type]
        pricing=None,
        source_bars=ledger,
    )

    assert price is None

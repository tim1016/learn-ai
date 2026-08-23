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

from decimal import Decimal

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import Settlement
from app.marketdata.feed import MarketDataBar
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

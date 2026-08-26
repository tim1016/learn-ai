"""Live signal-adapter tests: ``strategy_evaluations()`` and
``PauseAwareFeed`` driven directly, without a ``BotTaskRegistry``.

Split from ``tests/services/test_bot_runner.py`` (issue #1737).
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from decimal import Decimal

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import EvaluationMode, Settlement
from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_runtime import PauseAwareFeed
from app.services.bot_trade_strategy import StrategyEvaluation, strategy_evaluations

from .conftest import (
    _EMA_FIRST_ENTER_MS,
    _RTH_MS,
    _T0,
    _bar,
    _ema_parity_bars_through_first_exit,
    _FakeFeed,
)


def _strategy_signal_bars(closes: list[str], *, bar_minutes: int = 1) -> list[MarketDataBar]:
    """One synthetic bar per 15-minute decision window, spaced 15 minutes apart.

    ``bar_minutes`` (default 1) is the bar's own width -- a legacy
    compatibility strategy's ``_on_consolidated_bar`` never checks a
    consolidated bar's width, so a narrow 1-minute source bar landing in an
    otherwise-empty 15-minute bucket is a fine, cheap stand-in for a full
    bucket's worth of source bars. A registered Signal Program is stricter:
    ``SignalSession.advance()`` (``app/engine/strategy/signal_program.py``)
    rejects any consolidated bar whose width isn't exactly the session's
    own ``timeframe_ms`` as ``TIMEFRAME_MISMATCH`` -- a 1-minute source bar
    alone in its bucket produces a 1-minute-wide consolidated bar,
    quarantining every decision clock. The strategies covered here build a
    15-minute decision clock from their default parameters, so pass
    ``bar_minutes=15`` and each source bar alone already spans its full
    decision window.
    """
    width_ms = bar_minutes * 60_000
    return [
        MarketDataBar(
            symbol="SPY",
            start_ms=_RTH_MS + index * 15 * 60_000,
            end_ms=_RTH_MS + index * 15 * 60_000 + width_ms,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=100,
            fetched_at_ms=_RTH_MS + index * 15 * 60_000 + width_ms + 100,
            feed_id="canonical-signal-test",
            session_phase="RTH",
        )
        for index, close in enumerate(closes)
    ]


def _rsi_range_family_closes(bar_count: int = 100) -> list[str]:
    """Deterministic random-walk closes that trigger every RSI-range-family
    strategy's (A/B/C) distinct entry gate at least once (#1700).

    Same synthetic-walk technique as the ENG-008 backtest fixture generator
    (``scripts/fixture_generators/strategy_abc_self_equivalence.py``), tuned
    to a short, cheap bar count for this live-seam smoke test rather than
    ENG-008's own numerical-equivalence receipt.
    """
    rng = random.Random(1700)
    price = 400.0
    closes = []
    for _ in range(bar_count):
        price += rng.gauss(0, 1.2)
        closes.append(f"{price:.2f}")
    return closes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy_key", "closes", "expected_kinds"),
    [
        ("sma_crossover", ["1"] * 30 + ["100", "100"], [SignalIntentKind.ENTER]),
        (
            "rsi_mean_reversion",
            [str(100 - index) for index in range(16)]
            + [str(85 + index * 5) for index in range(18)],
            [SignalIntentKind.ENTER, SignalIntentKind.EXIT],
        ),
        ("spy_strategy_a", _rsi_range_family_closes(), [SignalIntentKind.ENTER, SignalIntentKind.EXIT]),
        ("spy_strategy_b", _rsi_range_family_closes(), [SignalIntentKind.ENTER, SignalIntentKind.EXIT]),
        ("spy_strategy_c", _rsi_range_family_closes(), [SignalIntentKind.ENTER, SignalIntentKind.EXIT]),
    ],
)
async def test_human_override_strategies_emit_canonical_live_intents(
    strategy_key: str,
    closes: list[str],
    expected_kinds: list[SignalIntentKind],
) -> None:
    """Both compatibility strategies (no ``SignalSession``, e.g.
    ``rsi_mean_reversion``) and registered Signal Programs (e.g.
    ``sma_crossover``, issue #1730 Slice 5) appear in this parametrize list.

    Two adaptations keep both families working through the same harness:

    * Bar shape: a registered Signal Program's ``SignalSession.advance()``
      rejects a consolidated bar whose width isn't exactly 15 minutes
      (``TIMEFRAME_MISMATCH``) -- see ``_strategy_signal_bars``'s
      ``bar_minutes`` docstring. Every strategy this test currently covers
      that IS a Signal Program needs ``bar_minutes=15``; strategies not yet
      promoted (PRD Slice 5 has not reached them) keep the cheaper
      1-minute default.
    * Settlement: a Signal Program leaves its stage pending until a runner
      reports an explicit disposition (``evaluation.settle_stage`` is
      non-``None``, mirroring
      ``test_ema_live_adapter_exposes_and_settles_signal_program_stages``'s
      pattern) — an unsettled stage would otherwise quarantine every later
      decision clock (``UNSETTLED_STAGE``). Immediately committing each
      staged evaluation matches ``strategy_intents``' own "no custody seam,
      therefore immediate commit" semantics; it is a no-op for
      compatibility strategies, whose evaluations never carry a stage to
      settle.
    """
    binding = BrokerBotBinding(
        strategy_instance_id=f"{strategy_key}-live-test",
        strategy_key=strategy_key,
        broker="alpaca",
        symbol="SPY",
        mode="dry_run",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-001",
        created_at_ms=_T0,
    )
    is_signal_program = _STRATEGY_REGISTRY[strategy_key].signal_program_factory is not None
    feed = _FakeFeed(_strategy_signal_bars(closes, bar_minutes=15 if is_signal_program else 1), mode="finite")

    kinds: list[SignalIntentKind] = []
    async for evaluation in strategy_evaluations(binding, feed):
        if evaluation.settle_stage is not None:
            evaluation.settle_stage(Settlement.COMMIT)
        kinds.extend(intent.kind for intent in evaluation.intents)

    assert kinds == expected_kinds


@pytest.mark.asyncio
async def test_binding_strategy_params_reach_the_constructed_live_strategy() -> None:
    """#1701: the deploy-time parameter set bound to an instance actually
    changes strategy behavior — it isn't merely accepted and ignored.

    Raising rsi_mean_reversion's overbought threshold well above this bar
    series' RSI range suppresses the EXIT intent the default parameters
    produce, proving ``strategy_params`` flows from the binding into the
    live-constructed strategy via the registry `build` callable (#1700).

    ``rsi_mean_reversion`` is a registered Signal Program (issue #1730 Slice
    5): ``bar_minutes=15`` and an explicit ``settle_stage`` commit are the
    same two harness adaptations
    ``test_human_override_strategies_emit_canonical_live_intents`` documents
    -- see that test's docstring.
    """
    closes = [str(100 - index) for index in range(16)] + [str(85 + index * 5) for index in range(18)]
    bars = _strategy_signal_bars(closes, bar_minutes=15)

    async def kinds_for(strategy_params: dict[str, float]) -> list[SignalIntentKind]:
        binding = BrokerBotBinding(
            strategy_instance_id="rsi-mean-reversion-params-live-test",
            strategy_key="rsi_mean_reversion",
            broker="alpaca",
            symbol="SPY",
            mode="dry_run",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id="run-001",
            created_at_ms=_T0,
            strategy_params=strategy_params,
        )
        feed = _FakeFeed(bars, mode="finite")
        kinds: list[SignalIntentKind] = []
        async for evaluation in strategy_evaluations(binding, feed):
            if evaluation.settle_stage is not None:
                evaluation.settle_stage(Settlement.COMMIT)
            kinds.extend(intent.kind for intent in evaluation.intents)
        return kinds

    assert await kinds_for({}) == [SignalIntentKind.ENTER, SignalIntentKind.EXIT]
    assert await kinds_for({"overbought": 99.9}) == [SignalIntentKind.ENTER]


@pytest.mark.asyncio
async def test_ema_live_adapter_exposes_and_settles_signal_program_stages() -> None:
    """#1727: the live adapter must not silently fall back to legacy EMA dispatch.

    A stage is intentionally left pending until its runner reports an explicit
    disposition.  Advancing through the first ENTER and EXIT proves no staged
    no-action decision stays locked and suppresses the later legitimate intent.
    """
    binding = BrokerBotBinding(
        strategy_instance_id="ema-staged-live-test",
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="dry_run",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-001",
        created_at_ms=_T0,
    )
    evaluation_count = 0
    staged_count = 0
    intents: list[SignalIntentKind] = []
    async for evaluation in strategy_evaluations(
        binding,
        _FakeFeed(_ema_parity_bars_through_first_exit(), mode="finite"),
    ):
        evaluation_count += 1
        if evaluation.settle_stage is not None:
            staged_count += 1
            evaluation.settle_stage(Settlement.COMMIT)
        intents.extend(intent.kind for intent in evaluation.intents)

    assert staged_count == evaluation_count
    assert intents == [SignalIntentKind.ENTER, SignalIntentKind.EXIT]


@pytest.mark.asyncio
async def test_strategy_evaluations_unlocks_each_discarded_signal_stage() -> None:
    """Observe-only and rejected candidates cannot strand the next EMA stage."""
    binding = BrokerBotBinding(
        strategy_instance_id="ema-staged-discard-test",
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="dry_run",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-001",
        created_at_ms=_T0,
    )
    evaluation_count = 0
    staged_count = 0
    async for evaluation in strategy_evaluations(
        binding,
        _FakeFeed(_ema_parity_bars_through_first_exit(), mode="finite"),
    ):
        evaluation_count += 1
        assert evaluation.settle_stage is not None
        staged_count += 1
        evaluation.settle_stage(Settlement.DISCARD)

    assert staged_count == evaluation_count
    assert evaluation_count > 1


def test_live_market_bar_translates_to_numeric_engine_timestamps() -> None:
    from app.services.bot_trade_strategy import _engine_bar

    source = _bar(_RTH_MS)
    engine_bar = _engine_bar(source)

    assert (engine_bar.start_ms, engine_bar.end_ms) == (source.start_ms, source.end_ms)
    assert not any(isinstance(value, datetime) for value in vars(engine_bar).values())


@pytest.mark.asyncio
async def test_pause_aware_feed_progresses_bars_in_observe_only_mode() -> None:
    class _QueueFeed:
        feed_id = "queue"

        def __init__(self) -> None:
            self.queue: asyncio.Queue[MarketDataBar] = asyncio.Queue()

        async def stream_bars(self, _symbol: str, *, use_rth: bool = True):
            while True:
                yield await self.queue.get()

        async def recent_closed_bars(
            self,
            _symbol: str,
            *,
            use_rth: bool = True,
            lookback_days: int = 5,
        ) -> list[MarketDataBar]:
            del use_rth, lookback_days
            return []

    source = _QueueFeed()
    gate = asyncio.Event()
    gate.set()
    feed = PauseAwareFeed(source, gate)
    stream = feed.stream_bars("SPY")

    await source.queue.put(_bar(0))
    assert (await anext(stream)).end_ms == 60_000

    gate.clear()
    await source.queue.put(_bar(100))
    assert (await anext(stream)).end_ms == 60_100
    assert feed.observe_only is True

    gate.set()
    await source.queue.put(_bar(300_000))
    assert (await anext(stream)).end_ms == 360_000
    assert feed.observe_only is False


@pytest.mark.asyncio
async def test_pause_mode_is_captured_at_the_decision_bar_not_sampled_after_continue() -> None:
    """A Continue after the raw close cannot release its paused EMA candidate."""

    gate = asyncio.Event()
    gate.set()

    class _ModeBoundaryFeed(_FakeFeed):
        async def stream_bars(self, symbol: str, *, use_rth: bool = True):
            async for bar in super().stream_bars(symbol, use_rth=use_rth):
                if bar.end_ms == _EMA_FIRST_ENTER_MS:
                    gate.clear()
                elif bar.end_ms > _EMA_FIRST_ENTER_MS:
                    gate.set()
                yield bar

    binding = BrokerBotBinding(
        strategy_instance_id="ema-pause-mode-test",
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="dry_run",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-001",
        created_at_ms=_T0,
    )
    paused_enter: list[StrategyEvaluation] = []
    decided_intents: list[SignalIntentKind] = []
    feed = PauseAwareFeed(
        _ModeBoundaryFeed(_ema_parity_bars_through_first_exit(), mode="finite"),
        gate,
    )

    async for evaluation in strategy_evaluations(binding, feed):
        if evaluation.evaluation_mode is EvaluationMode.OBSERVE_ONLY and evaluation.intents:
            paused_enter.append(evaluation)
        elif evaluation.intents:
            decided_intents.extend(intent.kind for intent in evaluation.intents)
        if evaluation.settle_stage is not None:
            evaluation.settle_stage(Settlement.COMMIT)

    assert [evaluation.intents[0].kind for evaluation in paused_enter] == [SignalIntentKind.ENTER]
    assert decided_intents == []

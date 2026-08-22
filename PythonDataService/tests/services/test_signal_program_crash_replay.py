"""Crash-window replay evidence for sealed Signal Programs (issue #1730).

Proves the acceptance criterion: "Crash-window fixtures prove deterministic
replay, named candidate-without-disposition divergence, idempotent no-action
evidence, and no duplicate broker contact per program."

Every test is parametrized over the DERIVED sealed-program list
(``SEALED_KEYS`` in ``tests/_helpers/signal_program.py``, computed from
``_STRATEGY_REGISTRY``) -- never a hand-written key list: a future promotion
is covered the moment it registers a ``signal_program_factory``.

Bars are fed at the MINUTE level through ``_LiveSignalRuntime.replay_closed_bar``
-- the same seam ``replay_warmup_bars`` (the function under test) itself
drives -- rather than calling ``SignalSession.advance`` directly with
already-consolidated bars the way ``test_signal_program_discard_safety.py``
does. That file can skip the consolidator because it only needs *a* staged
bar shape; this file needs to prove behavior that specifically depends on
``replay_warmup_bars``'s own bar-by-bar minute loop, so it goes through the
real ``TradeBarConsolidator`` (see ``app/engine/consolidators/
trade_bar_consolidator.py``) exactly as ``feed.stream_bars``/
``feed.recent_closed_bars`` do in production. Every strategy is still driven
directly rather than through ``BacktestEngine``, for the same reason that
file gives: these strategies declare fixed backtest windows, so synthetic
bars outside them would be silently filtered out before ever reaching the
indicators.

Each consolidated bucket's close is a term from a seeded
(``numpy.random.default_rng``) random walk with a small positive drift --
reproducible per ``.claude/rules/python.md``, and unlike a monotonic ramp it
produces genuine up-and-down moves so RSI/ADX/Supertrend-gated programs (not
just EMA/SMA crossovers) actually clear their entry conditions. A pure
monotonic ramp pins Wilder's RSI at 100 (no losses to average), which never
clears an ``<= overbought`` gate. Verified empirically against every
currently-registered sealed program that a 230-bucket walk (seed 7) produces
at least one ENTER and one EXIT for each; ``_reference_traces`` asserts this
budget holds rather than silently producing a vacuous test if a future
program needs a longer warmup.

"Broker contact" at this layer is proxied by a recording
``SignalIntentExecutor`` bound to the strategy's own execution boundary
(``StrategyContext.set_signal_intent_executor`` -- the same seam
``SignalSymbolExecutor``/Backtest binds, and the one
``test_signal_program_discard_safety.py`` uses for the same reason). This is
the only concrete execution-facing seam ``replay_warmup_bars`` and
``_LiveSignalRuntime`` expose without also standing up the SQLite Alpaca
Clerk. The live adapter (``bot_trade_strategy.py``) always binds a no-op
stub here (``_RecordingSignalIntentExecutor``) precisely so a *real* broker
is never reachable from this layer at all -- the actual Clerk contact
boundary (``clerk.execute_for_instance``) lives one layer up, in
``run_trade_bot``, gated entirely by what ``replay_warmup_bars`` RETURNS
(``None``, or the one named crash candidate). So every test asserts on BOTH
signals: the function's return value (the authoritative gate on what can
ever reach ``run_trade_bot``) and the recording executor's call count/keys
(a defense-in-depth signal on the strategy's own internal reapplication
path). GAP: a direct proof against ``clerk.execute_for_instance`` itself
would need the full SQLite Clerk integration (``run_trade_bot``'s own test
coverage in ``tests/services/test_bot_runner.py`` exercises that boundary);
it is out of scope for this file, which is scoped to ``replay_warmup_bars``.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np
import pytest

from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationStage,
    EvaluationTrace,
    Settlement,
    StageQuarantine,
    trace_root,
)
from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy import _build_signal_strategy
from app.services.bot_trade_strategy_warmup import replay_warmup_bars
from tests._helpers.signal_program import (
    SEALED_KEYS,
    RecordingExecutor,
    bind_strategy_context,
    bucket,
)

if TYPE_CHECKING:
    from app.services.bot_trade_strategy import _LiveSignalRuntime

_MINUTE_MS = 60_000
# 2024-01-02 09:30 ET (EST, UTC-5) -- a real, documented, unambiguous
# int64-ms-UTC instant (matching the anchor `tests/services/test_bot_runner.py`
# already uses). The consolidator itself does not care about session
# boundaries, only about elapsed America/New_York wall-clock 15-minute
# periods (`_floor_to_period_ms`), so this anchor is arbitrary EXCEPT that
# it must be real and documented -- not an epoch-0 fake.
_ANCHOR_MS = 1_704_205_800_000


def _generate_random_walk_closes(
    *, seed: int, count: int, drift: float = 0.05, vol: float = 1.0, start: float = 500.0
) -> list[str]:
    rng = np.random.default_rng(seed=seed)
    steps = rng.normal(loc=drift, scale=vol, size=count)
    prices = start + np.cumsum(steps)
    return [f"{price:.4f}" for price in prices]


_RANDOM_WALK_COUNT = 230
"""Fired-bucket budget for the "needs a real intent" tests (2, 3, 4). Margin
above the slowest sealed program observed while writing this file
(``spy_strategy_a``, first intent at bucket 207 of this exact seeded walk)."""

# +1: the last close never itself fires a bucket -- it only flushes the
# second-to-last one out of the consolidator's one-input lag (see
# `TradeBarConsolidator.update`'s docstring: "The fired bar is *not* the one
# this input started"). N+1 closes are required to observe N fired buckets.
_RANDOM_WALK_CLOSES = _generate_random_walk_closes(seed=7, count=_RANDOM_WALK_COUNT + 1)

_DETERMINISM_BUCKET_COUNT = 40
"""Smaller, independent budget for the pure-determinism test: it never needs
a real intent (``captured_decisions=None`` discards every bucket regardless
of content), so it stays cheap rather than paying for the full 230-bucket
walk twice per program."""
_DETERMINISM_CLOSES = _generate_random_walk_closes(seed=7, count=_DETERMINISM_BUCKET_COUNT + 1)


class _StaticWarmupFeed:
    """``MarketDataFeed`` double returning a fixed, pre-built minute-bar
    sequence as warmup history. Mirrors ``_RecordingFeed`` in
    ``test_bot_trade_strategy_warmup.py`` except it actually returns bars,
    since these tests need real replay, not just the requested lookback."""

    feed_id = "test-crash-replay"

    def __init__(self, bars: list[MarketDataBar]) -> None:
        self._bars = bars

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        return list(self._bars)


def _binding(*, strategy_key: str, symbol: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="crash-replay-test",
        strategy_key=strategy_key,
        broker="alpaca",
        symbol=symbol,
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan(symbol),
        run_id="run-1",
        created_at_ms=0,
    )


def _resolution_minutes(key: str) -> int:
    registration = _STRATEGY_REGISTRY[key]
    program = registration.signal_program_factory(registration.param_schema())
    width_ms = program.session.timeframe_ms
    assert width_ms % _MINUTE_MS == 0, f"'{key}' declares a non-minute-aligned timeframe: {width_ms}"
    return width_ms // _MINUTE_MS


def _minute_bars_for_closes(symbol: str, resolution_minutes: int, closes: list[str]) -> list[MarketDataBar]:
    """Build a contiguous run of real 1-minute closed bars -- the shape
    ``replay_warmup_bars`` actually consumes. Every minute inside one
    bucket carries the SAME OHLC (a "flat" bucket), so the fired
    consolidated bar's close is exactly ``closes[i]``. The last entry in
    ``closes`` never itself fires (see the module-level docstring on
    ``_RANDOM_WALK_CLOSES``).
    """
    bars: list[MarketDataBar] = []
    cursor = _ANCHOR_MS
    for close in closes:
        price = Decimal(close)
        for _ in range(resolution_minutes):
            end = cursor + _MINUTE_MS
            bars.append(
                MarketDataBar(
                    symbol=symbol,
                    start_ms=cursor,
                    end_ms=end,
                    open=price,
                    high=price + Decimal("1"),
                    low=price - Decimal("1"),
                    close=price,
                    volume=100,
                    fetched_at_ms=end,
                    feed_id="test-crash-replay",
                    session_phase="RTH",
                )
            )
            cursor = end
    return bars


def _fresh_runtime_and_executor(
    key: str, symbol: str
) -> tuple[_LiveSignalRuntime, StrategyContext, RecordingExecutor]:
    """Construct one live-shaped ``_LiveSignalRuntime`` exactly as the live
    adapter's own ``_signal_strategy_evaluations`` does (``_build_signal_strategy``
    then bind context, initialize, bind executor) -- except the executor here
    actually records instead of the adapter's no-op stub.

    The recorded intents are this file's proxy for "broker contact" (see the
    module docstring)."""
    runtime = _build_signal_strategy(key, symbol, None)
    context, executor = bind_strategy_context(runtime.strategy)
    return runtime, context, executor


_REFERENCE_TRACES_CACHE: dict[str, list[EvaluationTrace]] = {}


async def _reference_traces(key: str, symbol: str, resolution_minutes: int) -> list[EvaluationTrace]:
    """Run ``replay_warmup_bars`` once, fresh, with no captured decisions
    (every bucket DISCARDed), and cache the resulting trace corpus per key.

    This is a reference pass, not the function under test in the sense that
    matters here: every later test treats its output as ground truth for
    deriving real ``evaluation_id``s/intents/bar-close identities without
    hardcoding content hashes. That is only sound because
    ``test_replay_warmup_bars_is_deterministic_across_two_fresh_instances``
    below independently proves two fresh instances reproduce this corpus
    identically -- so reusing one already-computed corpus across the other
    tests costs time, not correctness. Caching matters here: a full
    230-bucket walk is 231 * resolution_minutes minute-bar drains per
    program, and three other tests would otherwise each pay for it again.
    """
    cached = _REFERENCE_TRACES_CACHE.get(key)
    if cached is not None:
        return cached
    minute_bars = _minute_bars_for_closes(symbol, resolution_minutes, _RANDOM_WALK_CLOSES)
    runtime, context, _executor = _fresh_runtime_and_executor(key, symbol)
    binding = _binding(strategy_key=key, symbol=symbol)
    recovered = await replay_warmup_bars(
        runtime, context, _StaticWarmupFeed(minute_bars), binding, captured_decisions=None
    )
    assert recovered is None, f"reference pass for '{key}' unexpectedly flagged a crash candidate"
    assert runtime.program is not None
    traces = runtime.program.session.traces
    assert len(traces) == _RANDOM_WALK_COUNT, (
        f"'{key}' fired {len(traces)} buckets over the reference walk, expected {_RANDOM_WALK_COUNT}"
    )
    assert any(trace.staged_candidate is not None for trace in traces), (
        f"'{key}' never proposed a single intent within {_RANDOM_WALK_COUNT} synthetic buckets -- "
        "the random-walk budget in this file is not generous enough for this program; widen "
        "_RANDOM_WALK_COUNT rather than let the tests below pass vacuously"
    )
    _REFERENCE_TRACES_CACHE[key] = traces
    return traces


# ---------------------------------------------------------------------------
# 1. Deterministic replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SEALED_KEYS)
async def test_replay_warmup_bars_is_deterministic_across_two_fresh_instances(key: str) -> None:
    """Replaying the identical bar sequence from two independent, freshly
    constructed instances of the same program must yield an identical trace
    corpus (``trace_root``). ``captured_decisions=None`` means every bucket
    here is DISCARDed (an empty/``None`` map never flags a crash candidate,
    per ``replay_warmup_bars``'s own docstring), which isolates pure replay
    determinism from the crash-window branch covered separately below --
    and, unlike the other tests in this file, this one must NOT go through
    the shared ``_reference_traces`` cache: it exists specifically to prove
    two independent calls agree, so reusing one call's output for both
    sides would make the comparison vacuous.
    """
    registration = _STRATEGY_REGISTRY[key]
    symbol = registration.param_schema().symbol
    resolution_minutes = _resolution_minutes(key)
    bars = _minute_bars_for_closes(symbol, resolution_minutes, _DETERMINISM_CLOSES)
    binding = _binding(strategy_key=key, symbol=symbol)

    runtime_a, context_a, _executor_a = _fresh_runtime_and_executor(key, symbol)
    recovered_a = await replay_warmup_bars(
        runtime_a, context_a, _StaticWarmupFeed(bars), binding, captured_decisions=None
    )
    runtime_b, context_b, _executor_b = _fresh_runtime_and_executor(key, symbol)
    recovered_b = await replay_warmup_bars(
        runtime_b, context_b, _StaticWarmupFeed(bars), binding, captured_decisions=None
    )

    assert recovered_a is None
    assert recovered_b is None
    assert runtime_a.program is not None
    assert runtime_b.program is not None
    traces_a = runtime_a.program.session.traces
    traces_b = runtime_b.program.session.traces
    assert traces_a, f"'{key}' produced no traces at all -- nothing to prove deterministic"
    assert len(traces_a) == _DETERMINISM_BUCKET_COUNT
    assert len(traces_b) == _DETERMINISM_BUCKET_COUNT
    assert trace_root(traces_a) == trace_root(traces_b)


# ---------------------------------------------------------------------------
# 2. Idempotent no-action: a captured, non-commit-worthy candidate never
#    contacts the broker-facing executor on replay.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SEALED_KEYS)
async def test_replay_warmup_bars_is_idempotent_for_an_already_blocked_candidate(key: str) -> None:
    """A bucket whose candidate was already durably captured with a
    NON-commit outcome (here: ``"blocked"`` -- the liveness gate refused it,
    so it was proposed but never acted on) must replay as pure no-action:
    ``replay_warmup_bars`` must not re-flag it as a crash candidate (it
    already has a known disposition), and the strategy's own execution
    boundary must never be touched, because ``"blocked"`` is not in
    ``_COMMIT_WORTHY_OUTCOMES``.
    """
    registration = _STRATEGY_REGISTRY[key]
    symbol = registration.param_schema().symbol
    resolution_minutes = _resolution_minutes(key)

    reference_traces = await _reference_traces(key, symbol, resolution_minutes)
    target_index = next(i for i, trace in enumerate(reference_traces) if trace.staged_candidate is not None)
    captured = {trace.evaluation_id: "no_action" for trace in reference_traces[:target_index]}
    captured[reference_traces[target_index].evaluation_id] = "blocked"

    replay_bars = _minute_bars_for_closes(symbol, resolution_minutes, _RANDOM_WALK_CLOSES[: target_index + 2])
    runtime, context, executor = _fresh_runtime_and_executor(key, symbol)
    binding = _binding(strategy_key=key, symbol=symbol)
    recovered = await replay_warmup_bars(
        runtime, context, _StaticWarmupFeed(replay_bars), binding, captured_decisions=captured
    )

    assert recovered is None, f"'{key}' re-flagged an already-captured (blocked) candidate as a crash window"
    assert executor.intents == [], (
        f"'{key}' contacted the broker-facing executor while replaying an already-captured, "
        f"non-commit-worthy bucket: {executor.intents}"
    )


# ---------------------------------------------------------------------------
# 3. No duplicate broker contact: warmup replay followed by the live loop's
#    own resumption, including an overlapping already-replayed bucket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SEALED_KEYS)
async def test_warmup_replay_then_live_loop_never_duplicates_broker_contact(key: str) -> None:
    """A clean-restart warmup (every bucket already durably captured with
    its TRUE outcome -- no crash) followed by the live loop's own
    continuation must never contact the broker-facing executor more than
    once for the same decision, even when the live loop's own resumption
    re-offers an already-replayed bucket (the "overlap" the criterion
    names). ``SignalSession.advance``'s monotonic-clock guard is what makes
    this true regardless of how a bucket reaches it (consolidator-fired
    during warmup, or a bare ``TradeBar`` here) -- proven directly against
    the same session object warmup just left its clock on.
    """
    registration = _STRATEGY_REGISTRY[key]
    symbol = registration.param_schema().symbol
    resolution_minutes = _resolution_minutes(key)
    width_ms = resolution_minutes * _MINUTE_MS

    reference_traces = await _reference_traces(key, symbol, resolution_minutes)
    warmup_end = next(i for i, trace in enumerate(reference_traces) if trace.staged_candidate is not None) + 1
    captured = {
        trace.evaluation_id: ("entered" if trace.staged_candidate == "ENTER" else "exited")
        if trace.staged_candidate is not None
        else "no_action"
        for trace in reference_traces[:warmup_end]
    }
    warmup_bars = _minute_bars_for_closes(symbol, resolution_minutes, _RANDOM_WALK_CLOSES[: warmup_end + 1])

    runtime, context, executor = _fresh_runtime_and_executor(key, symbol)
    binding = _binding(strategy_key=key, symbol=symbol)
    recovered = await replay_warmup_bars(
        runtime, context, _StaticWarmupFeed(warmup_bars), binding, captured_decisions=captured
    )
    assert recovered is None, f"'{key}' flagged a crash candidate in a fully-captured warmup window"
    assert runtime.program is not None
    session = runtime.program.session

    last_bar_close_ms = reference_traces[warmup_end - 1].bar_close_ms

    # The live loop's own resumption re-offers a bucket already consumed
    # during warmup replay -- the exact "overlap" the criterion names.
    overlap_bar = bucket(symbol, last_bar_close_ms - width_ms, last_bar_close_ms, "500")
    quarantined = session.advance(overlap_bar, mode=EvaluationMode.DECIDE)
    assert isinstance(quarantined, StageQuarantine), (
        f"'{key}' re-decided an already-replayed bucket instead of refusing it: {quarantined}"
    )
    assert quarantined.reason == "NON_MONOTONIC_DECISION_CLOCK"

    # Genuinely new bars: the live loop's ordinary decide-and-commit path.
    for offset, close in enumerate(_RANDOM_WALK_CLOSES[warmup_end : warmup_end + 20]):
        end_ms = last_bar_close_ms + (offset + 1) * width_ms
        new_bar = bucket(symbol, end_ms - width_ms, end_ms, close)
        stage = session.advance(new_bar, mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine), f"'{key}' quarantined a genuinely new bucket: {stage}"
        session.settle(Settlement.COMMIT)

    contact_counts = Counter(intent.bar_close_ms for intent in executor.intents)
    assert contact_counts, f"'{key}' never produced a single broker contact to prove non-duplication against"
    assert max(contact_counts.values()) == 1, (
        f"'{key}' contacted the broker-facing executor more than once for the same decision "
        f"(keyed by bar_close_ms): {contact_counts}"
    )


# ---------------------------------------------------------------------------
# 4. Candidate-without-disposition is named, not dropped or re-executed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", SEALED_KEYS)
async def test_replay_warmup_bars_names_the_candidate_without_disposition(key: str) -> None:
    """The crash window: a bucket ``SignalSession.advance`` staged but that
    has no known disposition (the process died after staging, before Clerk
    intake captured it -- FR-016) must be returned by ``replay_warmup_bars``
    as the one named candidate, and never silently dropped or re-executed.
    """
    registration = _STRATEGY_REGISTRY[key]
    symbol = registration.param_schema().symbol
    resolution_minutes = _resolution_minutes(key)

    reference_traces = await _reference_traces(key, symbol, resolution_minutes)
    target_index = next(i for i, trace in enumerate(reference_traces) if trace.staged_candidate is not None)
    target_trace = reference_traces[target_index]
    # Every bucket BEFORE the crash window is durably captured; the target
    # bucket's own evaluation_id is deliberately OMITTED.
    captured = {trace.evaluation_id: "no_action" for trace in reference_traces[:target_index]}

    replay_bars = _minute_bars_for_closes(symbol, resolution_minutes, _RANDOM_WALK_CLOSES[: target_index + 2])
    runtime, context, executor = _fresh_runtime_and_executor(key, symbol)
    binding = _binding(strategy_key=key, symbol=symbol)
    recovered: tuple[MarketDataBar, EvaluationStage] | None = await replay_warmup_bars(
        runtime, context, _StaticWarmupFeed(replay_bars), binding, captured_decisions=captured
    )

    assert recovered is not None, (
        f"'{key}' silently dropped a staged candidate with no known disposition instead of "
        "naming it as the FR-016 crash window"
    )
    # The first tuple element is the MINUTE bar whose arrival caused the
    # consolidator to fire the staged bucket -- one minute AFTER the
    # decision bucket's own close, per `TradeBarConsolidator`'s one-input
    # lag (see the module docstring) -- so it is not itself asserted on
    # here; the decision bucket's identity is `candidate_stage.trace`.
    _triggering_minute_bar, candidate_stage = recovered
    assert not isinstance(candidate_stage, StageQuarantine)
    assert candidate_stage.trace.evaluation_id == target_trace.evaluation_id
    assert candidate_stage.trace.bar_close_ms == target_trace.bar_close_ms
    assert executor.intents == [], (
        f"'{key}' silently re-executed the uncaptured crash candidate instead of only naming it: "
        f"{executor.intents}"
    )

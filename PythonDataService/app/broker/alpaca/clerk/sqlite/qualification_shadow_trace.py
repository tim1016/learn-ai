"""Shadow evaluation: canonical trace parity proof with zero broker contact.

Issue #1729 AC #2: "Shadow mode compares canonical traces without broker
contact and fails closed on the first divergent authority edge." PRD
``docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`` Slice 3 names
this as a distinct deliverable from the Paper coordinator itself: before the
governed Paper adapter is trusted with one strategy/account canary, its
session must be proven to emit the same canonical `EvaluationTrace` sequence
as the already-qualified reference path, over the same bars, with no broker
in the loop at all.

Two authorities compute the trace for the identical qualified bars:

* **reference** -- the already-qualified path. `registration.build(params)`
  driven by `BacktestEngine`, exactly as
  ``tests/engine/strategy/test_ema_signal_program.py``
  ::test_validated_ema_settings_corpus_has_a_pinned_trace_root`` already
  proves equals the registry's sealed ``golden_trace_root``.
* **observed** -- the live-adapter path. `registration.signal_program_factory`
  driven by ``app.services.bot_trade_strategy.strategy_evaluations``, the
  exact shared session-construction seam Paper's ``run_trade_bot`` and Dry
  Run's ``run_dry_run_bot`` already use (see those functions' shared call
  into ``_signal_strategy_evaluations`` / ``_build_signal_strategy``).

Both paths are Clerk- and broker-free by construction: `BacktestEngine.run`
never receives a broker port, and `strategy_evaluations` never imports or
calls `get_alpaca_clerk` / `get_clerk_runtime` -- the Clerk is reached only
*after* an evaluation is yielded, inside `run_trade_bot`/`run_dry_run_bot`,
neither of which this module calls. ``_QualifiedBarFeed`` below is a
controlled, in-memory port (no network I/O) in the same family as
``qualification_polygon_replay.PolygonReplayMarketDataFeed``.

``compare_canonical_traces`` fails closed: it stops at the first field, in
the first trace, where the two authorities disagree, and raises naming that
exact edge. It never accumulates a diff report -- a canary program that
cannot prove trace parity is refused outright, not admitted with a warning
(PRD §22.1 item 3, Slice 3 "shadow trace comparison with no broker contact").
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import (
    _STRATEGY_REGISTRY,
    StrategyRegistration,
)
from app.engine.strategy.signal_program import EvaluationTrace, Settlement, trace_root
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy import strategy_evaluations
from app.services.spec_strategy_runner import InMemoryDataReader
from app.utils.timestamps import now_ms_utc

SHADOW_TRACE_FEED_ID = "qualification_shadow_trace"
_SHADOW_RUN_ID = "shadow-trace-run"


class UnsupportedShadowProgramError(ValueError):
    """``strategy_key`` names no registered Signal Program to shadow-evaluate.

    Only a strategy with a ``signal_program_factory`` produces the canonical
    `EvaluationTrace` this comparison needs; compatibility-mode strategies
    (e.g. ``deployment_validation``) have no SignalSession to trace.
    """


@dataclass(frozen=True)
class ShadowTraceDivergence:
    """The first point where the reference and observed trace disagree."""

    index: int
    evaluation_id: str | None
    field: str
    expected: Any
    observed: Any


class ShadowTraceDivergenceError(RuntimeError):
    """Fails closed: the canary path is refused, not merely warned about."""

    def __init__(self, divergence: ShadowTraceDivergence) -> None:
        self.divergence = divergence
        super().__init__(
            f"Shadow trace diverged at index {divergence.index} "
            f"(evaluation_id={divergence.evaluation_id!r}): field "
            f"{divergence.field!r} expected={divergence.expected!r} "
            f"observed={divergence.observed!r}"
        )


@dataclass(frozen=True)
class ShadowTraceEvaluation:
    """Canonical trace-parity proof for one qualified bar sequence."""

    strategy_key: str
    symbol: str
    compared_count: int
    trace_root: str
    traces: tuple[EvaluationTrace, ...]


class _QualifiedBarFeed:
    """In-memory ``MarketDataFeed`` streaming a fixed, pinned bar sequence.

    Controlled port with no network or broker I/O -- the same role
    ``qualification_polygon_replay.PolygonReplayMarketDataFeed`` plays for
    the Polygon-replay qualification suite, but streaming already-consolidated
    ``TradeBar``s directly instead of synthesizing 5-second prints. Never
    exposes an ``evaluation_mode_for`` attribute, so
    ``bot_trade_strategy._evaluation_mode_for`` falls back to
    ``EvaluationMode.DECIDE`` for every bar -- the same mode
    ``SignalProgram.activate_for_backtest`` uses for the
    reference path, so the two sides are comparable on that field too.
    """

    feed_id = SHADOW_TRACE_FEED_ID

    def __init__(self, symbol: str, bars: Sequence[TradeBar]) -> None:
        self._symbol = symbol
        self._bars = tuple(bars)

    @property
    def capability_account_id(self) -> str | None:
        return None

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]:
        del use_rth  # the qualified corpus is already RTH-filtered
        for bar in self._bars:
            if bar.symbol != symbol:
                continue
            yield MarketDataBar(
                symbol=bar.symbol,
                start_ms=bar.start_ms,
                end_ms=bar.end_ms,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                fetched_at_ms=bar.end_ms,
                feed_id=self.feed_id,
                session_phase="RTH",
            )

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        # The shadow harness proves exact reproduction of a pinned qualified
        # bar sequence; it deliberately never grows extra warmup history the
        # caller didn't supply (mirrors PolygonReplayMarketDataFeed).
        del symbol, use_rth, lookback_days
        return []

    def health(self, symbol: str | None = None) -> FeedHealth:
        del symbol
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._bars[-1].end_ms if self._bars else None,
            reason="",
            active_subscription_count=1,
            observed_at_ms=now_ms_utc(),
        )


def _registered_signal_program(strategy_key: str) -> StrategyRegistration:
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None or registration.signal_program_factory is None:
        raise UnsupportedShadowProgramError(
            f"{strategy_key!r} has no registered Signal Program to shadow-evaluate"
        )
    return registration


def _reference_backtest_traces(
    strategy_key: str,
    params: StrategyParamsBase,
    bars: Sequence[TradeBar],
) -> tuple[EvaluationTrace, ...]:
    """Recompute the already-qualified trace via the sole Backtest seam."""
    registration = _registered_signal_program(strategy_key)
    strategy = registration.build(params)
    BacktestEngine(InMemoryDataReader(list(bars))).run(strategy)
    program = strategy.signal_program
    if program is None:
        raise UnsupportedShadowProgramError(
            f"{strategy_key!r} built a strategy with no SignalSession to trace"
        )
    return tuple(program.session.traces)


async def _live_adapter_traces(
    strategy_key: str,
    symbol: str,
    strategy_params: Mapping[str, Any] | None,
    bars: Sequence[TradeBar],
) -> tuple[EvaluationTrace, ...]:
    """Recompute the candidate trace via the exact seam Paper/Dry Run share.

    Calls ``strategy_evaluations`` directly -- never ``run_trade_bot`` or
    ``run_dry_run_bot`` -- so no Clerk lookup (``get_alpaca_clerk`` /
    ``get_clerk_runtime``) is ever reached from this path; each yielded
    evaluation is settled locally with ``COMMIT`` (mirroring
    ``strategy_intents``' read-only commit policy) since there is no custody
    seam here to make any other choice.
    """
    _registered_signal_program(strategy_key)
    binding = BrokerBotBinding(
        strategy_instance_id=f"shadow-trace-{strategy_key}-{symbol}",
        strategy_key=strategy_key,
        broker="alpaca",
        symbol=symbol,
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan(symbol),
        run_id=_SHADOW_RUN_ID,
        created_at_ms=bars[0].start_ms if bars else now_ms_utc(),
        strategy_params=dict(strategy_params) if strategy_params else None,
    )
    feed = _QualifiedBarFeed(symbol, bars)
    traces: list[EvaluationTrace] = []
    async for evaluation in strategy_evaluations(binding, feed):
        # Both `trace` and `settle_stage` are non-optional on the dataclass;
        # the `is not None` guards these two lines used to carry described
        # states that could not occur (issue #1736).
        traces.append(evaluation.trace)
        evaluation.settle_stage(Settlement.COMMIT)
    return tuple(traces)


def compare_canonical_traces(
    expected: Sequence[EvaluationTrace],
    observed: Sequence[EvaluationTrace],
) -> None:
    """Fail closed at the first divergent trace edge; silent on full parity.

    Compares each trace's ``semantic_payload()`` field by field, in
    declaration order, so the raised error names the exact field that
    diverged -- not merely "some trace differs". A length mismatch is a
    divergence at the first index past the shorter sequence.
    """
    length = max(len(expected), len(observed))
    for index in range(length):
        if index >= len(expected):
            raise ShadowTraceDivergenceError(
                ShadowTraceDivergence(
                    index=index,
                    evaluation_id=observed[index].evaluation_id,
                    field="trace_presence",
                    expected="<reference sequence exhausted>",
                    observed="<unexpected extra observed trace>",
                )
            )
        if index >= len(observed):
            raise ShadowTraceDivergenceError(
                ShadowTraceDivergence(
                    index=index,
                    evaluation_id=expected[index].evaluation_id,
                    field="trace_presence",
                    expected="<expected trace present>",
                    observed="<observed sequence exhausted>",
                )
            )
        expected_trace = expected[index]
        expected_payload = expected_trace.semantic_payload()
        observed_payload = observed[index].semantic_payload()
        for field_name, expected_value in expected_payload.items():
            observed_value = observed_payload.get(field_name)
            if expected_value != observed_value:
                raise ShadowTraceDivergenceError(
                    ShadowTraceDivergence(
                        index=index,
                        evaluation_id=expected_trace.evaluation_id,
                        field=field_name,
                        expected=expected_value,
                        observed=observed_value,
                    )
                )


async def run_shadow_trace_evaluation(
    strategy_key: str,
    symbol: str,
    strategy_params: Mapping[str, Any] | None,
    bars: Sequence[TradeBar],
) -> ShadowTraceEvaluation:
    """Run the shared session over qualified bars and prove trace parity.

    Raises ``UnsupportedShadowProgramError`` for a strategy with no
    registered Signal Program, or ``ShadowTraceDivergenceError`` at the first
    field where the reference and live-adapter traces disagree. Returns the
    live-adapter trace sequence (the one Paper/Dry Run would actually
    produce) only once every trace has matched the reference exactly.
    """
    registration = _registered_signal_program(strategy_key)
    params = registration.param_schema(**{**(strategy_params or {}), "symbol": symbol})
    expected = _reference_backtest_traces(strategy_key, params, bars)
    observed = await _live_adapter_traces(strategy_key, symbol, strategy_params, bars)
    compare_canonical_traces(expected, observed)
    return ShadowTraceEvaluation(
        strategy_key=strategy_key,
        symbol=symbol,
        compared_count=len(observed),
        trace_root=trace_root(list(observed)),
        traces=observed,
    )


__all__ = [
    "SHADOW_TRACE_FEED_ID",
    "ShadowTraceDivergence",
    "ShadowTraceDivergenceError",
    "ShadowTraceEvaluation",
    "UnsupportedShadowProgramError",
    "compare_canonical_traces",
    "run_shadow_trace_evaluation",
]

"""Alpaca Clerk adapter for validated strategy decisions.

Strategy mathematics remains canonical in ``app.engine.strategy.algorithms``.
This module selects an admitted strategy, feeds it the broker-neutral minute
stream, and routes only its semantic ENTER/EXIT intents to the Clerk.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPT_READ,
    DecisionOutcome,
    SqliteDecisionReceipts,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentDecision,
    DeploymentValidationDecisionKernel,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import (
    EmaCrossoverSignalProgram,
    EvaluationMode,
    EvaluationStage,
    Settlement,
)
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import FeedHealth, MarketDataBar, MarketDataFeed
from app.schemas.market_liveness import MarketLivenessFact
from app.services.bot_start_admission import market_data_capability_account_id
from app.services.broker_capability_service import extended_phase_proven_at_ms
from app.services.market_liveness import liveness_blocks_entry, market_liveness_fact
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.timestamps import now_ms_utc, ny_datetime

if TYPE_CHECKING:
    from app.services.bot_dry_run import DryRunActivityJournal
    from app.services.bot_runner import BrokerBotBinding

logger = logging.getLogger(__name__)

_EFFECT_PURPOSE_BY_INTENT = {
    SignalIntentKind.ENTER: EffectPurpose.ENTER,
    SignalIntentKind.EXIT: EffectPurpose.EXIT,
}
_INTENT_BY_DEPLOYMENT_DECISION = {
    DeploymentDecision.ENTER: SignalIntentKind.ENTER,
    DeploymentDecision.EXIT: SignalIntentKind.EXIT,
}
# Carryover is globally disabled until a future, separately reviewed slice can
# prove replay equivalence, retained open-cycle coverage, and first-future-
# decision safety for each individual program.  The empty set is deliberately
# an explicit policy boundary, not an omitted configuration default.
EXPOSURE_CARRYOVER_STRATEGY_KEYS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StrategyEvaluation:
    """One closed bar and the zero-or-one semantic intent it produced."""

    bar: MarketDataBar
    evaluation_id: str
    decision_bar_close_ms: int
    intents: tuple[SignalIntent, ...]
    # Undoes the strategy's own ENTER-time state mutation when the caller
    # blocks this evaluation's ENTER intent after signal emission (#1671
    # AC6). Both signal generators commit lifecycle state (an active
    # cycle / an open position) unconditionally at emission time, before
    # the liveness gate has a chance to veto — without this, a blocked
    # ENTER still leaves the strategy believing it holds a position, and
    # the later EXIT it emits has no real custody to close, crashing the
    # run with ``MissingEntryCustodyError``.
    rollback_blocked_entry: Callable[[], None]
    # Symmetric undo for a blocked/rejected EXIT (#1708 review finding 1).
    # Both signal generators flip their position-tracking flag to "flat" at
    # EXIT *emission*, before the caller knows whether the Clerk will admit
    # it. Without this, a rejected EXIT leaves the strategy believing it is
    # flat while the broker still holds the position, and the next eligible
    # bar can emit a fresh ENTER that overwrites bookkeeping for a position
    # that was never actually closed.
    rollback_blocked_exit: Callable[[], None]
    # A registry-backed SignalSession owns the staged decision cycle.  The
    # execution adapter supplies exactly one disposition before the next bar.
    settle_stage: Callable[[Settlement], None] | None = None
    # This mode was captured with the source bar before a consolidator could
    # turn it into a semantic decision. It must never be sampled later.
    evaluation_mode: EvaluationMode = EvaluationMode.DECIDE
    # FR-016: warmup replay recreated this staged candidate but found no
    # matching Clerk decision receipt -- the process crashed after the
    # candidate was staged and before intake captured it. The caller must
    # record `CANDIDATE_UNCAPTURED_AT_CRASH` and discard; it must never be
    # routed through the ordinary no-action/blocked/effect branches.
    crash_recovered: bool = False


class _EffectReceipt(Protocol):
    state: object


class _LiveSignalStrategy(Protocol):
    """Canonical strategy surface required by the long-lived signal adapter."""

    ctx: StrategyContext | None

    def initialize(self) -> None: ...

    def on_minute_bar(self, bar: TradeBar) -> None: ...

    def rollback_blocked_entry(self) -> None: ...

    def rollback_blocked_exit(self) -> None: ...

    def on_force_flat(self) -> None: ...


@dataclass(frozen=True)
class _LiveSignalRuntime:
    """Typed program/strategy composition for one runner-owned instance."""

    strategy: _LiveSignalStrategy
    program: EmaCrossoverSignalProgram | None

    def capture_source_bar(self, bar: TradeBar, *, mode: EvaluationMode) -> None:
        if self.program is not None:
            self.program.capture_source_bar(bar, mode=mode)

    def active_stage(self) -> EvaluationStage | None:
        return None if self.program is None else self.program.session.active_stage

    def take_completed_stage(self) -> EvaluationStage | None:
        return None if self.program is None else self.program.take_completed_stage()

    def settle(self, settlement: Settlement) -> None:
        if self.program is None:
            raise RuntimeError("This legacy strategy has no SignalSession to settle.")
        self.program.session.settle(settlement)


class _RecordingSignalIntentExecutor:
    """Satisfy the strategy boundary while the async adapter drains intents."""

    def execute(
        self,
        _context: SignalIntentExecutionContext,
        _intent: SignalIntent,
    ) -> None:
        return


class _RetainedSourceBarFeed:
    """Append exact source observations before strategy warmup or advance."""

    def __init__(self, source: MarketDataFeed, ledger: SourceBarLedger) -> None:
        self._source = source
        self._ledger = ledger
        self.feed_id = source.feed_id

    @property
    def capability_account_id(self) -> str | None:
        return getattr(self._source, "capability_account_id", None)

    @property
    def observe_only(self) -> bool:
        """Whether the enclosing run is progressing without custody effects."""
        return bool(getattr(self._source, "observe_only", False))

    def evaluation_mode_for(self, bar: MarketDataBar) -> EvaluationMode:
        """Forward the immutable mode captured by the outer runtime feed."""
        return _evaluation_mode_for(self._source, bar)

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]:
        # Capture first, then apply the sealed session policy locally. Asking
        # the provider for RTH-only data would make the authority ledger
        # depend on a lossy upstream filter and prevent a later program from
        # replaying its own session rule over the same observations.
        async for bar in self._source.stream_bars(symbol, use_rth=False):
            self._ledger.append(bar)
            if _includes_session_phase(bar, use_rth=use_rth):
                yield bar

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        retained = self._ledger.bars(provider=self.feed_id, symbol=symbol)
        if retained:
            # Recovery must rebuild the session from the precise observations
            # that drove its first run. A provider's corrected history is new
            # information, not safe warmup input for an already-running bot.
            return [
                MarketDataBar(
                    symbol=row.symbol,
                    start_ms=row.start_ms,
                    end_ms=row.end_ms,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    fetched_at_ms=row.fetched_at_ms,
                    feed_id=row.provider,
                    session_phase=row.session_phase,
                )
                for row in retained
                if _includes_session_phase(row, use_rth=use_rth)
            ]
        bars = await self._source.recent_closed_bars(
            symbol, use_rth=False, lookback_days=lookback_days
        )
        for bar in bars:
            self._ledger.append_history(bar)
        return [bar for bar in bars if _includes_session_phase(bar, use_rth=use_rth)]

    def health(self, symbol: str | None = None) -> FeedHealth:
        return self._source.health(symbol)


def _includes_session_phase(
    bar: MarketDataBar | RetainedSourceBar,
    *,
    use_rth: bool,
) -> bool:
    """Apply the sealed RTH policy after the source observation is durable."""
    return not use_rth or bar.session_phase == "RTH"


def _liveness_blocks_entry(
    binding: BrokerBotBinding,
    capability_account_id: str | None,
    liveness: MarketLivenessFact,
) -> bool:
    """Decide whether the live liveness fact should block this ENTER.

    Thin binding-aware wrapper around the shared
    ``market_liveness.liveness_blocks_entry`` predicate — also used by the
    Clerk's own submission-boundary recheck in ``runtime.py`` — so the two
    can never silently diverge (#1671).

    ``capability_account_id`` must be the market-data feed's own capability
    account (``bot_start_admission.market_data_capability_account_id``), NOT
    the Alpaca execution account: Alpaca custody identifies the execution
    account, which cannot scope an IBKR market-data entitlement. Passing the
    wrong one means the capability lookup never finds a match and every
    extended-hours entry is rejected. ``None`` (no capability account
    resolvable) fails closed — never proven.
    """
    return liveness_blocks_entry(
        liveness,
        use_rth=binding.use_rth,
        extended_phase_proven=lambda: extended_phase_proven_at_ms(
            now_ms=now_ms_utc(), symbol=binding.symbol, account_id=capability_account_id
        ),
    )


def _engine_bar(bar: MarketDataBar) -> TradeBar:
    """Translate the broker-neutral wire bar into the canonical engine bar."""
    return TradeBar(
        symbol=bar.symbol,
        start_ms=bar.start_ms,
        end_ms=bar.end_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def _evaluation_mode_for(feed: MarketDataFeed, bar: MarketDataBar) -> EvaluationMode:
    """Read a mode captured at feed yield; ordinary feeds always decide."""
    try:
        return feed.evaluation_mode_for(bar)  # type: ignore[attr-defined,no-any-return]
    except AttributeError:
        return EvaluationMode.DECIDE


async def _deployment_validation_evaluations(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    captured_decisions: Mapping[str, str] | None,
) -> AsyncIterator[StrategyEvaluation]:
    del captured_decisions  # no SignalSession here to recover a crash candidate from
    kernel = DeploymentValidationDecisionKernel()
    async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        decision = kernel.on_closed_bar(
            end_ms=bar.end_ms,
            open_price=bar.open,
            close_price=bar.close,
        )
        kind = _INTENT_BY_DEPLOYMENT_DECISION.get(decision)
        intents = (
            ()
            if kind is None
            else (
                SignalIntent(
                    kind=kind,
                    bar_close_ms=bar.end_ms,
                    intended_price=bar.close,
                ),
            )
        )
        yield StrategyEvaluation(
            bar=bar,
            evaluation_id=_generic_evaluation_id(binding, bar.end_ms),
            decision_bar_close_ms=bar.end_ms,
            intents=intents,
            rollback_blocked_entry=kernel.rollback_blocked_entry,
            rollback_blocked_exit=kernel.rollback_blocked_exit,
            evaluation_mode=_evaluation_mode_for(feed, bar),
        )


def _build_signal_strategy(
    strategy_key: str,
    symbol: str,
    strategy_params: dict[str, Any] | None,
) -> _LiveSignalRuntime:
    """Construct one registered strategy through its canonical registry `build`.

    ``strategy_params`` is the instance's immutable, already-resolved
    deploy-time parameter set (#1701) — registered defaults merged with the
    deploy request's overrides. It is ``None`` (not ``{}``) on a binding
    persisted before #1701 existed — see ``BrokerBotBinding.strategy_params``
    for why the field is optional rather than defaulted — so every registered
    default applies here exactly as it did before this feature existed. The
    deploy request's symbol is always authoritative and is injected last,
    overriding any `symbol` key that might otherwise be present. There is no
    live-adapter-private construction path — a strategy is only
    live-executable if its registry registration builds it.
    """
    registration = _STRATEGY_REGISTRY[strategy_key]
    params = registration.param_schema(**{**(strategy_params or {}), "symbol": symbol})  # type: ignore[arg-type]
    if registration.signal_program_factory is not None:
        program = registration.signal_program_factory(params)
        program.activate_for_runner()
        return _LiveSignalRuntime(strategy=program.strategy, program=program)  # type: ignore[arg-type]
    return _LiveSignalRuntime(strategy=registration.build(params), program=None)  # type: ignore[arg-type]


_WARMUP_LOOKBACK_DAYS = 5
"""Trailing calendar-day window backfilled before live decisions begin.

Generous relative to any registered signal strategy's indicator: the
largest known requirement is Strategy A's EMA(50) on 15-min bars (750
minutes, roughly two RTH sessions). #1708 review finding 3: a fresh RTH
session alone provides only ~26 fifteen-minute bars -- short of the
ADX(28)/EMA(50)-class warmup several registered strategies need -- so a
strategy deployed cold would silently withhold decisions for its first
day(s) of live trading.
"""


def _drain_bar(strategy: _LiveSignalStrategy, context: StrategyContext, bar: TradeBar) -> None:
    """Feed one closed bar through the strategy/consolidator pipeline."""
    context.current_time_ms = bar.end_ms
    strategy.on_minute_bar(bar)
    for consolidator in context.get_consolidators(bar.symbol):
        consolidator.update(bar)


_COMMIT_WORTHY_OUTCOMES: frozenset[str] = frozenset(
    {"enter_intent", "exit_intent", "entered", "exited"}
)


async def _warm_up_signal_strategy(
    runtime: _LiveSignalRuntime,
    context: StrategyContext,
    feed: MarketDataFeed,
    binding: BrokerBotBinding,
    *,
    captured_decisions: Mapping[str, str] | None,
) -> StrategyEvaluation | None:
    """Replay recent closed bars so indicators are ready before live
    decisions begin (#1708 review finding 3), reapplying each bucket's own
    known Clerk disposition so position-lifecycle state -- not just
    indicator math -- carries forward correctly (FR-016).

    ``captured_decisions`` maps this strategy instance's own recently
    durable decision identities to their outcomes (``None`` or empty when
    it has never captured one). Every bucket whose evaluation identity is
    present is reapplied exactly as it was originally disposed -- COMMIT
    for an effect-bearing outcome, DISCARD otherwise -- so a bucket later
    in the window sees the correct ``_in_position``/countdown state, not
    the permanently-flat state a blanket discard would leave. This is why
    warmup no longer captures every bar as ``OBSERVE_ONLY`` (which
    forces an unconditional auto-discard, see
    ``EmaCrossoverSignalSession.advance``): a bucket must stay inspectable
    in ``DECIDE`` mode so its settlement can be chosen per bucket.

    FR-016: a process can die after ``SignalSession.advance()`` stages a
    candidate but before Clerk intake captures it. Once a bucket's
    evaluation identity is *not* found in ``captured_decisions``, the first
    such bucket that still stages a live candidate is exactly the
    recreated, uncaptured candidate PRD section 13.3 describes -- it is
    discarded like everything else here, but also returned for the caller
    to record as ``CANDIDATE_UNCAPTURED_AT_CRASH``; it is never routed to
    custody and never given a broker contact. Any later "off-duty" bucket
    also missing from ``captured_decisions`` is caught up the ordinary
    (unflagged) way -- PRD section 13.3 step 6 names only the one recreated
    candidate, step 7 silently catches up the rest. An empty/``None``
    ``captured_decisions`` (this instance has never captured a decision at
    all) never flags anything: every bucket here would then predate this
    instance's own live-decision authority, so none of it can be a genuine
    crash artifact. Legacy strategies with no ``SignalSession``
    (``runtime.program is None``) never produce a stage and are therefore
    never flagged -- FR-016 is scoped to sealed signal programs (PRD
    Slice 5 has not yet promoted the others).

    Any position this replay reconstructs is discarded by ``on_force_flat``
    at the end regardless -- a freshly (re)deployed run always starts flat;
    carryover is a separate, still-disabled policy
    (``EXPOSURE_CARRYOVER_STRATEGY_KEYS``). Reapplying known dispositions
    only makes the *replayed math* accurate; it does not resurrect exposure.
    """
    warmup_bars = await feed.recent_closed_bars(
        binding.symbol, use_rth=binding.use_rth, lookback_days=_WARMUP_LOOKBACK_DAYS
    )
    captured = captured_decisions or {}
    uncaptured: tuple[MarketDataBar, EvaluationStage] | None = None
    for market_bar in warmup_bars:
        engine_bar = _engine_bar(market_bar)
        # DECIDE, not OBSERVE_ONLY: OBSERVE_ONLY auto-discards
        # unconditionally inside `advance()` before this loop ever gets a
        # say, which is exactly the blanket-discard behavior FR-016 needs
        # to stop applying to already-captured buckets.
        runtime.capture_source_bar(engine_bar, mode=EvaluationMode.DECIDE)
        _drain_bar(runtime.strategy, context, engine_bar)
        stage = runtime.active_stage()
        if stage is not None:
            known_outcome = captured.get(stage.trace.evaluation_id)
            if known_outcome is not None:
                runtime.settle(
                    Settlement.COMMIT
                    if known_outcome in _COMMIT_WORTHY_OUTCOMES
                    else Settlement.DISCARD
                )
            else:
                if captured and uncaptured is None and stage.decision.intent is not None:
                    uncaptured = (market_bar, stage)
                runtime.settle(Settlement.DISCARD)
        context.signal_intents.clear()
        context.consolidated_bars.clear()
    runtime.strategy.on_force_flat()
    if uncaptured is None:
        return None
    candidate_bar, candidate_stage = uncaptured
    intent = candidate_stage.decision.intent
    assert intent is not None  # narrowed by the `is not None` check above
    return StrategyEvaluation(
        bar=candidate_bar,
        evaluation_id=candidate_stage.trace.evaluation_id,
        decision_bar_close_ms=candidate_stage.trace.bar_close_ms,
        intents=(intent,),
        rollback_blocked_entry=runtime.strategy.rollback_blocked_entry,
        rollback_blocked_exit=runtime.strategy.rollback_blocked_exit,
        # Already settled DISCARD above when this stage was staged -- a
        # second `settle()` call would raise ("no staged evaluation").
        settle_stage=lambda _settlement: None,
        evaluation_mode=EvaluationMode.OBSERVE_ONLY,
        crash_recovered=True,
    )


async def _signal_strategy_evaluations(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    captured_decisions: Mapping[str, str] | None,
) -> AsyncIterator[StrategyEvaluation]:
    """Run one canonical signal-intent strategy on the production minute stream."""
    runtime = _build_signal_strategy(binding.strategy_key, binding.symbol, binding.strategy_params)
    strategy = runtime.strategy
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    context.set_signal_intent_executor(_RecordingSignalIntentExecutor())
    uncaptured_candidate = await _warm_up_signal_strategy(
        runtime,
        context,
        feed,
        binding,
        captured_decisions=captured_decisions,
    )
    if uncaptured_candidate is not None:
        yield uncaptured_candidate

    async for market_bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        mode = _evaluation_mode_for(feed, market_bar)
        engine_bar = _engine_bar(market_bar)
        runtime.capture_source_bar(engine_bar, mode=mode)
        _drain_bar(strategy, context, engine_bar)
        evaluation = _evaluation_from_active_stage(
            binding, runtime, context, market_bar, mode=mode
        )
        if evaluation.settle_stage is not None or evaluation.intents:
            yield evaluation
        # #1708 review finding 2: the consolidator only fires a working
        # bucket lazily, when a *later* bar arrives. RTH streaming stops
        # at the session close, so the final 15:45-16:00 bucket would
        # otherwise sit unflushed until the next session's bars start
        # arriving -- stranding that decision overnight. Force the flush
        # at the exact session-close boundary instead.
        if market_bar.end_ms == session_close_ms_utc(ny_datetime(market_bar.end_ms).date()):
            for consolidator in context.get_consolidators(market_bar.symbol):
                if consolidator.scan(market_bar.end_ms) is None:
                    continue
                evaluation = _evaluation_from_active_stage(
                    binding, runtime, context, market_bar, mode=mode
                )
                if evaluation.settle_stage is not None or evaluation.intents:
                    yield evaluation


def _evaluation_from_active_stage(
    binding: BrokerBotBinding,
    runtime: _LiveSignalRuntime,
    context: StrategyContext,
    market_bar: MarketDataBar,
    *,
    mode: EvaluationMode,
) -> StrategyEvaluation:
    """Drain one strategy stage and release adapter-only output buffers."""
    stage = runtime.active_stage() or runtime.take_completed_stage()
    intents = stage.intents if stage is not None else tuple(context.signal_intents)
    context.signal_intents.clear()
    # StrategyContext retains consolidated bars for finite backtest charting.
    # This adapter is long-lived and has no chart consumer, so release each
    # emitted bar after the strategy has processed it.
    context.consolidated_bars.clear()
    return StrategyEvaluation(
        bar=market_bar,
        evaluation_id=(
            stage.trace.evaluation_id
            if stage is not None
            else _generic_evaluation_id(binding, market_bar.end_ms)
        ),
        decision_bar_close_ms=(
            stage.trace.bar_close_ms if stage is not None else market_bar.end_ms
        ),
        intents=intents,
        rollback_blocked_entry=runtime.strategy.rollback_blocked_entry,
        rollback_blocked_exit=runtime.strategy.rollback_blocked_exit,
        settle_stage=(
            None
            if stage is None
            else (lambda _settlement: None)
            if stage.settlement is not None
            else lambda settlement: _settle_active_stage(runtime, context, settlement)
        ),
        evaluation_mode=(stage.trace.evaluation_mode if stage is not None else mode),
    )


def _settle_active_stage(
    runtime: _LiveSignalRuntime,
    context: StrategyContext,
    settlement: Settlement,
) -> None:
    """Settle a typed stage without leaking its committed intent into legacy buffers."""
    runtime.settle(settlement)
    context.signal_intents.clear()


def _settle_evaluation(evaluation: StrategyEvaluation, settlement: Settlement) -> None:
    """Settle a staged evaluation exactly once when the runner owns one."""
    if evaluation.settle_stage is not None:
        evaluation.settle_stage(settlement)


def _discard_evaluation(evaluation: StrategyEvaluation, intent: SignalIntent) -> None:
    """Restore local strategy state after an intent cannot reach custody."""
    if evaluation.settle_stage is not None:
        _settle_evaluation(evaluation, Settlement.DISCARD)
    elif intent.kind is SignalIntentKind.ENTER:
        evaluation.rollback_blocked_entry()
    else:
        evaluation.rollback_blocked_exit()


async def strategy_evaluations(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    *,
    captured_decisions: Mapping[str, str] | None = None,
) -> AsyncIterator[StrategyEvaluation]:
    """Stream one strategy's evaluations.

    ``captured_decisions`` maps this strategy instance's own recently
    durable decision identities to their outcomes (FR-016) -- pass it so
    warmup replay can reapply each bucket's own known disposition and
    recognize a candidate a crash left uncaptured. Omitting it (the
    default) means no such recovery is attempted, which is correct for
    :func:`strategy_intents`, a read-only stream with no custody seam to
    record or discard a recovered candidate through.
    """
    evaluation_stream = _STRATEGY_EVALUATION_STREAMS.get(binding.strategy_key)
    if evaluation_stream is None:
        raise ValueError(f"unsupported Alpaca paper strategy: {binding.strategy_key}")
    async for evaluation in evaluation_stream(binding, feed, captured_decisions):
        yield evaluation


async def strategy_intents(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[SignalIntent]:
    """Compatibility stream that commits each yielded staged evaluation.

    Custody runners use :func:`strategy_evaluations` so they can atomically
    choose a disposition.  This read-only convenience stream has no custody
    seam, therefore its only sound disposition is an immediate commit.
    """
    async for evaluation in strategy_evaluations(binding, feed):
        for intent in evaluation.intents:
            yield intent
        _settle_evaluation(evaluation, Settlement.COMMIT)


_StrategyEvaluationStream = Callable[
    ["BrokerBotBinding", MarketDataFeed, "Mapping[str, str] | None"],
    AsyncIterator[StrategyEvaluation],
]
_STRATEGY_EVALUATION_STREAMS: dict[str, _StrategyEvaluationStream] = {
    "deployment_validation": _deployment_validation_evaluations,
    "ema_crossover_signal": _signal_strategy_evaluations,
    "sma_crossover": _signal_strategy_evaluations,
    "rsi_mean_reversion": _signal_strategy_evaluations,
    "spy_strategy_a": _signal_strategy_evaluations,
    "spy_strategy_b": _signal_strategy_evaluations,
    "spy_strategy_c": _signal_strategy_evaluations,
}


def supported_alpaca_paper_strategy_keys() -> frozenset[str]:
    """Return the registry keys backed by an executable Clerk intent stream."""
    return frozenset(_STRATEGY_EVALUATION_STREAMS)


def alpaca_paper_strategy_default_symbol(strategy_key: str) -> str:
    """Return the registered parameter schema's default symbol for one strategy."""
    registration = _STRATEGY_REGISTRY[strategy_key]
    return registration.param_schema().symbol  # type: ignore[attr-defined]


async def run_trade_bot(binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
    """Execute one admitted strategy; the Clerk owns all execution truth."""
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise RuntimeError("The SQLite Alpaca Clerk is unavailable; trade-mode decisions are blocked.")
    if getattr(clerk, "authority_kind", None) != "sqlite":
        raise RuntimeError("Trade-mode decisions require the active SQLite Alpaca Clerk.")
    account_id = getattr(clerk, "account_id", None)
    if not isinstance(account_id, str) or not account_id:
        raise RuntimeError("The active SQLite Clerk has no account identity for decision receipts.")
    # Distinct from `account_id` above: this is the market-data feed's own
    # capability-scoping identity, not the Alpaca execution account — see
    # `_liveness_blocks_entry`'s docstring for why the two must never be
    # conflated.
    capability_account_id = market_data_capability_account_id(feed)
    repository = getattr(clerk, "repository", None)
    if repository is None:
        raise RuntimeError("The active SQLite Clerk has no repository for decision receipts.")
    decision_receipts = SqliteDecisionReceipts(
        repository,
        strategy_instance_id=binding.strategy_instance_id,
    )
    async for evaluation in strategy_evaluations(
        binding,
        feed,
        captured_decisions=_captured_decision_outcomes(decision_receipts),
    ):
        if len(evaluation.intents) > 1:
            raise RuntimeError("A supported trade strategy emitted multiple intents for one closed bar.")
        if evaluation.crash_recovered:
            # FR-016: DISCARD was already applied when replay staged this
            # candidate (see `_warm_up_signal_strategy`) -- record the
            # crash-window evidence and never route it to custody.
            _settle_evaluation(evaluation, Settlement.DISCARD)
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="candidate_uncaptured_at_crash",
                reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
            )
            continue
        if not evaluation.intents:
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="no_action",
                reason_code="NO_ACTION",
            )
            _settle_evaluation(evaluation, Settlement.COMMIT)
            continue
        intent = evaluation.intents[0]
        decision_id = evaluation.evaluation_id
        if evaluation.evaluation_mode is EvaluationMode.OBSERVE_ONLY:
            _discard_evaluation(evaluation, intent)
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="blocked",
                reason_code="PAUSED_OBSERVE_ONLY",
            )
            continue
        # The liveness gate applies only to ENTER — creating new exposure.
        # EXIT is deliberately exempt and always reaches the Clerk unblocked:
        # an emergency risk-reduction close must never be held hostage by
        # missing/stale liveness evidence (#1671 AC3). If a distinct
        # cancellation primitive is ever added, it must be exempted the
        # same way for the same reason.
        if intent.kind is SignalIntentKind.ENTER:
            liveness = market_liveness_fact(binding.symbol, now_ms_utc())
            if _liveness_blocks_entry(binding, capability_account_id, liveness):
                # Undo the ENTER-time state mutation the strategy already
                # committed at signal emission — otherwise it believes it
                # holds a position it was never actually granted, and its
                # later EXIT has no real custody to close (#1671 AC6).
                _discard_evaluation(evaluation, intent)
                _append_decision_receipt(
                    decision_receipts,
                    binding=binding,
                    evaluation=evaluation,
                    outcome="blocked",
                    reason_code=liveness.reason_code,
                    liveness=liveness,
                )
                logger.warning(
                    "Trade bot blocked new exposure on live market-liveness evidence",
                    extra={
                        "action": "bot_market_liveness_blocked",
                        "strategy_instance_id": binding.strategy_instance_id,
                        "strategy_key": binding.strategy_key,
                        "symbol": binding.symbol,
                        "market_liveness_state": liveness.state,
                        "reason_code": liveness.reason_code,
                    },
                )
                continue
        logger.info(
            "Trade bot decision",
            extra={
                "action": "bot_decision",
                "strategy_instance_id": binding.strategy_instance_id,
                "strategy_key": binding.strategy_key,
                "decision": intent.kind,
                "symbol": binding.symbol,
                "bar_end_ms": intent.bar_close_ms,
            },
        )
        receipt = await clerk.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            decision_id=decision_id,
            purpose=_EFFECT_PURPOSE_BY_INTENT[intent.kind],
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=binding.use_rth,
            capability_account_id=capability_account_id,
            decision_evidence=EffectDecisionEvidence(
                evaluation_id=decision_id,
                bar_ref=_decision_bar_ref(binding, evaluation),
                symbol=binding.symbol,
                outcome=(
                    "enter_intent"
                    if intent.kind is SignalIntentKind.ENTER
                    else "exit_intent"
                ),
                reason_code=f"STRATEGY_{intent.kind.value}",
                observed_at_ms=now_ms_utc(),
            ),
        )
        if _effect_state_value(receipt) == EffectOperationState.REJECTED.value:
            # The strategy already committed its ENTER/EXIT-time state
            # before this call — the outer liveness gate above only catches
            # evidence that was already stale/blocking *before* the Clerk
            # was reached. This Clerk-boundary rejection is a distinct
            # failure mode: evidence that changed *while* the intent
            # awaited the Clerk's sole-writer intake lock. A rejected ENTER
            # needs the identical rollback or a later EXIT has no real
            # custody to close (#1671 AC6); a rejected EXIT needs the
            # symmetric rollback or the strategy believes it is flat while
            # the broker still holds the position (#1708 review finding 1).
            _discard_evaluation(evaluation, intent)
        else:
            _settle_evaluation(evaluation, Settlement.COMMIT)
        logger.info(
            "Trade bot effect accepted",
            extra={
                "action": "bot_effect_accepted",
                "strategy_instance_id": binding.strategy_instance_id,
                "strategy_key": binding.strategy_key,
                "purpose": intent.kind,
                "effect_state": receipt.state.value,
                "order_refs": receipt.child_order_refs,
            },
        )


_PROTECTED_RETENTION_CLASS_BY_OUTCOME: dict[str, str] = {
    "blocked": "protected_refusal",
    # FR-016 / AC #9: crash-window evidence must survive the tail
    # compaction in `decision_receipts.py` the same way a refusal does.
    "candidate_uncaptured_at_crash": "protected_crash_evidence",
}


def _append_decision_receipt(
    receipts: SqliteDecisionReceipts,
    *,
    binding: BrokerBotBinding,
    evaluation: StrategyEvaluation,
    outcome: DecisionOutcome,
    reason_code: str,
    intent_id: str | None = None,
    order_ref: str = "",
    liveness: MarketLivenessFact | None = None,
) -> None:
    facts: dict[str, object] = {
        "bar_ref": _decision_bar_ref(binding, evaluation),
        "decision_id": evaluation.evaluation_id,
        "evaluation_id": evaluation.evaluation_id,
        "run_id": binding.run_id,
        "reason_code": reason_code,
        "retention_class": _PROTECTED_RETENTION_CLASS_BY_OUTCOME.get(outcome, "tail"),
    }
    if liveness is not None:
        facts["market_liveness"] = liveness.model_dump(mode="json")
    receipts.append(
        outcome=outcome,
        symbol=binding.symbol,
        observed_at_ms=now_ms_utc(),
        facts=facts,
        intent_id=intent_id or evaluation.evaluation_id,
        order_ref=order_ref or None,
    )


def _captured_decision_outcomes(receipts: SqliteDecisionReceipts) -> dict[str, str]:
    """Return this instance's own recently durable decisions, by identity.

    FR-016: every appended receipt's ``intent_id`` mirrors its evaluation
    identity (see the atomic-effect and refusal/no-action append sites
    above), so this is exactly the lookup warmup replay needs to reapply
    each bucket's own known disposition and recognize the one bucket that
    has none. ``MAX_DECISION_RECEIPT_READ`` bounds the read the same way
    every other decision-receipt read in this module is bounded; it
    comfortably covers ``_WARMUP_LOOKBACK_DAYS`` (5 trading days is at most
    a few hundred 15-minute buckets for a registered signal program) with
    wide margin. An empty result means this instance has never captured a
    decision -- there is no crash to recover from.
    """
    return {
        receipt.intent_id: receipt.outcome
        for receipt in receipts.tail(MAX_DECISION_RECEIPT_READ)
        if receipt.intent_id is not None
    }


def _effect_state_value(receipt: _EffectReceipt) -> str:
    state = receipt.state
    return str(getattr(state, "value", state))


def _generic_evaluation_id(binding: BrokerBotBinding, bar_close_ms: int) -> str:
    """Give compatibility evaluators the same closed identity shape as sessions."""
    payload = {
        "program_key": binding.strategy_key,
        "program_version": (
            binding.sealed_program.configured_signal.program_version
            if binding.sealed_program is not None
            else f"{binding.strategy_key}/legacy-v1"
        ),
        "settings": {"symbol": binding.symbol, **(binding.strategy_params or {})},
        "bar_close_ms": bar_close_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decision_bar_ref(binding: BrokerBotBinding, evaluation: StrategyEvaluation) -> str:
    return (
        f"decision-bar:{evaluation.bar.feed_id}:{binding.symbol}:"
        f"{evaluation.decision_bar_close_ms}"
    )


async def run_dry_run_bot(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    journal: DryRunActivityJournal,
    *,
    source_bars: SourceBarLedger | None,
) -> None:
    """Run the signal session through its isolated synthetic Clerk authority."""
    from app.services.bot_dry_run import DryRunActivity

    if source_bars is None:
        raise RuntimeError("Dry Run requires its durable source-bar ledger.")
    account_id = synthetic_account_id_for_strategy(binding.strategy_instance_id)
    runtime = get_clerk_runtime(account_id)
    clerk = None if runtime is None else runtime.clerk
    if clerk is None or getattr(clerk, "authority_kind", None) != "synthetic":
        raise RuntimeError("Dry Run requires its activated synthetic Clerk authority.")
    repository = getattr(clerk, "repository", None)
    if repository is None:
        raise RuntimeError("The synthetic Clerk has no decision-evidence repository.")
    decision_receipts = SqliteDecisionReceipts(
        repository,
        strategy_instance_id=binding.strategy_instance_id,
    )
    retained_feed = _RetainedSourceBarFeed(feed, source_bars)
    async for evaluation in strategy_evaluations(
        binding,
        retained_feed,
        captured_decisions=_captured_decision_outcomes(decision_receipts),
    ):
        if len(evaluation.intents) > 1:
            raise RuntimeError("A supported Dry Run strategy emitted multiple intents for one closed bar.")
        if evaluation.crash_recovered:
            # FR-016: DISCARD was already applied when replay staged this
            # candidate -- record the crash-window evidence and never route
            # it to the synthetic authority's custody either.
            _settle_evaluation(evaluation, Settlement.DISCARD)
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="candidate_uncaptured_at_crash",
                reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
            )
            continue
        if not evaluation.intents:
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="no_action",
                reason_code="NO_ACTION",
            )
            _settle_evaluation(evaluation, Settlement.COMMIT)
            continue
        intent = evaluation.intents[0]
        if evaluation.evaluation_mode is EvaluationMode.OBSERVE_ONLY:
            _discard_evaluation(evaluation, intent)
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="blocked",
                reason_code="PAUSED_OBSERVE_ONLY",
            )
            logger.info(
                "Dry-run candidate discarded while paused in observe-only mode",
                extra={
                    "action": "dry_run_paused_observe_only",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "intent": intent.kind.value,
                    "bar_end_ms": intent.bar_close_ms,
                },
            )
            continue
        side = "buy" if intent.kind is SignalIntentKind.ENTER else "sell"
        retained = source_bars.find_by_closed_end(
            # The ledger identity is authored from each observation's feed
            # provenance. A wrapper's stream capability name may differ
            # (for example a test or pause wrapper), so it is not evidence
            # of the decision bar's provider.
            provider=evaluation.bar.feed_id,
            symbol=binding.symbol,
            end_ms=intent.bar_close_ms,
        )
        receipt = await clerk.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            decision_id=evaluation.evaluation_id,
            purpose=_EFFECT_PURPOSE_BY_INTENT[intent.kind],
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=binding.use_rth,
            capability_account_id=market_data_capability_account_id(feed),
            retained_source_bar=retained,
            decision_evidence=EffectDecisionEvidence(
                evaluation_id=evaluation.evaluation_id,
                bar_ref=(
                    retained.bar_ref
                    if retained is not None
                    else _decision_bar_ref(binding, evaluation)
                ),
                symbol=binding.symbol,
                outcome=(
                    "enter_intent"
                    if intent.kind is SignalIntentKind.ENTER
                    else "exit_intent"
                ),
                reason_code=f"STRATEGY_{intent.kind.value}",
                observed_at_ms=now_ms_utc(),
            ),
        )
        if _effect_state_value(receipt) == EffectOperationState.REJECTED.value:
            _discard_evaluation(evaluation, intent)
            continue
        _settle_evaluation(evaluation, Settlement.COMMIT)
        if retained is None:
            raise RuntimeError(
                "A synthetic effect was accepted without its exact retained decision-bar evidence."
            )
        order_ref = receipt.child_order_refs[0] if receipt.child_order_refs else (
            f"simulated:{binding.run_id}:{evaluation.evaluation_id}"
        )
        journal.append(
            DryRunActivity(
                seq=journal.next_seq(),
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                authority_account_id=account_id,
                authority_kind="synthetic",
                recorded_at_ms=intent.bar_close_ms,
                bar_ref=retained.bar_ref,
                intent=intent.kind.value,
                order_ref=order_ref,
                symbol=binding.symbol,
                side=side,
                quantity=float(binding.quantity),
                fill_price=float(retained.close),
            )
        )
        logger.info(
            "Dry-run simulated fill",
            extra={
                "action": "dry_run_simulated_fill",
                "strategy_instance_id": binding.strategy_instance_id,
                "run_id": binding.run_id,
                "intent": intent.kind.value,
                "order_ref": order_ref,
            },
        )

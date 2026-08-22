"""FR-016 crash-recovery warmup replay for live signal strategies.

Extracted from ``bot_trade_strategy.py`` once that file's warmup /
crash-recovery slice pushed it past the thermo-nuclear 1,000-line
threshold (see ``docs/prds/sealed-signal-program-to-governed-alpaca-bot.md``
section 13.3 for the PRD this implements). This module owns exactly the
replay concern: reconstruct indicator/lifecycle state from recent closed
bars, reapply each bucket's own already-durable Clerk disposition, and
identify the one bucket a crash may have left staged but uncaptured.

Shaping a recovered candidate into ``bot_trade_strategy.StrategyEvaluation``
-- the live adapter's own public surface type -- deliberately stays in that
module, not here: this module's return type is the raw ``(bar, stage)``
pair the adapter already has to unpack. That keeps the dependency between
the two modules one-directional (``bot_trade_strategy`` imports this
module; this module never imports back), so the split needs no runtime
circular import -- only ``TYPE_CHECKING``-only references to the
runtime/binding types this module duck-types against.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPT_READ,
    SqliteDecisionReceipts,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import EvaluationMode, EvaluationStage, Settlement
from app.marketdata.feed import MarketDataBar, MarketDataFeed

if TYPE_CHECKING:
    from app.services.bot_runner import BrokerBotBinding
    from app.services.bot_trade_strategy import _LiveSignalRuntime

_WARMUP_LOOKBACK_DAYS = 5
"""Floor for the trailing calendar-day window backfilled before live
decisions begin.

This is no longer the lookback every strategy gets -- it is only the floor
:func:`_warmup_lookback_days_for` falls back to for an unregistered
``strategy_key`` or a registration with no ``signal_program_contract`` (see
that function's docstring). #1708 review finding 3: a fresh RTH session
alone provides only ~26 fifteen-minute bars -- short of the
ADX(28)/EMA(50)-class warmup several registered strategies need -- so a
strategy deployed cold would silently withhold decisions for its first
day(s) of live trading. Review finding (post-#1708): the floor alone was
not generous enough for every registered program either -- Strategy A's
sealed contract declares 9 days, not 5 -- so a fixed constant let a
program silently start deciding on incomplete history. Each sealed
program's own ``SignalProgramContract.warmup_lookback_days`` (see
``app/engine/strategy/registry.py``) is now the primary source; this
constant only floors it.
"""

_COMMIT_WORTHY_OUTCOMES: frozenset[str] = frozenset(
    {"enter_intent", "exit_intent", "entered", "exited"}
)


def _warmup_lookback_days_for(binding: BrokerBotBinding) -> int:
    """Resolve the trailing calendar-day warmup window for this binding.

    Prefers the value on the instance's own seal. A sealed program has
    already had that field proven against the live registry at Start/Resume
    (``_seal_checks``' ``clock.warmup_lookback_days`` row), so reading the
    seal is both authoritative and consistent with what admission verified —
    whereas reading the registry directly would let an edit between sealing
    and Resume silently change how far back the bot warms.

    No ``max()`` against the module floor. Flooring the sealed value would
    reintroduce exactly the defect repaired for ``decision_timeframe_ms``:
    the seal would attest one number while the bot used another. The floor
    remains only for an instance with no seal at all — a legacy or
    non-Signal-Program strategy, which has no attestation to contradict.
    """
    seal = binding.sealed_program
    if seal is not None:
        return seal.configured_signal.clock.warmup_lookback_days
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    contract = None if registration is None else registration.signal_program_contract
    if contract is None:
        return _WARMUP_LOOKBACK_DAYS
    return max(contract.warmup_lookback_days, _WARMUP_LOOKBACK_DAYS)


async def replay_warmup_bars(
    runtime: _LiveSignalRuntime,
    context: StrategyContext,
    feed: MarketDataFeed,
    binding: BrokerBotBinding,
    *,
    captured_decisions: Mapping[str, str] | None,
) -> tuple[MarketDataBar, EvaluationStage] | None:
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
    ``SignalSession.advance``): a bucket must stay inspectable
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

    Returns the ``(bar, stage)`` pair for the one bucket a crash left
    staged but uncaptured, or ``None`` when replay found no such bucket.
    The caller (``bot_trade_strategy._warm_up_signal_strategy``) shapes a
    non-``None`` result into a ``StrategyEvaluation`` with
    ``crash_recovered=True``.

    Any position this replay reconstructs is discarded by ``on_force_flat``
    at the end regardless -- a freshly (re)deployed run always starts flat;
    carryover is a separate, still-disabled policy
    (``EXPOSURE_CARRYOVER_STRATEGY_KEYS`` in ``bot_trade_strategy.py``).
    Reapplying known dispositions only makes the *replayed math* accurate;
    it does not resurrect exposure.
    """
    warmup_bars = await feed.recent_closed_bars(
        binding.symbol, use_rth=binding.use_rth, lookback_days=_warmup_lookback_days_for(binding)
    )
    captured = captured_decisions or {}
    uncaptured: tuple[MarketDataBar, EvaluationStage] | None = None
    for market_bar in warmup_bars:
        # DECIDE, not OBSERVE_ONLY: OBSERVE_ONLY auto-discards
        # unconditionally inside `advance()` before this loop ever gets a
        # say, which is exactly the blanket-discard behavior FR-016 needs
        # to stop applying to already-captured buckets.
        runtime.replay_closed_bar(context, market_bar, mode=EvaluationMode.DECIDE)
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
    return uncaptured


def captured_decision_outcomes(receipts: SqliteDecisionReceipts) -> dict[str, str]:
    """Return this instance's own recently durable decisions, by identity.

    FR-016: every appended receipt's ``intent_id`` mirrors its evaluation
    identity (see the atomic-effect and refusal/no-action append sites in
    ``bot_trade_strategy.py``), so this is exactly the lookup
    :func:`replay_warmup_bars` needs to reapply each bucket's own known
    disposition and recognize the one bucket that has none.
    ``MAX_DECISION_RECEIPT_READ`` bounds the read the same way every other
    decision-receipt read in this codebase is bounded; it comfortably
    covers even the longest lookback :func:`_warmup_lookback_days_for`
    resolves today (9 trading days is at most a few hundred 15-minute
    buckets for a registered signal program) with wide margin. An empty
    result means this instance has never captured a decision -- there is
    no crash to recover from.
    """
    return {
        receipt.intent_id: receipt.outcome
        for receipt in receipts.tail(MAX_DECISION_RECEIPT_READ)
        if receipt.intent_id is not None
    }

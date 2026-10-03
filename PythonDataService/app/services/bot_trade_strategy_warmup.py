"""FR-016 crash-recovery warmup replay for live signal strategies.

Extracted from ``bot_trade_strategy.py``; the disposition rule it applies
is ADR 0042's. This module owns exactly the
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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    SqliteDecisionReceipts,
    rotated_out_before_ms,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import EvaluationMode, EvaluationStage, Settlement
from app.marketdata.feed import MarketDataBar, MarketDataFeed
from app.schemas.decision_explanation import DecisionExplanationRecord

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
    and Resume silently change how far back the bot warms. The sealed value
    is the one the instance's own periods resolved (#2841), so a long-period
    deploy warms on more than its program's default days.

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
    captured_decisions: CapturedDecisions | None,
    record_before_start: Callable[[Sequence[DecisionExplanationRecord]], None] | None = None,
) -> tuple[MarketDataBar, EvaluationStage] | None:
    """Replay recent closed bars so indicators are ready before live
    decisions begin (#1708 review finding 3), reapplying each bucket's own
    known Clerk disposition so position-lifecycle state -- not just
    indicator math -- carries forward correctly (FR-016).

    ``captured_decisions`` is one read of this instance's retained decision
    receipts (:class:`CapturedDecisions`); its ``outcomes`` map recently
    durable decision identities to their outcomes (``None``, or empty, when
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
    recreated, uncaptured candidate -- it is discarded like everything else
    here, but also returned for the caller to record as
    ``CANDIDATE_UNCAPTURED_AT_CRASH``; it is never routed to custody and
    never given a broker contact. Any later "off-duty" bucket
    also missing from ``captured_decisions`` is caught up the ordinary
    (unflagged) way -- only the one recreated candidate is flagged, the
    rest are silently caught up. An empty/``None``
    ``captured_decisions`` (this instance has never captured a decision at
    all) never flags anything: every bucket here would then predate this
    instance's own live-decision authority, so none of it can be a genuine
    crash artifact. FR-016 is scoped to sealed signal programs, and since
    #1730 that is the only kind the live adapter builds --
    ``_build_signal_strategy`` refuses a strategy with no registered
    Signal Program rather than returning a runtime that cannot stage.

    A bucket the read no longer covers (:meth:`CapturedDecisions.covers`)
    is never flagged either (#2850). A Resume replays every retained bar
    back to the instance's warm floor (#2314), and a long-lived instance
    has decided more buckets over that span than the receipt window
    retains: such a bucket is missing from ``captured_decisions`` because
    its receipt rotated out, not because a crash left it staged. The bucket
    a crash did leave staged closed after every decision made before the
    crash, so the read always covers it.

    Returns the ``(bar, stage)`` pair for the one bucket a crash left
    staged but uncaptured, or ``None`` when replay found no such bucket.
    The caller (``bot_trade_strategy._warm_up_signal_strategy``) shapes a
    non-``None`` result into a ``StrategyEvaluation`` with
    ``crash_recovered=True``.

    Any position this replay reconstructs is discarded by ``on_force_flat``
    at the end regardless -- a freshly (re)deployed run always starts flat.
    Reapplying known dispositions only makes the *replayed math* accurate;
    it does not resurrect exposure.

    ``record_before_start`` receives every replayed bucket's explanation once
    replay ends (#2639): these are the run's before-start evaluations, shown
    behind the "bot started" line and never acted on. They are handed over
    here rather than receipted -- a receipt is a decision this run took, and
    FR-016 reads receipts as exactly that.
    """
    warmup_bars = await feed.recent_closed_bars(
        binding.symbol, use_rth=binding.use_rth, lookback_days=_warmup_lookback_days_for(binding)
    )
    captured = (
        captured_decisions
        if captured_decisions is not None
        else CapturedDecisions(outcomes={}, rotated_out_before_ms=None)
    )
    uncaptured: tuple[MarketDataBar, EvaluationStage] | None = None
    before_start: list[DecisionExplanationRecord] = []
    for market_bar in warmup_bars:
        # DECIDE, not OBSERVE_ONLY: OBSERVE_ONLY auto-discards
        # unconditionally inside `advance()` before this loop ever gets a
        # say, which is exactly the blanket-discard behavior FR-016 needs
        # to stop applying to already-captured buckets.
        runtime.replay_closed_bar(context, market_bar, mode=EvaluationMode.DECIDE)
        stage = runtime.active_stage()
        if stage is not None:
            if record_before_start is not None:
                record = DecisionExplanationRecord.from_decision(stage.bar, stage.decision)
                if record is not None:
                    before_start.append(record)
            known_outcome = captured.outcomes.get(stage.trace.evaluation_id)
            if known_outcome is not None:
                runtime.settle(
                    Settlement.COMMIT
                    if known_outcome in _COMMIT_WORTHY_OUTCOMES
                    else Settlement.DISCARD
                )
            else:
                # Only a bucket some run decided live can be a crash artifact:
                # history (a fresh deploy's warmup, or #2314's hole fill) was
                # never decided, so its candidates are ordinary catch-up. Nor
                # is a bucket whose receipt has rotated out (#2850).
                if (
                    captured.outcomes
                    and uncaptured is None
                    and stage.decision.intent is not None
                    and market_bar.provenance != "history"
                    and captured.covers(stage.trace.bar_close_ms)
                ):
                    uncaptured = (market_bar, stage)
                runtime.settle(Settlement.DISCARD)
        context.signal_intents.clear()
        context.consolidated_bars.clear()
    runtime.strategy.on_force_flat()
    if record_before_start is not None and before_start:
        record_before_start(before_start)
    return uncaptured


@dataclass(frozen=True)
class CapturedDecisions:
    """What one read of the retained decision receipts tells a Resume.

    ``outcomes`` is each retained decision's disposition, by evaluation
    identity. ``rotated_out_before_ms`` is the decision-bar close before
    which a bucket's receipt may have rotated out of that same read, or
    ``None`` while the read still holds every receipt the instance wrote
    (``decision_receipts.rotated_out_before_ms``). Neither has a default: a
    caller that names decisions also says how far back they reach, because
    replay reads the two together (#2850).
    """

    outcomes: Mapping[str, str]
    rotated_out_before_ms: int | None

    def covers(self, bar_close_ms: int) -> bool:
        """Whether this read reaches back to a bucket that closed at ``bar_close_ms``.

        Only then does a bucket missing from ``outcomes`` mean it was never
        receipted. One that closed before ``rotated_out_before_ms`` may have
        lost its receipt to rotation instead, so its absence is not evidence
        of a crash.
        """
        return self.rotated_out_before_ms is None or bar_close_ms >= self.rotated_out_before_ms


def captured_decision_outcomes(receipts: SqliteDecisionReceipts) -> CapturedDecisions:
    """Return this instance's own recently durable decisions, by identity,
    and how far back that record reaches.

    FR-016: every appended receipt's ``intent_id`` mirrors its evaluation
    identity (see the atomic-effect and refusal/no-action append sites in
    ``bot_trade_strategy.py``), so ``outcomes`` is exactly the lookup
    :func:`replay_warmup_bars` needs to reapply each bucket's own known
    disposition and recognize the one bucket that has none.
    The read is the full retention window, not the presentation cap: a
    one-minute program (``deployment_validation``) writes more receipts
    over its warmup than ``MAX_DECISION_RECEIPT_READ`` returns, and any
    bucket the read cannot see would be replayed as uncaptured (#1740).
    The window does not cover everything a Resume replays, and no window
    could: a Resume replays every retained bar back to its warm floor
    (#2314), and a long-lived instance has decided more buckets over that
    span than the window retains. ``rotated_out_before_ms`` marks where
    the window stops reaching, so replay judges absence only for the
    buckets it still covers (#2850). An empty result means this instance
    has never captured a decision -- there is no crash to recover from.
    """
    window = receipts.retained_window()
    return CapturedDecisions(
        outcomes={
            receipt.intent_id: receipt.outcome
            for receipt in window
            if receipt.intent_id is not None
        },
        rotated_out_before_ms=rotated_out_before_ms(window),
    )

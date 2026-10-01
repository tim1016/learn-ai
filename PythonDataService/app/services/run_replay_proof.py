"""Disposition-faithful run replay: classify live-vs-math divergence.

Replays a run's retained source bars through the same Signal Program the live
runner evaluated and classifies every disagreement with the run's durable
decision records as an expected live-only effect or drift. The per-run replay
receipt that used to be written from this on every Stop was cut as write-only
output (#2755); the classifier stays because it is the proof of fidelity
classification.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.runtime import STREAM_HEALTH_REASON_CODE
from app.broker.alpaca.clerk.sqlite.uncertainty import TRANSIENT_ADMISSION_REASON_CODES
from app.engine.strategy.signal_program import Settlement, trace_root
from app.lean_sidecar.closing_bar import CLOSING_BAR_REASON_CODE, is_closing_bar
from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar
from app.services.bot_trade_strategy import strategy_evaluations
from app.services.bot_trade_strategy_warmup import _COMMIT_WORTHY_OUTCOMES
from app.services.decision_session import RunDecisionSession
from app.services.feed_continuity_policy import DECISION_LATE_REASON_CODE
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.services.bot_binding_repository import BrokerBotBinding


def to_market_bar(bar: RetainedSourceBar) -> MarketDataBar:
    return bar.to_market_bar()


@dataclass(frozen=True)
class LiveDecisionRecord:
    """One durable per-bucket decision fact from the run's receipt journal."""

    seq: int
    evaluation_id: str
    outcome: str
    reason_code: str
    bar_ref: str
    # Task 5b live-time capture; empty/0 on rows recorded before it existed.
    trace_digest: str
    bar_close_ms: int


_OUTCOMES_BY_STAGED_KIND: dict[str, frozenset[str]] = {
    "ENTER": frozenset({"enter_intent", "entered"}),
    "EXIT": frozenset({"exit_intent", "exited"}),
}

EXPECTED_LIVE_GATE_REASON_CODES: frozenset[str] = frozenset(
    {
        # Terminal stop fence; retain the legacy pause reason for historical replay.
        "STOPPED_OBSERVE_ONLY",
        "PAUSED_OBSERVE_ONLY",
        # bot_trade_strategy._screen_late_decision: an ENTER decided after its
        # delivery allowance (#2303/#2345). Wall-clock lateness is live-only;
        # a replay cannot see it.
        DECISION_LATE_REASON_CODE,
        # bot_trade_strategy._refused_on_the_closing_bar: a decision on the
        # session's closing bar is never sent (#2607). Unlike the codes above,
        # whether a bucket is the closing bar is a fact of its close, so the
        # classifier re-checks it (``_blocked_divergence``) rather than
        # trusting the label.
        CLOSING_BAR_REASON_CODE,
        # app/services/market_liveness.py — every liveness fact reason that can
        # block an ENTER at the pre-Clerk gate. MARKET_TRADABLE is deliberately
        # absent: it never blocks.
        "MARKET_LIVENESS_UNAVAILABLE",
        "MARKET_CLOCK_UNAVAILABLE",
        "SYMBOL_HALTED",
        "SYMBOL_STATUS_UNKNOWN",
        "MARKET_CLOSED",
        "MARKET_CLOCK_UNKNOWN",
        "STATUS_STREAM_DISCONNECTED",
        # app/broker/alpaca/clerk/sqlite/runtime.py — every rejected() branch
        # that appends a pre-custody `blocked` receipt, plus the stream-health
        # hold whose constant we import.
        STREAM_HEALTH_REASON_CODE,
        "MARKET_LIVENESS_BLOCKED",
        "SIMULATED_SOURCE_BAR_UNPROVEN",
        "EXIT_CUSTODY_UNPROVEN",
    }
    # The SQLite facade's ENTER rejected() path calls
    # _append_pre_custody_refusal to record a protected `blocked` receipt for
    # this closed transient set. Import it rather than restate it so the
    # two sets can never drift apart.
    | TRANSIENT_ADMISSION_REASON_CODES
)
"""The CLOSED set of live-only gates (PR #1751 finding 3b).

A `blocked` receipt whose reason is outside this set is classified `drift`
(`UNRECOGNIZED_BLOCK_REASON`), never trusted. When a new live-only gate is
added to the runner or Clerk intake, its reason code must be added here in
the same PR -- the classifier failing closed on the new code is the reminder.
"""


@dataclass(frozen=True)
class RunFidelityDivergence:
    """One classified disagreement between the replayed math and the live record."""

    evaluation_id: str
    bar_close_ms: int
    classification: str  # "expected_live_effect" | "drift"
    reason_code: str
    replay_staged: str | None
    live_outcome: str | None
    detail: str


# Drift proven by a retained, aligned row (real math/decision disagreement) --
# dominant even under a truncated window. The complement (absence drift:
# MISSING_LIVE_RECORD / UNMATCHED_LIVE_RECORD) is an alignment gap a truncated
# journal cannot distinguish from real drift, so it is only a verdict on a
# complete journal.
_CONTENT_DRIFT_REASONS: frozenset[str] = frozenset(
    {"TRACE_DIGEST_MISMATCH", "DECISION_MISMATCH", "UNRECOGNIZED_BLOCK_REASON", "CLOSING_BAR_MISAPPLIED"}
)


@dataclass(frozen=True)
class RunFidelityResult:
    compared_count: int
    match_count: int
    expected_live_effect_count: int
    drift_count: int
    # Drift proven by a retained aligned row (content mismatch), separated from
    # absence drift so a known-truncated window cannot be promoted to a
    # real-drift verdict (Codex PR #1767): content drift dominates always;
    # absence drift is real only when the journal is complete.
    content_drift_count: int
    # Crash-window expected effects whose live digest was absent, so their
    # replayed content could not be verified -- forces the receipt to
    # `indeterminate`, never a clean proof verdict (Codex PR #1771).
    unverified_crash_count: int
    # Aligned buckets whose live trace_digest was present AND matched the
    # replayed trace -- the receipt's disclosure of content-level coverage
    # (digest-less legacy rows fall back to intent-kind comparison).
    digest_verified_count: int
    divergences: tuple[RunFidelityDivergence, ...]


def _blocked_divergence(*, eval_id: str, bar_close_ms: int, reason_code: str, staged: str) -> RunFidelityDivergence:
    """Classify a live ``blocked`` receipt for a bucket whose intent the replay staged too."""
    if reason_code == CLOSING_BAR_REASON_CODE and not is_closing_bar(bar_close_ms):
        classification, reason, detail = (
            "drift",
            "CLOSING_BAR_MISAPPLIED",
            "The live receipt refused this bucket as the session's closing bar, "
            "but the calendar's close for its session is a different instant.",
        )
    elif reason_code in EXPECTED_LIVE_GATE_REASON_CODES:
        classification, reason, detail = (
            "expected_live_effect",
            reason_code,
            "The shared math staged this intent; a live-only gate "
            "(liveness, pause, closing bar, or Clerk refusal) durably refused it.",
        )
    else:
        classification, reason, detail = (
            "drift",
            "UNRECOGNIZED_BLOCK_REASON",
            f"Blocked reason {reason_code!r} is not in the closed "
            "live-only-gate set; refusing to classify it as expected.",
        )
    return RunFidelityDivergence(
        evaluation_id=eval_id,
        bar_close_ms=bar_close_ms,
        classification=classification,
        reason_code=reason,
        replay_staged=staged,
        live_outcome="blocked",
        detail=detail,
    )


class _RunReplayFeed:
    """In-memory feed replaying one retained stream through the shared seam.

    ``recent_closed_bars`` returns the warmup slice regardless of
    ``lookback_days`` -- the exact behavior of ``_RetainedSourceBarFeed``'s
    retained branch, which is what the live run's own warmup consumed. Both
    streams filter through the run's ``RunDecisionSession``, resolved once by
    ``RunReplayProofService._compute``, so a replay decides on exactly the
    bars the live run did.
    Exposes no ``evaluation_mode_for``, so every bar replays in DECIDE mode
    (``bot_trade_strategy._evaluation_mode_for`` fallback); live OBSERVE_ONLY
    buckets are receipted ``blocked``/``STOPPED_OBSERVE_ONLY`` and classify as
    expected live effects. Historical ``PAUSED_OBSERVE_ONLY`` receipts keep
    the same classification without authorizing any lifecycle action.
    """

    def __init__(
        self,
        *,
        provider: str,
        symbol: str,
        warmup_bars: Sequence[MarketDataBar],
        live_bars: Sequence[MarketDataBar],
        session: RunDecisionSession,
    ) -> None:
        self.feed_id = provider
        self._symbol = symbol
        self._warmup_bars = list(warmup_bars)
        self._live_bars = list(live_bars)
        self._session = session

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]:
        del continuity, use_rth  # the run's own session filters; see __init__
        for bar in self._live_bars:
            if bar.symbol == symbol and self._session.includes(bar):
                yield bar

    async def recent_closed_bars(
        self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5
    ) -> list[MarketDataBar]:
        del lookback_days, use_rth
        return [
            bar
            for bar in self._warmup_bars
            if bar.symbol == symbol and self._session.includes(bar)
        ]

    def health(self, symbol: str | None = None) -> FeedHealth:
        del symbol
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._live_bars[-1].end_ms if self._live_bars else None,
            reason="",
            active_subscription_count=1,
            observed_at_ms=now_ms_utc(),
        )


async def run_fidelity_over_bars(
    binding: BrokerBotBinding,
    *,
    provider: str,
    warmup: Sequence[RetainedSourceBar],
    live: Sequence[RetainedSourceBar],
    records: Sequence[LiveDecisionRecord],
    captured_decisions: Mapping[str, str],
    session: RunDecisionSession,
    crash_records: Sequence[LiveDecisionRecord] = (),
) -> RunFidelityResult:
    """Replay the run's bars through the production seam, settling each stage
    with the live-recorded disposition, and classify every disagreement.

    Alignment is keyed on the deterministic ``evaluation_id``, not on receipt
    order: a single missing mid-journal receipt produces one localized
    divergence instead of cascading into a mismatched remainder (Codex PR
    #1767). Warmup buckets settle inside ``strategy_evaluations`` via
    ``captured_decisions`` (the FR-016 machinery); crash-window buckets replay
    as ``crash_recovered`` and are digest-verified against their protected
    receipts rather than trusted on presence.

    ``session`` is the run's decision session, resolved by the caller
    (``RunReplayProofService._compute``), which refuses replay outright when a
    ``use_rth=False`` binding has no declared window rather than replaying an
    empty stream (Task 4 review finding 1 -- an empty replay must never be
    mistaken for a proven one).
    """
    feed = _RunReplayFeed(
        provider=provider,
        symbol=binding.symbol,
        warmup_bars=[to_market_bar(bar) for bar in warmup],
        live_bars=[to_market_bar(bar) for bar in live],
        session=session,
    )
    records_by_eval = {record.evaluation_id: record for record in records}
    crash_by_eval = {record.evaluation_id: record for record in crash_records}
    matched: set[str] = set()
    matched_crash: set[str] = set()
    divergences: list[RunFidelityDivergence] = []
    compared = 0
    match_count = 0
    digest_verified = 0
    unverified_crash = 0

    def _digest_mismatch(record: LiveDecisionRecord, replay_digest: str, *, staged: str | None) -> None:
        divergences.append(
            RunFidelityDivergence(
                evaluation_id=record.evaluation_id,
                bar_close_ms=record.bar_close_ms,
                classification="drift",
                reason_code="TRACE_DIGEST_MISMATCH",
                replay_staged=staged,
                live_outcome=record.outcome,
                detail=(
                    "Replayed trace content differs from the live-captured digest "
                    f"(live={record.trace_digest} replay={replay_digest})."
                ),
            )
        )

    async for evaluation in strategy_evaluations(
        binding, feed, captured_decisions=dict(captured_decisions), session=session
    ):
        eval_id = evaluation.evaluation_id
        replay_digest = trace_root([evaluation.trace])
        if evaluation.crash_recovered:
            # Crash-window candidate (FR-016). It carries the same live-time
            # trace digest as an ordinary decision, so verify it -- a tampered
            # crash receipt or drift confined to the recovered candidate must
            # not launder into an expected effect (Codex PR #1767).
            evaluation.settle_stage(Settlement.DISCARD)
            crash_record = crash_by_eval.get(eval_id)
            if crash_record is None:
                divergences.append(
                    RunFidelityDivergence(
                        evaluation_id=eval_id,
                        bar_close_ms=evaluation.decision_bar_close_ms,
                        classification="drift",
                        reason_code="MISSING_LIVE_RECORD",
                        replay_staged=None,
                        live_outcome=None,
                        detail="Replay reconstructed a crash-window candidate the journal never recorded.",
                    )
                )
                continue
            matched_crash.add(eval_id)
            digest_checked = bool(crash_record.trace_digest)
            if digest_checked and crash_record.trace_digest != replay_digest:
                _digest_mismatch(crash_record, replay_digest, staged=None)
                continue
            if not digest_checked:
                # A crash-window live effect we cannot content-verify -- a legacy
                # row recorded before live-time digest capture -- must never earn
                # a clean proof verdict: its presence is legitimate FR-016
                # evidence, so classify it expected, but count it as unverified so
                # the receipt becomes `indeterminate` rather than laundering
                # unverifiable post-crash content into parity (Codex PR #1771).
                unverified_crash += 1
                divergences.append(
                    RunFidelityDivergence(
                        evaluation_id=eval_id,
                        bar_close_ms=crash_record.bar_close_ms,
                        classification="expected_live_effect",
                        reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
                        replay_staged=None,
                        live_outcome=crash_record.outcome,
                        detail=f"FR-016 crash-window evidence (bar_ref={crash_record.bar_ref!r}); no live digest to verify against.",
                    )
                )
                continue
            # Crash rows are not aligned live buckets, so a verified crash digest
            # does NOT increment digest_verified/live_compared -- the disclosed
            # coverage ratio stays scoped to the aligned decision sequence
            # (Codex PR #1771).
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=eval_id,
                    bar_close_ms=crash_record.bar_close_ms,
                    classification="expected_live_effect",
                    reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
                    replay_staged=None,
                    live_outcome=crash_record.outcome,
                    detail=f"FR-016 crash-window evidence (bar_ref={crash_record.bar_ref!r}), digest-verified.",
                )
            )
            continue
        compared += 1
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        record = records_by_eval.get(eval_id)
        if record is None:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=eval_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="MISSING_LIVE_RECORD",
                    replay_staged=staged,
                    live_outcome=None,
                    detail="The replay produced a decision bucket the live journal never recorded.",
                )
            )
            evaluation.settle_stage(Settlement.DISCARD)
            continue
        matched.add(eval_id)
        settlement = (
            Settlement.COMMIT if record.outcome in _COMMIT_WORTHY_OUTCOMES else Settlement.DISCARD
        )
        # Content-level comparison first (PR #1751 finding 3): evaluation_id
        # hashes identity, not decision content -- only the digest proves the
        # replayed trace IS the live trace. Digest-less legacy rows fall back
        # to intent-kind comparison and are excluded from digest_verified.
        # The replay side always has a digest (issue #1736), so only the
        # durable row's own nullability is still worth guarding.
        digest_checked = bool(record.trace_digest)
        if digest_checked and record.trace_digest != replay_digest:
            _digest_mismatch(record, replay_digest, staged=staged)
            evaluation.settle_stage(settlement)
            continue
        if digest_checked:
            digest_verified += 1
        staged_matches_live = staged is not None and record.outcome in _OUTCOMES_BY_STAGED_KIND.get(
            staged, frozenset()
        )
        if (staged is None and record.outcome == "no_action") or staged_matches_live:
            match_count += 1
        elif staged is not None and record.outcome == "blocked":
            # A blocked row is cross-checked, never trusted on presence: the
            # replay staged the intent (guaranteed by this branch), the digest
            # matched (checked above when present), and the reason must be a
            # known live-only gate that fits the bucket -- anything else is
            # drift, fail closed.
            divergences.append(
                _blocked_divergence(
                    eval_id=eval_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    reason_code=record.reason_code,
                    staged=staged,
                )
            )
        else:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=eval_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="DECISION_MISMATCH",
                    replay_staged=staged,
                    live_outcome=record.outcome,
                    detail="Replayed decision and live receipt disagree with no enumerating live effect.",
                )
            )
        evaluation.settle_stage(settlement)
    # Live receipts the replay never produced a bucket for: use the record's own
    # captured decision-bar close, not the Unix epoch (Codex PR #1767).
    for record in (*records, *crash_records):
        if record.evaluation_id in matched or record.evaluation_id in matched_crash:
            continue
        divergences.append(
            RunFidelityDivergence(
                evaluation_id=record.evaluation_id,
                bar_close_ms=record.bar_close_ms,
                classification="drift",
                reason_code="UNMATCHED_LIVE_RECORD",
                replay_staged=None,
                live_outcome=record.outcome,
                detail=f"Live journal receipt (bar_ref={record.bar_ref!r}) has no replayed bucket.",
            )
        )
    expected = sum(1 for d in divergences if d.classification == "expected_live_effect")
    drift = sum(1 for d in divergences if d.classification == "drift")
    content_drift = sum(
        1
        for d in divergences
        if d.classification == "drift" and d.reason_code in _CONTENT_DRIFT_REASONS
    )
    return RunFidelityResult(
        compared_count=compared,
        match_count=match_count,
        expected_live_effect_count=expected,
        drift_count=drift,
        content_drift_count=content_drift,
        unverified_crash_count=unverified_crash,
        digest_verified_count=digest_verified,
        divergences=tuple(divergences),
    )

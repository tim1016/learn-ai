"""Run-scoped replay proof: retained bars in, parity receipt out.

Direction 2 (docs/audits/strategy-execution-research-directions-2026-08-24.md):
every paper run becomes its own experiment receipt. This module owns the
pure assembly/compute pieces; the orchestration service and Stop trigger are
added by later slices of the same plan.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.qualification_shadow_trace import (
    ShadowTraceDivergence,
    ShadowTraceDivergenceError,
    UnsupportedShadowProgramError,
    run_shadow_trace_evaluation,
)
from app.broker.alpaca.clerk.sqlite.runtime import STREAM_HEALTH_REASON_CODE
from app.broker.alpaca.paths import safe_path_component
from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.signal_program import Settlement, trace_root
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.schemas.run_replay import RunReplayReceipt
from app.services.bot_trade_strategy import _includes_session_phase, strategy_evaluations
from app.services.bot_trade_strategy_warmup import _COMMIT_WORTHY_OUTCOMES
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.services.bot_binding_repository import BrokerBotBinding

logger = logging.getLogger(__name__)

RUN_REPLAY_RECEIPTS_DIRECTORY = "run_replay_receipts"
"""Per-run parity receipts, sibling of ``run_build_evidence/`` (same pattern)."""


class RunReplayUnavailableError(RuntimeError):
    """The replay proof cannot be computed for this run right now."""

    def __init__(self, message: str, *, detail: str = "", http_status: int = 409) -> None:
        self.detail = detail
        self.http_status = http_status
        super().__init__(message)


def bar_set_digest(bars: Sequence[RetainedSourceBar]) -> str:
    """Stable content digest of one retained stream, in durable order.

    Mirrors ``signal_program._semantic_hash``'s canonical-JSON discipline;
    excludes ``seq``/``fetched_at_ms``/``account_id`` so the digest names the
    market payload, not the storage row.
    """
    payload = [
        {
            "bar_identity": bar.bar_identity,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "session_phase": bar.session_phase,
        }
        for bar in bars
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def split_warmup_and_live(
    bars: Sequence[RetainedSourceBar],
    run_started_at_ms: int,
) -> tuple[list[RetainedSourceBar], list[RetainedSourceBar]]:
    """Split one retained stream at the run's durable launch instant.

    A bar that closed at or before ``started_at_ms`` was already history when
    the run launched, so the live run consumed it through warmup replay; a
    bar that closed after launch arrived through ``stream_bars``.
    """
    warmup = [bar for bar in bars if bar.end_ms <= run_started_at_ms]
    live = [bar for bar in bars if bar.end_ms > run_started_at_ms]
    return warmup, live


def to_trade_bar(bar: RetainedSourceBar) -> TradeBar:
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


def to_market_bar(bar: RetainedSourceBar) -> MarketDataBar:
    return MarketDataBar(
        symbol=bar.symbol,
        start_ms=bar.start_ms,
        end_ms=bar.end_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        fetched_at_ms=bar.fetched_at_ms,
        feed_id=bar.provider,
        session_phase=bar.session_phase,
    )


def replay_provider_for(ledger: SourceBarLedger, symbol: str) -> str:
    """Return the one provider whose retained stream is this run's evidence."""
    providers = ledger.providers_for(symbol)
    if len(providers) != 1:
        raise RunReplayUnavailableError(
            f"Retained evidence for {symbol!r} names {len(providers)} providers; replay requires exactly one.",
            detail="A replay over mixed provider streams would not reproduce any single run.",
        )
    return providers[0]


_CRASH_OUTCOME = "candidate_uncaptured_at_crash"


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


@dataclass(frozen=True)
class LiveRunDecisionEvidence:
    """Everything the fidelity replay needs from the decision-receipt journal."""

    records: tuple[LiveDecisionRecord, ...]
    crash_records: tuple[LiveDecisionRecord, ...]
    captured_decisions: dict[str, str]
    truncated: bool


def live_run_decision_evidence_from_rows(
    rows: Sequence[DecisionReceiptResource],
    run_id: str,
) -> LiveRunDecisionEvidence:
    """Shape one instance's retained receipt window into run-scoped evidence.

    ``records`` alignment excludes crash-window receipts: FR-016 records them
    during the *next* run's warmup replay, and warmup evaluations are never
    yielded by ``strategy_evaluations``, so they can never align with a
    replayed live bucket. They are reported as expected live effects instead.
    ``captured_decisions`` deliberately spans every run -- it is the same
    map ``captured_decision_outcomes`` builds for the live warmup replay.
    """
    records: list[LiveDecisionRecord] = []
    crash_records: list[LiveDecisionRecord] = []
    captured: dict[str, str] = {}
    for row in rows:  # retained_window() yields ascending seq order
        facts = json.loads(row.facts_json) if row.facts_json else {}
        evaluation_id = row.intent_id or str(facts.get("evaluation_id") or "")
        if evaluation_id:
            captured[evaluation_id] = row.outcome
        if str(facts.get("run_id") or "") != run_id:
            continue
        if not evaluation_id:
            raise RunReplayUnavailableError(
                f"Decision receipt seq {row.seq} for run {run_id!r} carries no evaluation identity.",
                detail="Replay alignment is keyed on evaluation_id; this journal cannot be aligned.",
            )
        record = LiveDecisionRecord(
            seq=row.seq,
            evaluation_id=evaluation_id,
            outcome=row.outcome,
            reason_code=str(facts.get("reason_code") or ""),
            bar_ref=str(facts.get("bar_ref") or ""),
            trace_digest=str(facts.get("trace_digest") or ""),
            bar_close_ms=int(facts.get("decision_bar_close_ms") or 0),
        )
        (crash_records if row.outcome == _CRASH_OUTCOME else records).append(record)
    return LiveRunDecisionEvidence(
        records=tuple(records),
        crash_records=tuple(crash_records),
        captured_decisions=captured,
        truncated=len(rows) >= MAX_DECISION_RECEIPTS_PER_STRATEGY,
    )


@dataclass(frozen=True)
class EngineParityResult:
    """BacktestEngine vs runner-seam trace parity over one run's exact bars."""

    trace_root: str | None
    compared_count: int
    divergence: ShadowTraceDivergence | None
    error: str | None


def engine_parity_over_bars(
    strategy_key: str,
    symbol: str,
    strategy_params: Mapping[str, Any] | None,
    bars: Sequence[TradeBar],
) -> EngineParityResult:
    """Prove (or refute) the two-seam decision-math parity for one bar set.

    Synchronous by design: the orchestrator runs it inside
    ``asyncio.to_thread``, where ``asyncio.run`` is legal because the worker
    thread has no running loop. Never call this from a coroutine.
    """
    try:
        evaluation = asyncio.run(
            run_shadow_trace_evaluation(strategy_key, symbol, strategy_params, list(bars))
        )
    except ShadowTraceDivergenceError as error:
        return EngineParityResult(
            trace_root=None,
            compared_count=error.divergence.index,
            divergence=error.divergence,
            error=None,
        )
    except UnsupportedShadowProgramError as error:
        return EngineParityResult(trace_root=None, compared_count=0, divergence=None, error=str(error))
    return EngineParityResult(
        trace_root=evaluation.trace_root,
        compared_count=evaluation.compared_count,
        divergence=None,
        error=None,
    )


_OUTCOMES_BY_STAGED_KIND: dict[str, frozenset[str]] = {
    "ENTER": frozenset({"enter_intent", "entered"}),
    "EXIT": frozenset({"exit_intent", "exited"}),
}

EXPECTED_LIVE_GATE_REASON_CODES: frozenset[str] = frozenset(
    {
        # bot_trade_strategy.py: the pause gate's blocked-receipt reason.
        "PAUSED_OBSERVE_ONLY",
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


@dataclass(frozen=True)
class RunFidelityResult:
    compared_count: int
    match_count: int
    expected_live_effect_count: int
    drift_count: int
    # Aligned buckets whose live trace_digest was present AND matched the
    # replayed trace -- the receipt's disclosure of content-level coverage
    # (digest-less legacy rows fall back to intent-kind comparison).
    digest_verified_count: int
    divergences: tuple[RunFidelityDivergence, ...]


class _RunReplayFeed:
    """In-memory feed replaying one retained stream through the shared seam.

    ``recent_closed_bars`` returns the warmup slice regardless of
    ``lookback_days`` -- the exact behavior of ``_RetainedSourceBarFeed``'s
    retained branch, which is what the live run's own warmup consumed.
    Exposes no ``evaluation_mode_for``, so every bar replays in DECIDE mode
    (``bot_trade_strategy._evaluation_mode_for`` fallback); live OBSERVE_ONLY
    buckets are receipted ``blocked``/``PAUSED_OBSERVE_ONLY`` and classify as
    expected live effects.
    """

    def __init__(
        self,
        *,
        provider: str,
        symbol: str,
        warmup_bars: Sequence[MarketDataBar],
        live_bars: Sequence[MarketDataBar],
    ) -> None:
        self.feed_id = provider
        self._symbol = symbol
        self._warmup_bars = list(warmup_bars)
        self._live_bars = list(live_bars)

    @property
    def capability_account_id(self) -> None:
        return None

    async def stream_bars(self, symbol: str, *, use_rth: bool = True) -> AsyncIterator[MarketDataBar]:
        for bar in self._live_bars:
            if bar.symbol == symbol and _includes_session_phase(bar, use_rth=use_rth):
                yield bar

    async def recent_closed_bars(
        self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5
    ) -> list[MarketDataBar]:
        del lookback_days
        return [
            bar
            for bar in self._warmup_bars
            if bar.symbol == symbol and _includes_session_phase(bar, use_rth=use_rth)
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
) -> RunFidelityResult:
    """Replay the run's bars through the production seam, settling each stage
    with the live-recorded disposition, and classify every disagreement.

    Warmup buckets settle inside ``strategy_evaluations`` via
    ``captured_decisions`` (the FR-016 machinery) and are never yielded, so
    yielded evaluations align 1:1 with the run's own receipt sequence.
    """
    feed = _RunReplayFeed(
        provider=provider,
        symbol=binding.symbol,
        warmup_bars=[to_market_bar(bar) for bar in warmup],
        live_bars=[to_market_bar(bar) for bar in live],
    )
    pending = deque(records)
    divergences: list[RunFidelityDivergence] = []
    compared = 0
    match_count = 0
    digest_verified = 0
    async for evaluation in strategy_evaluations(
        binding, feed, captured_decisions=dict(captured_decisions)
    ):
        if evaluation.crash_recovered:
            # A warmup bucket whose receipt aged out of retention replays as
            # uncaptured; it precedes this run's live window and was already
            # settled DISCARD inside the warmup machinery. Not part of the
            # alignment sequence.
            continue
        compared += 1
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        record = pending.popleft() if pending else None
        if record is None:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
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
        if record.evaluation_id != evaluation.evaluation_id:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="EVALUATION_ID_MISMATCH",
                    replay_staged=staged,
                    live_outcome=record.outcome,
                    detail=f"Live journal recorded {record.evaluation_id!r} at this position.",
                )
            )
            evaluation.settle_stage(Settlement.DISCARD)
            continue
        settlement = (
            Settlement.COMMIT if record.outcome in _COMMIT_WORTHY_OUTCOMES else Settlement.DISCARD
        )
        # Content-level comparison first (PR #1751 finding 3): evaluation_id
        # hashes identity, not decision content -- only the digest proves the
        # replayed trace IS the live trace. Digest-less legacy rows fall back
        # to intent-kind comparison and are excluded from digest_verified.
        replay_digest = None if evaluation.trace is None else trace_root([evaluation.trace])
        digest_checked = bool(record.trace_digest) and replay_digest is not None
        if digest_checked and record.trace_digest != replay_digest:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
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
            # known live-only gate -- anything else is drift, fail closed.
            if record.reason_code in EXPECTED_LIVE_GATE_REASON_CODES:
                divergences.append(
                    RunFidelityDivergence(
                        evaluation_id=evaluation.evaluation_id,
                        bar_close_ms=evaluation.decision_bar_close_ms,
                        classification="expected_live_effect",
                        reason_code=record.reason_code,
                        replay_staged=staged,
                        live_outcome=record.outcome,
                        detail=(
                            "The shared math staged this intent; a live-only gate "
                            "(liveness, pause, or Clerk refusal) durably refused it."
                        ),
                    )
                )
            else:
                divergences.append(
                    RunFidelityDivergence(
                        evaluation_id=evaluation.evaluation_id,
                        bar_close_ms=evaluation.decision_bar_close_ms,
                        classification="drift",
                        reason_code="UNRECOGNIZED_BLOCK_REASON",
                        replay_staged=staged,
                        live_outcome=record.outcome,
                        detail=(
                            f"Blocked reason {record.reason_code!r} is not in the closed "
                            "live-only-gate set; refusing to classify it as expected."
                        ),
                    )
                )
        else:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="DECISION_MISMATCH",
                    replay_staged=staged,
                    live_outcome=record.outcome,
                    detail="Replayed decision and live receipt disagree with no enumerating live effect.",
                )
            )
        evaluation.settle_stage(settlement)
    for leftover in pending:
        divergences.append(
            RunFidelityDivergence(
                evaluation_id=leftover.evaluation_id,
                bar_close_ms=0,
                classification="drift",
                reason_code="UNMATCHED_LIVE_RECORD",
                replay_staged=None,
                live_outcome=leftover.outcome,
                detail=f"Live journal receipt (bar_ref={leftover.bar_ref!r}) has no replayed bucket.",
            )
        )
    expected = sum(1 for d in divergences if d.classification == "expected_live_effect")
    drift = sum(1 for d in divergences if d.classification == "drift")
    return RunFidelityResult(
        compared_count=compared,
        match_count=match_count,
        expected_live_effect_count=expected,
        drift_count=drift,
        digest_verified_count=digest_verified,
        divergences=tuple(divergences),
    )


def _receipt_path(instance_dir: Path, run_id: str) -> Path:
    return instance_dir / RUN_REPLAY_RECEIPTS_DIRECTORY / f"{safe_path_component(run_id, 'run id')}.json"


def write_run_replay_receipt(instance_dir: Path, receipt: RunReplayReceipt) -> Path:
    """Atomically persist one run's replay receipt (replaceable: pending -> final)."""
    path = _receipt_path(instance_dir, receipt.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(receipt.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{receipt.run_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def read_run_replay_receipt(
    instance_dir: Path,
    strategy_instance_id: str,
    run_id: str,
) -> RunReplayReceipt | None:
    """Return one run's replay receipt, or an honest None when never generated."""
    path = _receipt_path(instance_dir, run_id)
    if not path.is_file():
        return None
    receipt = RunReplayReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    if receipt.strategy_instance_id != strategy_instance_id or receipt.run_id != run_id:
        raise ValueError("replay receipt belongs to another run identity")
    return receipt

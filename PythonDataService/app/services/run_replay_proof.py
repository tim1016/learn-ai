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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import (
    paper_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPTS_PER_STRATEGY,
    SqliteDecisionReceipts,
)
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
    from app.services.bot_binding_repository import (
        BotRunOutcomeRecord,
        BotRunRecord,
        BrokerBotBinding,
    )

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


_MAX_RECEIPT_DIVERGENCES = 50


def bounded_replay_bars(
    bars: Sequence[RetainedSourceBar],
    *,
    ledger_end_seq: int | None,
    terminal_recorded_at_ms: int | None,
) -> list[RetainedSourceBar]:
    """Bound one run's replay input at its durable end (PR #1751 finding 4).

    The ledger-sequence bound (snapshotted at Stop) wins; the run's terminal
    outcome instant is the wall-clock fallback for crashed/legacy runs. With
    neither, refuse: regenerating run N after run N+1 appended bars would
    otherwise change N's input, digest, and verdict.
    """
    if ledger_end_seq is not None:
        return [bar for bar in bars if bar.seq <= ledger_end_seq]
    if terminal_recorded_at_ms is not None:
        return [bar for bar in bars if bar.end_ms <= terminal_recorded_at_ms]
    raise RunReplayUnavailableError(
        "The run has no durable end boundary (no receipt snapshot and no terminal outcome).",
        detail="An unbounded replay input is not evidence; stop the run or repair its terminal record.",
    )


def refine_split_with_first_decision(
    warmup: list[RetainedSourceBar],
    live: list[RetainedSourceBar],
    *,
    first_decision_close_ms: int | None,
    decision_timeframe_ms: int | None,
) -> tuple[list[RetainedSourceBar], list[RetainedSourceBar]]:
    """Anchor the warmup/live boundary at the first recorded decision bucket.

    The wall-clock split can misclassify a bar that closed between launch and
    the warmup fetch; the run's own first decision receipt names its bucket
    exactly, so when both facts exist the boundary is that bucket's open
    (``bar_close_ms - decision_timeframe_ms``). Passthrough otherwise.
    """
    if not first_decision_close_ms or not decision_timeframe_ms:
        return warmup, live
    boundary_ms = first_decision_close_ms - decision_timeframe_ms
    merged = warmup + live
    return (
        [bar for bar in merged if bar.end_ms <= boundary_ms],
        [bar for bar in merged if bar.end_ms > boundary_ms],
    )


def _seal_decision_timeframe_ms(binding: BrokerBotBinding) -> int | None:
    """The seal-attested decision clock width, when this instance carries one.

    ``decision_timeframe_ms`` lives on the sealed program's inner
    ``configured_signal.data`` contract (``app/schemas/signal_program_seal.py``);
    fall back to ``None`` (wall-clock split) for a compatibility-mode strategy
    with no seal.
    """
    seal = binding.sealed_program
    if seal is None:
        return None
    return int(seal.configured_signal.data.decision_timeframe_ms)


def ledger_account_id_for(binding: BrokerBotBinding) -> str:
    """Return the evidence namespace whose ledger retained this binding's bars."""
    if binding.mode == "dry_run":
        return synthetic_account_id_for_strategy(binding.strategy_instance_id)
    if binding.mode == "trade":
        return paper_evidence_account_id_for_strategy(binding.strategy_instance_id)
    raise RunReplayUnavailableError(
        f"Mode {binding.mode!r} retains no source-bar evidence; nothing to replay.",
        http_status=404,
    )


@dataclass
class RunReplayProofService:
    """Compute and persist one run's replay-parity receipt."""

    artifacts_root: Path
    instance_dir_for: Callable[[str], Path]
    binding_for: Callable[[str, str], BrokerBotBinding]
    run_record_for: Callable[[str, str], BotRunRecord | None]
    is_running: Callable[[str], bool]
    run_outcome_for: Callable[[str, str], BotRunOutcomeRecord | None] | None = None
    authority_for: Callable[[BrokerBotBinding], Any] | None = None
    records_for_run: Callable[[BrokerBotBinding, str], Awaitable[LiveRunDecisionEvidence]] | None = None

    def read(self, strategy_instance_id: str, run_id: str) -> RunReplayReceipt | None:
        return read_run_replay_receipt(
            self.instance_dir_for(strategy_instance_id), strategy_instance_id, run_id
        )

    def write_pending(self, binding: BrokerBotBinding, run_id: str) -> None:
        """Durably record that a receipt is owed before background compute starts.

        Snapshots the retained stream's terminal ``seq`` as the run's end
        bound (PR #1751 finding 4) -- Stop time is the one moment "everything
        retained so far" and "everything this run observed" coincide. Bound
        resolution failures degrade to ``None`` (the terminal-outcome
        fallback still bounds generation); they must never fail Stop itself.
        """
        end_seq: int | None = None
        try:
            ledger = SourceBarLedger(
                artifacts_root=self.artifacts_root, account_id=ledger_account_id_for(binding)
            )
            try:
                provider = replay_provider_for(ledger, binding.symbol)
                stream = ledger.bars(provider=provider, symbol=binding.symbol)
                end_seq = stream[-1].seq if stream else None
            finally:
                ledger.close(checkpoint=False)
        except RunReplayUnavailableError as error:
            logger.warning(
                "Pending replay receipt written without a ledger end bound",
                extra={
                    "action": "run_replay_end_bound_unavailable",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": run_id,
                    "reason": str(error),
                },
            )
        write_run_replay_receipt(
            self.instance_dir_for(binding.strategy_instance_id),
            self._skeleton(binding, run_id, status="pending", ledger_end_seq=end_seq),
        )

    async def generate(self, broker: str, strategy_instance_id: str, run_id: str) -> RunReplayReceipt:
        binding = self.binding_for(broker, strategy_instance_id)
        if binding.run_id == run_id and self.is_running(strategy_instance_id):
            raise RunReplayUnavailableError(
                "The run is still live; stop it before generating its replay receipt.",
                detail="A live run's decision journal is still growing.",
            )
        run_record = self.run_record_for(strategy_instance_id, run_id)
        if run_record is None:
            raise RunReplayUnavailableError(
                f"Run {run_id!r} has no durable launch evidence.", http_status=404
            )
        instance_dir = self.instance_dir_for(strategy_instance_id)
        try:
            receipt = await self._compute(binding, run_record)
        except RunReplayUnavailableError:
            raise
        except Exception as error:  # compute failure becomes durable evidence, not silence
            logger.exception(
                "Run replay receipt computation failed",
                extra={
                    "action": "run_replay_receipt_failed",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": run_id,
                },
            )
            stored = self.read(strategy_instance_id, run_id)
            receipt = self._skeleton(
                binding,
                run_record.run_id,
                status="replay_failed",
                error=str(error),
                # Preserve the Stop-time end bound so a later regeneration
                # replays the same run-bounded input (PR #1751 finding 4).
                ledger_end_seq=None if stored is None else stored.ledger_end_seq,
            )
        write_run_replay_receipt(instance_dir, receipt)
        return receipt

    async def _compute(self, binding: BrokerBotBinding, run_record: BotRunRecord) -> RunReplayReceipt:
        ledger = SourceBarLedger(
            artifacts_root=self.artifacts_root, account_id=ledger_account_id_for(binding)
        )
        try:
            provider = replay_provider_for(ledger, binding.symbol)
            all_bars = ledger.bars(provider=provider, symbol=binding.symbol)
        finally:
            ledger.close(checkpoint=False)
        # Run-bounded input (PR #1751 finding 4): stored seq snapshot first,
        # terminal-outcome wall clock second, refuse when neither exists.
        stored = self.read(binding.strategy_instance_id, run_record.run_id)
        outcome = (
            None
            if self.run_outcome_for is None
            else self.run_outcome_for(binding.strategy_instance_id, run_record.run_id)
        )
        bars = bounded_replay_bars(
            all_bars,
            ledger_end_seq=None if stored is None else stored.ledger_end_seq,
            terminal_recorded_at_ms=None if outcome is None else outcome.recorded_at_ms,
        )
        if not bars:
            raise RunReplayUnavailableError(
                f"No retained source bars exist for {binding.symbol!r} within this run's bounds.",
                http_status=404,
            )
        evidence = await self._evidence(binding, run_record.run_id)
        warmup, live = split_warmup_and_live(bars, run_record.started_at_ms)
        warmup, live = refine_split_with_first_decision(
            warmup,
            live,
            first_decision_close_ms=(
                evidence.records[0].bar_close_ms if evidence.records else None
            ),
            decision_timeframe_ms=_seal_decision_timeframe_ms(binding),
        )
        decided = [bar for bar in bars if _includes_session_phase(bar, use_rth=binding.use_rth)]

        def _compute_sync() -> tuple[EngineParityResult, RunFidelityResult]:
            parity = engine_parity_over_bars(
                binding.strategy_key,
                binding.symbol,
                binding.strategy_params,
                [to_trade_bar(bar) for bar in decided],
            )
            fidelity = asyncio.run(
                run_fidelity_over_bars(
                    binding,
                    provider=provider,
                    warmup=warmup,
                    live=live,
                    records=evidence.records,
                    captured_decisions=evidence.captured_decisions,
                )
            )
            return parity, fidelity

        parity, fidelity = await asyncio.to_thread(_compute_sync)
        return self._final_receipt(binding, run_record, provider, bars, evidence, parity, fidelity)

    async def _evidence(self, binding: BrokerBotBinding, run_id: str) -> LiveRunDecisionEvidence:
        if self.records_for_run is not None:
            return await self.records_for_run(binding, run_id)
        rows = await self._receipt_rows(binding)
        return live_run_decision_evidence_from_rows(rows, run_id)

    async def _receipt_rows(self, binding: BrokerBotBinding) -> Sequence[DecisionReceiptResource]:
        if binding.mode == "dry_run":
            if self.authority_for is None:
                raise RunReplayUnavailableError(
                    "No authority selector is wired; Dry Run receipts are unreachable.",
                    http_status=503,
                )
            async with self.authority_for(binding).runtime_for_projection() as runtime:
                repository = None if runtime is None else runtime.sqlite_repository
                if repository is None:
                    raise RunReplayUnavailableError(
                        "The Dry Run synthetic authority could not be projected.",
                        http_status=503,
                    )
                return SqliteDecisionReceipts(
                    repository, strategy_instance_id=binding.strategy_instance_id
                ).retained_window()
        clerk = get_alpaca_clerk()
        repository = getattr(clerk, "repository", None)
        if repository is None:
            raise RunReplayUnavailableError(
                "The active SQLite Clerk is unavailable for replay evidence.",
                http_status=503,
            )
        return SqliteDecisionReceipts(
            repository, strategy_instance_id=binding.strategy_instance_id
        ).retained_window()

    def _skeleton(
        self,
        binding: BrokerBotBinding,
        run_id: str,
        *,
        status: str,
        error: str | None = None,
        ledger_end_seq: int | None = None,
    ) -> RunReplayReceipt:
        proof = binding.program_build
        seal = binding.sealed_program
        return RunReplayReceipt(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=run_id,
            strategy_key=binding.strategy_key,
            symbol=binding.symbol,
            provider="",
            status=status,
            bar_set_digest="",
            retained_bar_count=0,
            ledger_end_seq=ledger_end_seq,
            engine_parity_trace_root=None,
            engine_parity_compared_count=0,
            engine_parity_divergence=None,
            live_compared_count=0,
            match_count=0,
            expected_live_effect_count=0,
            drift_count=0,
            digest_verified_count=0,
            records_truncated=False,
            divergences=[],
            program_version=None if proof is None else proof.program_version,
            sealed_program_hash=None if seal is None else seal.bot_configuration_hash,
            generated_at_ms=now_ms_utc(),
            error=error,
        )

    def _final_receipt(
        self,
        binding: BrokerBotBinding,
        run_record: BotRunRecord,
        provider: str,
        bars: Sequence[RetainedSourceBar],
        evidence: LiveRunDecisionEvidence,
        parity: EngineParityResult,
        fidelity: RunFidelityResult,
    ) -> RunReplayReceipt:
        from app.schemas.run_replay import EngineParityDivergenceModel, RunReplayDivergenceModel

        crash_divergences = [
            RunFidelityDivergence(
                evaluation_id=record.evaluation_id,
                bar_close_ms=0,
                classification="expected_live_effect",
                reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
                replay_staged=None,
                live_outcome=record.outcome,
                detail=f"FR-016 crash-window evidence (bar_ref={record.bar_ref!r}).",
            )
            for record in evidence.crash_records
        ]
        all_divergences = crash_divergences + list(fidelity.divergences)
        expected = fidelity.expected_live_effect_count + len(crash_divergences)
        # Verdict ordering (PR #1751 finding 6): real drift is the loudest
        # verdict; known-incomplete evidence (truncated records) or an
        # unprovable engine leg (`parity.error`) is INDETERMINATE -- partial
        # evidence never earns a proof verdict; only complete, clean evidence
        # may claim parity.
        if parity.divergence is not None or fidelity.drift_count > 0:
            status = "drift"
        elif evidence.truncated or parity.error is not None:
            status = "indeterminate"
        elif expected > 0:
            status = "parity_with_expected_live_effects"
        else:
            status = "parity"
        skeleton = self._skeleton(binding, run_record.run_id, status=status, error=parity.error)
        return skeleton.model_copy(
            update={
                "provider": provider,
                "bar_set_digest": bar_set_digest(bars),
                "retained_bar_count": len(bars),
                # Disclose the applied end bound so every regeneration -- even
                # one that resolved the bound via the terminal outcome -- is
                # seq-pinned from here on (stable under later appends).
                "ledger_end_seq": bars[-1].seq,
                "digest_verified_count": fidelity.digest_verified_count,
                "engine_parity_trace_root": parity.trace_root,
                "engine_parity_compared_count": parity.compared_count,
                "engine_parity_divergence": (
                    None
                    if parity.divergence is None
                    else EngineParityDivergenceModel(
                        index=parity.divergence.index,
                        evaluation_id=parity.divergence.evaluation_id,
                        field=parity.divergence.field,
                        expected=repr(parity.divergence.expected),
                        observed=repr(parity.divergence.observed),
                    )
                ),
                "live_compared_count": fidelity.compared_count,
                "match_count": fidelity.match_count,
                "expected_live_effect_count": expected,
                "drift_count": fidelity.drift_count,
                "records_truncated": evidence.truncated,
                "divergences": [
                    RunReplayDivergenceModel(
                        evaluation_id=d.evaluation_id,
                        bar_close_ms=d.bar_close_ms,
                        classification=d.classification,
                        reason_code=d.reason_code,
                        replay_staged=d.replay_staged,
                        live_outcome=d.live_outcome,
                        detail=d.detail,
                    )
                    for d in all_divergences[:_MAX_RECEIPT_DIVERGENCES]
                ],
            }
        )

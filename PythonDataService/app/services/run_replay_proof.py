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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.qualification_shadow_trace import (
    ShadowTraceDivergence,
    ShadowTraceDivergenceError,
    UnsupportedShadowProgramError,
    run_shadow_trace_evaluation,
)
from app.engine.data.trade_bar import TradeBar
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

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

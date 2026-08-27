"""Disposition-faithful run replay: classification of live-vs-math divergence."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.engine.strategy.signal_program import Settlement, trace_root
from app.marketdata.feed import MarketDataBar
from app.services.bot_trade_strategy import strategy_evaluations
from app.services.run_replay_proof import (
    LiveDecisionRecord,
    run_fidelity_over_bars,
)
from app.services.source_bar_ledger import RetainedSourceBar
from tests._helpers.bot_runner.ema_parity import _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import _binding, _PhaseFeed


def _retained(bars: Sequence[MarketDataBar]) -> list[RetainedSourceBar]:
    return [
        RetainedSourceBar.from_market_bar(seq=index + 1, account_id="paper:t", bar=bar)
        for index, bar in enumerate(bars)
    ]


async def _record_live_pass(bars: Sequence[MarketDataBar], *, block_first_enter: bool) -> list[LiveDecisionRecord]:
    """Simulate exactly what run_trade_bot durably records for each bucket,
    including the Task 5b live-time trace-digest capture."""
    binding = _binding(run_id="run-1")
    records: list[LiveDecisionRecord] = []
    blocked_once = False
    async for evaluation in strategy_evaluations(binding, _PhaseFeed(live_bars=list(bars))):
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        seq = len(records) + 1
        digest = trace_root([evaluation.trace]) if evaluation.trace is not None else ""
        close_ms = evaluation.decision_bar_close_ms
        if staged is None:
            records.append(LiveDecisionRecord(seq=seq, evaluation_id=evaluation.evaluation_id,
                                              outcome="no_action", reason_code="NO_ACTION", bar_ref="",
                                              trace_digest=digest, bar_close_ms=close_ms))
            evaluation.settle_stage(Settlement.COMMIT)
        elif block_first_enter and staged == "ENTER" and not blocked_once:
            blocked_once = True
            records.append(LiveDecisionRecord(seq=seq, evaluation_id=evaluation.evaluation_id,
                                              outcome="blocked", reason_code="MARKET_CLOSED", bar_ref="",
                                              trace_digest=digest, bar_close_ms=close_ms))
            evaluation.settle_stage(Settlement.DISCARD)
        else:
            outcome = "enter_intent" if staged == "ENTER" else "exit_intent"
            records.append(LiveDecisionRecord(seq=seq, evaluation_id=evaluation.evaluation_id,
                                              outcome=outcome, reason_code="", bar_ref="",
                                              trace_digest=digest, bar_close_ms=close_ms))
            evaluation.settle_stage(Settlement.COMMIT)
    return records


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_full_parity_on_an_unblocked_run() -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    assert any(record.outcome == "enter_intent" for record in records)  # guard the guard

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.compared_count == len(records) > 0
    assert result.match_count == len(records)
    assert result.expected_live_effect_count == 0
    assert result.drift_count == 0
    assert result.digest_verified_count == len(records)  # every bucket content-verified


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_classifies_a_blocked_enter_as_expected() -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=True)

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count == 0
    assert result.expected_live_effect_count >= 1
    first = next(d for d in result.divergences if d.classification == "expected_live_effect")
    assert first.reason_code == "MARKET_CLOSED"
    assert first.replay_staged == "ENTER"
    assert first.live_outcome == "blocked"


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_classifies_a_tampered_record_as_drift() -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    victim = next(i for i, record in enumerate(records) if record.outcome == "no_action")
    records[victim] = LiveDecisionRecord(
        seq=records[victim].seq,
        evaluation_id=records[victim].evaluation_id,
        outcome="enter_intent",
        reason_code="",
        bar_ref="",
        # Only the recorded outcome is tampered; the bucket's trace is
        # unchanged, so the digest still matches -> falls to intent-kind
        # comparison, which disagrees -> DECISION_MISMATCH.
        trace_digest=records[victim].trace_digest,
        bar_close_ms=records[victim].bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "DECISION_MISMATCH"


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_flags_a_content_level_digest_mismatch_as_drift() -> None:
    """PR #1751 finding 3: same intent direction, different trace CONTENT -> drift."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    victim = next(i for i, record in enumerate(records) if record.outcome == "no_action")
    records[victim] = LiveDecisionRecord(
        seq=records[victim].seq,
        evaluation_id=records[victim].evaluation_id,
        outcome=records[victim].outcome,          # intent-level identical
        reason_code=records[victim].reason_code,
        bar_ref="",
        trace_digest="f" * 64,                    # content-level different
        bar_close_ms=records[victim].bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "TRACE_DIGEST_MISMATCH"


async def _crash_setup() -> tuple[list, dict[str, str], LiveDecisionRecord]:
    """All bars in warmup with the EXIT bucket left uncaptured, so the warmup
    machinery reconstructs it as a crash-recovered candidate."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    exit_idx = next(i for i, record in enumerate(records) if record.outcome == "exit_intent")
    exit_record = records[exit_idx]
    captured = {
        record.evaluation_id: record.outcome
        for index, record in enumerate(records)
        if index != exit_idx
    }
    return bars, captured, exit_record


@pytest.mark.asyncio
async def test_run_fidelity_digest_verifies_a_faithful_crash_window_receipt() -> None:
    """PR #1767: a crash-window receipt is accepted only after its digest is
    verified against the reconstructed candidate — never on presence alone."""
    bars, captured, exit_record = await _crash_setup()
    crash_record = LiveDecisionRecord(
        seq=exit_record.seq, evaluation_id=exit_record.evaluation_id,
        outcome="candidate_uncaptured_at_crash", reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
        bar_ref="", trace_digest=exit_record.trace_digest, bar_close_ms=exit_record.bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"), provider="fake-phase",
        warmup=_retained(bars), live=[], records=[],
        captured_decisions=captured, crash_records=[crash_record],
    )

    assert result.drift_count == 0
    assert any(
        d.classification == "expected_live_effect" and d.reason_code == "CANDIDATE_UNCAPTURED_AT_CRASH"
        for d in result.divergences
    )


@pytest.mark.asyncio
async def test_run_fidelity_treats_a_digest_less_crash_receipt_as_unverified() -> None:
    """PR #1771: a crash receipt with no live digest cannot be content-verified,
    so it is an unverified expected effect (never a clean proof verdict) — not a
    laundered `expected_live_effect` with a false 'digest-verified' claim."""
    bars, captured, exit_record = await _crash_setup()
    digestless = LiveDecisionRecord(
        seq=exit_record.seq, evaluation_id=exit_record.evaluation_id,
        outcome="candidate_uncaptured_at_crash", reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
        bar_ref="", trace_digest="", bar_close_ms=exit_record.bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"), provider="fake-phase",
        warmup=_retained(bars), live=[], records=[],
        captured_decisions=captured, crash_records=[digestless],
    )

    assert result.drift_count == 0
    assert result.unverified_crash_count >= 1
    unverified = next(
        d for d in result.divergences if d.reason_code == "CANDIDATE_UNCAPTURED_AT_CRASH"
    )
    assert unverified.classification == "expected_live_effect"
    assert "no live digest" in unverified.detail


@pytest.mark.asyncio
async def test_run_fidelity_flags_a_tampered_crash_window_receipt_as_drift() -> None:
    """A crash receipt whose captured digest no longer matches the replayed
    crash-window trace is drift, not a laundered expected effect (PR #1767)."""
    bars, captured, exit_record = await _crash_setup()
    tampered = LiveDecisionRecord(
        seq=exit_record.seq, evaluation_id=exit_record.evaluation_id,
        outcome="candidate_uncaptured_at_crash", reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
        bar_ref="", trace_digest="f" * 64, bar_close_ms=exit_record.bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"), provider="fake-phase",
        warmup=_retained(bars), live=[], records=[],
        captured_decisions=captured, crash_records=[tampered],
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "TRACE_DIGEST_MISMATCH"


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_refuses_a_blocked_row_with_an_unrecognized_reason() -> None:
    """PR #1751 finding 3b: a `blocked` row is cross-checked, never trusted on presence."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=True)
    blocked = next(i for i, record in enumerate(records) if record.outcome == "blocked")
    records[blocked] = LiveDecisionRecord(
        seq=records[blocked].seq,
        evaluation_id=records[blocked].evaluation_id,
        outcome="blocked",
        reason_code="TOTALLY_MADE_UP_GATE",       # outside the closed live-only-gate set
        bar_ref="",
        trace_digest=records[blocked].trace_digest,
        bar_close_ms=records[blocked].bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "UNRECOGNIZED_BLOCK_REASON"

"""Replay input assembly: digests, run-boundary split, conversions (Direction 2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.feed import ContinuityEventKind, MarketDataBar
from app.schemas.run_replay import RunReplayReceipt
from app.services.bot_binding_repository import BotBindingRepository, BotRunRecord
from app.services.run_replay_proof import (
    RunReplayUnavailableError,
    bar_set_digest,
    bounded_replay_bars,
    continuity_event_digest,
    replay_provider_for,
    split_warmup_and_live,
    to_market_bar,
    to_trade_bar,
)
from app.services.source_bar_ledger import (
    RetainedContinuityEvent,
    RetainedSourceBar,
    SourceBarLedger,
)

_T0 = 1_700_000_000_000


def _market_bar(index: int, *, feed_id: str = "feed-a", close: str = "400.5") -> MarketDataBar:
    start = _T0 + index * 60_000
    return MarketDataBar(
        symbol="SPY",
        start_ms=start,
        end_ms=start + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start + 60_500,
        feed_id=feed_id,
        session_phase="RTH",
    )


def _retained(
    seq: int,
    *,
    close: str = "400.5",
    evidence_seq: int | None = None,
    provenance: str = "realtime",
) -> RetainedSourceBar:
    retained = RetainedSourceBar.from_market_bar(
        seq=seq, account_id="paper:bot-a", bar=_market_bar(seq - 1, close=close)
    )
    return retained.model_copy(update={"evidence_seq": evidence_seq, "provenance": provenance})


def _event(seq: int, evidence_seq: int, kind: ContinuityEventKind) -> RetainedContinuityEvent:
    return RetainedContinuityEvent(
        seq=seq,
        run_id="run-1",
        evidence_seq=evidence_seq,
        kind=kind,
        feed_id="feed-a",
        symbol="SPY",
        observed_at_ms=_T0 + seq * 1_000,
    )


def _legacy_receipt_dict() -> dict[str, object]:
    """One receipt exactly as written before the continuity fields existed."""
    return {
        "schema_version": 1,
        "strategy_instance_id": "bot-a",
        "run_id": "run-1",
        "strategy_key": "ema_crossover_signal",
        "symbol": "SPY",
        "provider": "feed-a",
        "status": "parity",
        "bar_set_digest": "0" * 64,
        "retained_bar_count": 0,
        "ledger_end_seq": None,
        "engine_parity_trace_root": None,
        "engine_parity_compared_count": 0,
        "engine_parity_divergence": None,
        "live_compared_count": 0,
        "match_count": 0,
        "expected_live_effect_count": 0,
        "drift_count": 0,
        "digest_verified_count": 0,
        "records_truncated": False,
        "divergences": [],
        "program_version": None,
        "sealed_program_hash": None,
        "generated_at_ms": _T0,
        "error": None,
    }


def test_bar_set_digest_changes_when_a_payload_changes() -> None:
    bars = [_retained(1), _retained(2)]
    tampered = [_retained(1), _retained(2, close="401.5")]

    assert bar_set_digest(bars) == bar_set_digest([_retained(1), _retained(2)])
    assert bar_set_digest(bars) != bar_set_digest(tampered)


def test_split_warmup_and_live_uses_run_start_boundary() -> None:
    bars = [_retained(1), _retained(2), _retained(3)]
    run_started_at_ms = bars[1].end_ms  # bar 1 closed exactly at start -> warmup

    warmup, live = split_warmup_and_live(bars, run_started_at_ms)

    assert [bar.seq for bar in warmup] == [1, 2]
    assert [bar.seq for bar in live] == [3]


def test_to_trade_bar_and_to_market_bar_round_trip_the_payload() -> None:
    retained = _retained(1)

    trade_bar = to_trade_bar(retained)
    market_bar = to_market_bar(retained)

    assert (trade_bar.symbol, trade_bar.start_ms, trade_bar.end_ms) == ("SPY", retained.start_ms, retained.end_ms)
    assert trade_bar.close == retained.close
    assert market_bar.feed_id == retained.provider
    assert market_bar.session_phase == retained.session_phase
    assert market_bar.fetched_at_ms == retained.fetched_at_ms


def test_replay_provider_for_requires_exactly_one_provider(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:bot-a")
    try:
        with pytest.raises(RunReplayUnavailableError):
            replay_provider_for(ledger, "SPY")  # zero providers

        ledger.append(_market_bar(0, feed_id="feed-a"))
        assert replay_provider_for(ledger, "SPY") == "feed-a"

        ledger.append(_market_bar(5, feed_id="feed-b"))
        with pytest.raises(RunReplayUnavailableError):
            replay_provider_for(ledger, "SPY")  # ambiguous evidence
    finally:
        ledger.close(checkpoint=False)


def test_read_run_serves_the_replay_run_boundary_reader(tmp_path: Path) -> None:
    # Replay reuses the canonical BotBindingRepository.read_run reader for the
    # run's durable launch instant (started_at_ms) rather than a bespoke
    # duplicate; it validates run identity and returns None for an unknown run.
    repository = BotBindingRepository(tmp_path, instance_dir_for=lambda sid: tmp_path / "live_state" / sid)
    record = BotRunRecord(
        run_id="run-1",
        strategy_instance_id="bot-a",
        configuration_hash="0" * 64,
        launch_reason="deploy",
        started_at_ms=_T0,
    )
    runs_dir = tmp_path / "live_state" / "bot-a" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run-1.json").write_text(record.model_dump_json(), encoding="utf-8")

    loaded = repository.read_run("bot-a", "run-1")

    assert loaded is not None
    assert loaded.started_at_ms == _T0
    assert repository.read_run("bot-a", "run-2") is None


def test_bar_set_digest_is_unchanged_for_realtime_streams_and_changes_with_a_substitute() -> None:
    bars = [_retained(seq=1), _retained(seq=2)]
    before = bar_set_digest(bars)
    assert bar_set_digest([b.model_copy(update={"provenance": "realtime"}) for b in bars]) == before
    with_substitute = bar_set_digest([bars[0], bars[1].model_copy(update={"provenance": "historical_substitute"})])
    assert with_substitute != before


def test_continuity_event_digest_is_stable_and_order_sensitive() -> None:
    events = [_event(seq=1, evidence_seq=3, kind="interruption"), _event(seq=2, evidence_seq=5, kind="recovered")]
    assert continuity_event_digest(events) == continuity_event_digest(list(events))
    assert continuity_event_digest(events) != continuity_event_digest(events[:1])
    assert continuity_event_digest(events) != continuity_event_digest(list(reversed(events)))


def test_bounded_replay_bars_prefers_the_evidence_bound() -> None:
    bars = [_retained(seq=1, evidence_seq=1), _retained(seq=2, evidence_seq=4), _retained(seq=3, evidence_seq=6)]
    kept = bounded_replay_bars(bars, ledger_end_seq=3, terminal_recorded_at_ms=None, evidence_end_seq=4)
    assert [b.seq for b in kept] == [1, 2]


def test_receipt_without_new_fields_still_parses() -> None:
    receipt = RunReplayReceipt.model_validate(_legacy_receipt_dict())
    assert receipt.continuity_event_digest is None and receipt.evidence_end_seq is None

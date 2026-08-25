"""Replay input assembly: digests, run-boundary split, conversions (Direction 2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BotBindingRepository, BotRunRecord
from app.services.run_replay_proof import (
    RunReplayUnavailableError,
    bar_set_digest,
    replay_provider_for,
    split_warmup_and_live,
    to_market_bar,
    to_trade_bar,
)
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

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


def _retained(index: int, *, close: str = "400.5") -> RetainedSourceBar:
    return RetainedSourceBar.from_market_bar(
        seq=index + 1, account_id="paper:bot-a", bar=_market_bar(index, close=close)
    )


def test_bar_set_digest_changes_when_a_payload_changes() -> None:
    bars = [_retained(0), _retained(1)]
    tampered = [_retained(0), _retained(1, close="401.5")]

    assert bar_set_digest(bars) == bar_set_digest([_retained(0), _retained(1)])
    assert bar_set_digest(bars) != bar_set_digest(tampered)


def test_split_warmup_and_live_uses_run_start_boundary() -> None:
    bars = [_retained(0), _retained(1), _retained(2)]
    run_started_at_ms = bars[1].end_ms  # bar 1 closed exactly at start -> warmup

    warmup, live = split_warmup_and_live(bars, run_started_at_ms)

    assert [bar.seq for bar in warmup] == [1, 2]
    assert [bar.seq for bar in live] == [3]


def test_to_trade_bar_and_to_market_bar_round_trip_the_payload() -> None:
    retained = _retained(0)

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


def test_read_run_record_returns_durable_launch_evidence(tmp_path: Path) -> None:
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

    loaded = repository.read_run_record("bot-a", "run-1")

    assert loaded is not None
    assert loaded.started_at_ms == _T0
    assert repository.read_run_record("bot-a", "run-2") is None

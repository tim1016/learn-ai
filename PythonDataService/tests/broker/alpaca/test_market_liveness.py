"""Tests for the owned Alpaca real-time market-liveness source (#1671)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.market_liveness import AlpacaMarketLivenessConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerClockEvidence
from app.schemas.market_liveness import SymbolTradingStatusEvidence
from app.services.market_liveness import MarketLivenessStore

_NOW = 1_700_000_000_000


class _Read:
    async def get_clock_evidence(self) -> BrokerClockEvidence:
        return BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )


def _consumer(tmp_path: Path, store: MarketLivenessStore) -> AlpacaMarketLivenessConsumer:
    async def _frames():
        if False:  # pragma: no cover - establishes an injected empty iterator.
            yield ""

    return AlpacaMarketLivenessConsumer(
        read=_Read(),  # type: ignore[arg-type]
        frame_source=_frames,
        store=store,
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _NOW),
        clock=lambda: _NOW,
        max_reconnects=0,
    )


@pytest.mark.asyncio
async def test_clock_poll_and_status_transition_compose_the_shared_fact(tmp_path: Path) -> None:
    store = MarketLivenessStore()
    consumer = _consumer(tmp_path, store)

    await consumer.refresh_clock()
    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "H", "t": "2023-11-14T22:13:20Z"}]))

    halted = store.fact("SPY", now_ms=_NOW)

    assert halted.state == "HALTED"
    assert halted.reason_code == "SYMBOL_HALTED"
    assert halted.symbol_status is not None
    assert halted.symbol_status.reason_code == "ALPACA_STATUS_HALT"

    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": "2023-11-14T22:13:21Z"}]))

    resumed = store.fact("SPY", now_ms=_NOW)

    assert resumed.state == "TRADABLE"
    assert resumed.reason_code == "MARKET_TRADABLE"
    assert resumed.symbol_status is not None
    assert resumed.symbol_status.reason_code == "ALPACA_STATUS_RESUME"


def test_bad_status_frame_clears_prior_symbol_evidence(tmp_path: Path) -> None:
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    store.observe_symbol_status(
        SymbolTradingStatusEvidence(
            symbol="SPY",
            state="TRADABLE",
            source="test.status",
            observed_at_ms=_NOW,
            source_timestamp_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame("not-json")

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "UNKNOWN"
    assert fact.reason_code == "SYMBOL_STATUS_UNAVAILABLE"

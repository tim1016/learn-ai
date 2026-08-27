"""Paper trade runs retain their source bars (Direction 2, deliverable 1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.marketdata.feed import MarketDataBar
from app.services.bot_runtime import PauseAwareFeed, execute_bot_run
from app.services.bot_trade_strategy import _RetainedSourceBarFeed, run_trade_bot
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.bot_runner.doubles import _FakeClerk
from tests._helpers.bot_runner.ema_parity import _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import (  # noqa: F401 -- autouse fixture
    _binding,
    _fresh_live_market_liveness,
    _PhaseFeed,
)

_T0 = 1_700_000_000_000


class _MixedPhaseFeed:
    """Yields bars across session phases so the RTH filter has something to drop."""

    feed_id = "mixed-phase"

    def __init__(self, bars: list[MarketDataBar]) -> None:
        self._bars = bars

    async def stream_bars(self, symbol: str, *, use_rth: bool = True) -> AsyncIterator[MarketDataBar]:
        del symbol, use_rth
        for bar in self._bars:
            yield bar


def _phase_bar(index: int, phase: str) -> MarketDataBar:
    start = _T0 + index * 60_000
    return MarketDataBar(
        symbol="SPY",
        start_ms=start,
        end_ms=start + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal("400.5"),
        volume=100,
        fetched_at_ms=start + 60_500,
        feed_id="mixed-phase",
        session_phase=phase,
    )


@pytest.mark.asyncio
async def test_retained_feed_consumes_modes_of_filtered_bars(tmp_path: Path) -> None:
    """Filtered (non-RTH) bars must not leak captured evaluation modes.

    The session pops a captured mode only for bars it actually evaluates, and
    it never sees a bar the retained feed filters out. Without the retained
    feed consuming those modes, PauseAwareFeed's map would grow unbounded over
    a long-running paper session (Codex PR #1764 P2).
    """
    gate = asyncio.Event()
    gate.set()  # DECIDE mode
    bars = [_phase_bar(0, "PRE"), _phase_bar(1, "RTH"), _phase_bar(2, "POST")]
    pause_feed = PauseAwareFeed(_MixedPhaseFeed(bars), gate)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:leak")
    try:
        retained = _RetainedSourceBarFeed(pause_feed, ledger)

        yielded = [bar async for bar in retained.stream_bars("SPY", use_rth=True)]

        assert [bar.session_phase for bar in yielded] == ["RTH"]
        # Only the yielded RTH bar's mode remains (the real session pops it as
        # it evaluates); the two filtered bars leaked nothing.
        assert set(pause_feed._captured_modes) == {
            (bar.symbol, bar.start_ms, bar.end_ms) for bar in yielded
        }
        # And all three bars were still durably retained (capture-first).
        assert len(ledger.bars(provider="mixed-phase", symbol="SPY")) == 3
    finally:
        ledger.close(checkpoint=False)


@pytest.mark.asyncio
async def test_run_trade_bot_retains_every_live_source_bar(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        bars = _ema_parity_bars_through_first_exit()

        await run_trade_bot(_binding(run_id="run-1"), _PhaseFeed(live_bars=bars), source_bars=ledger)

        # Retention keys each row by ``bar.feed_id`` (the canonical stream
        # identity), which for this golden fixture is "lean-golden" — in
        # production the feed's ``feed_id`` and its bars' ``feed_id`` coincide.
        provider = bars[0].feed_id
        retained = ledger.bars(provider=provider, symbol="SPY")
        assert [row.end_ms for row in retained] == [bar.end_ms for bar in bars]
        assert [str(row.close) for row in retained] == [str(bar.close) for bar in bars]
    finally:
        ledger.close()
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_execute_bot_run_trade_mode_requires_source_bar_ledger(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="source-bar ledger"):
        await execute_bot_run(
            _binding(run_id="run-1"),
            _PhaseFeed(),
            run_gate=None,
            instance_dir=tmp_path,
            source_bars=None,
        )

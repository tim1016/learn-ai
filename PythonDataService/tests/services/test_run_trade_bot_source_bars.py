"""Paper trade runs retain their source bars (Direction 2, deliverable 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services.bot_runtime import execute_bot_run
from app.services.bot_trade_strategy import run_trade_bot
from app.services.source_bar_ledger import SourceBarLedger
from tests.services.test_bot_runner import (
    _SID,
    _ema_parity_bars_through_first_exit,
    _FakeClerk,
)
from tests.services.test_candidate_uncaptured_at_crash import (  # noqa: F401 -- autouse fixture
    _binding,
    _fresh_live_market_liveness,
    _PhaseFeed,
)


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

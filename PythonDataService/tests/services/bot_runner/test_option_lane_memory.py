"""Opt-in, networkless container memory qualification for option bots (#3467).

The application, runners, SQLite account, quote supervisor and spawned search
workers are real. Market data and brokerage transports are doubles. Run only
in a dedicated cgroup, never in a serving Clerk.
"""

from __future__ import annotations

import asyncio
import gc
import multiprocessing
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from app.broker.ibkr.config import reset_settings_for_testing
from app.config import settings
from app.marketdata.feed import MarketDataBar
from app.options.named_structures import NamedStructure
from app.options.structure_catalogue import starting_plan
from app.services import bot_trade_strategy
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.option_search import OptionSearches
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.doubles import _FakeFeed

from ._option_floor import DELIVERY_MS, FIRST_GREEN_MS, MINUTE, red
from ._paper_option_floor import PAPER_ACCOUNT, PaperFloor
from .test_option_bot_entry_outcome import on_paper as on_paper

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("OPTION_LANE_MEMORY_CHECK") != "1", reason="requires a dedicated memory-limited container"
    ),
]


def _memory() -> int:
    return int(Path("/sys/fs/cgroup/memory.current").read_text())


class _Feed(_FakeFeed):
    """Keep each runner alive between deliveries without retaining every tick."""

    def __init__(self) -> None:
        super().__init__([])
        self.deliveries: asyncio.Queue[MarketDataBar] = asyncio.Queue(maxsize=1)

    async def stream_bars(self, symbol: str, **_: Any) -> AsyncIterator[MarketDataBar]:
        while True:
            yield await self.deliveries.get()


async def _round(floor: PaperFloor, *, bots: int, restart: int, worker_pids: set[int]) -> tuple[list[int], int, int]:
    feeds: list[tuple[str, _Feed]] = []
    tasks: list[asyncio.Task[None]] = []
    ledgers: list[SourceBarLedger] = []
    bindings: list[BrokerBotBinding] = []
    samples: list[int] = []
    finished_before = floor._evaluations_finished
    try:
        for index, symbol in enumerate(("SPY", "QQQ", "IWM", "DIA")[:bots]):
            binding = floor.binding.model_copy(
                update={
                    "strategy_instance_id": f"memory-bot-{restart}-{index}",
                    "run_id": f"memory-run-{restart}-{index}",
                    "symbol": symbol,
                    "action_plan": starting_plan(NamedStructure.IRON_BUTTERFLY, symbol),
                }
            )
            await floor.clerk.register_strategy_run(binding)
            bindings.append(binding)
            feed = _Feed()
            feeds.append((symbol, feed))
            ledger = SourceBarLedger(
                artifacts_root=floor.tmp_path / binding.strategy_instance_id, account_id=PAPER_ACCOUNT
            )
            ledgers.append(ledger)
            tasks.append(
                asyncio.create_task(
                    bot_trade_strategy.run_trade_bot(
                        binding,
                        feed,
                        source_bars=ledger,
                        pricing_feed=feed,
                        option_market=floor.market(),
                    )
                )
            )
        for minute in range(30):
            at = FIRST_GREEN_MS + (restart * 30 + minute) * MINUTE
            await floor.bar_arrives(at + DELIVERY_MS)
            for symbol, feed in feeds:
                await feed.deliveries.put(red(at, symbol=symbol))
            async with asyncio.timeout(5):
                while floor._evaluations_finished < finished_before + (minute + 1) * bots:
                    for task in tasks:
                        assert not task.done(), task.exception() if not task.cancelled() else "runner was cancelled"
                    await asyncio.sleep(0.002)
            samples.append(_memory())
            assert worker_pids <= {process.pid for process in multiprocessing.active_children()}
            # Call recording belongs to the transport double, not to a serving lane.
            floor.ibkr.client.reset_mock()
        assert len(floor.watch.requests()) == bots
        lines = len(floor.ibkr.open_option_lines())
        assert lines >= 84 * bots
        assert floor.venue.option_submit_calls == [], "the flat red-bar workload must send no order"
    finally:
        try:
            for binding in bindings:
                await floor.clerk.stop_strategy_run(
                    strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id
                )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ledger in ledgers:
                ledger.close()
    await floor.supervisor_passes()
    gc.collect()
    assert floor.watch.requests() == ()
    assert floor.ibkr.open_option_lines() == set()
    assert max(samples[-10:]) - min(samples[-10:]) < 16 * 1024**2, "memory kept growing after warmup"
    return samples, _memory(), lines


async def test_option_bots_fit_the_lane_with_headroom_across_stop_and_start(
    on_paper: Callable[..., PaperFloor],
    record_property: Callable[[str, Any], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A resolver-only process understates the serving lane's baseline.
    import app.main  # noqa: F401

    limit = int(Path("/sys/fs/cgroup/memory.max").read_text())
    assert 0 < limit <= 2 * 1024**3, "use a dedicated cgroup with a realistic lane limit"
    bots = int(os.environ.get("OPTION_LANE_BOTS", "4"))
    assert 1 <= bots <= 4
    reset_settings_for_testing()
    monkeypatch.setattr(settings, "ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED", True)
    floor = await on_paper(line_budget=400, reach=20).open()

    def quote(name: str) -> None:
        floor.ibkr.now = floor.clock.value
        floor.ibkr.tick(name, bid=4.08, ask=4.12)

    def underlyings() -> None:
        floor.ibkr.now = floor.clock.value
        for name, ticker in tuple(floor.ibkr.lines.items()):
            if ticker.contract.secType == "STK":
                floor.ibkr.tick(name, bid=585.10, ask=585.12)

    monkeypatch.setattr(floor, "_quote_line", quote)
    monkeypatch.setattr(floor, "spy", underlyings)
    floor.option_lines_tick = True
    searches = OptionSearches()
    floor.lane = searches.entry
    stopped: list[int] = []
    worker_pids: set[int] = set()
    try:
        # As in production, stopping a bot does not close the process's shared workers.
        for restart in range(3):
            searches.entry.warm()
            await searches.entry._worker()
            searches.preview.warm()
            await searches.preview.lane._worker()
            entry_pid, preview_pid = searches.entry.worker_pid, searches.preview.lane.worker_pid
            assert entry_pid is not None and preview_pid is not None
            pids = {entry_pid, preview_pid}
            assert len(pids) == 2
            if worker_pids:
                assert pids == worker_pids, "a bot stop must not replace the shared workers"
            worker_pids = pids
            samples, after_stop, lines = await _round(floor, bots=bots, restart=restart, worker_pids=pids)
            stopped.append(after_stop)
            record_property(f"round_{restart}_samples_bytes", samples)
            record_property(f"round_{restart}_stopped_bytes", after_stop)
            record_property(f"round_{restart}_option_lines", lines)
    finally:
        searches.close()
    assert worker_pids.isdisjoint(process.pid for process in multiprocessing.active_children())
    peak = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    record_property("bots", bots)
    record_property("peak_bytes", peak)
    record_property("workers_closed_bytes", _memory())
    # Reserve space for health checks, request bursts and retained account evidence.
    assert peak <= limit * 4 // 5, f"{bots} bots peaked at {peak} bytes against {limit}; less than 20% headroom"
    assert stopped[-1] - stopped[-2] < 16 * 1024**2, "successive bot runs retained more memory"

"""In-process bot task state, pause gating, and mode execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from app.marketdata.feed import FeedHealth, MarketDataBar, MarketDataFeed
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_dry_run import DryRunActivityJournal
from app.services.bot_runner_errors import RunAdmissionRefusedError, UnknownBotError
from app.services.bot_trade_strategy import run_dry_run_bot, run_trade_bot
from app.services.source_bar_ledger import SourceBarLedger

logger = logging.getLogger(__name__)


def _open_run_gate() -> asyncio.Event:
    """Create the gate for a newly evaluating run."""
    gate = asyncio.Event()
    gate.set()
    return gate


@dataclass
class ManagedBot:
    """One supervised bot task and its terminal bookkeeping."""

    binding: BrokerBotBinding
    task: asyncio.Task[None] = field(repr=False)
    stop_reason_code: str | None = None
    finalized: bool = False
    # A new managed run evaluates immediately; Pause is the only transition
    # that closes this gate.
    run_gate: asyncio.Event = field(default_factory=_open_run_gate, repr=False)


class PauseAwareFeed:
    """Progress the feed while a paused run is restricted to observation.

    A pause is deliberately not a stream backpressure mechanism.  Holding or
    discarding source bars would make the next strategy decision depend on an
    operator's timing and leave retained-bar replay unable to reproduce the
    session.  The runner reads ``observe_only`` per closed bar and settles any
    candidate as discarded instead of sending it to custody.
    """

    def __init__(
        self,
        source: MarketDataFeed,
        gate: asyncio.Event,
    ) -> None:
        self._source = source
        self._gate = gate
        self.feed_id = source.feed_id

    @property
    def observe_only(self) -> bool:
        """True only while the same live run remains paused."""
        return not self._gate.is_set()

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]:
        async for bar in self._source.stream_bars(symbol, use_rth=use_rth):
            yield bar

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        return await self._source.recent_closed_bars(symbol, use_rth=use_rth, lookback_days=lookback_days)

    def health(self, symbol: str | None = None) -> FeedHealth:
        return self._source.health(symbol)


def require_live_managed_bot(
    managed_bots: dict[str, ManagedBot],
    broker: str,
    strategy_instance_id: str,
) -> ManagedBot:
    """Return the exact live task that may accept same-run controls."""
    managed = managed_bots.get(strategy_instance_id)
    if managed is None or managed.task.done():
        raise RunAdmissionRefusedError(
            "The bot has no authoritatively live run.",
            detail="Use Resume only after the prior run has terminal evidence.",
        )
    if managed.binding.broker != broker:
        raise UnknownBotError(
            f"Bot '{strategy_instance_id}' is not bound to broker '{broker}'.",
            detail=f"The bot's binding carries broker '{managed.binding.broker}'.",
        )
    return managed


async def execute_bot_run(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    *,
    run_gate: asyncio.Event | None,
    instance_dir: Path,
    source_bars: SourceBarLedger | None = None,
) -> None:
    """Execute one binding's configured mode behind its same-run pause gate."""
    run_feed = PauseAwareFeed(feed, run_gate) if run_gate is not None else feed
    if binding.mode == "trade":
        await run_trade_bot(binding, run_feed)
    elif binding.mode == "dry_run":
        await run_dry_run_bot(
            binding,
            run_feed,
            DryRunActivityJournal(instance_dir),
            source_bars=source_bars,
        )
    else:
        await _run_log_only_bot(binding, run_feed)


async def _run_log_only_bot(binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
    """Consume closed bars and log the walking-skeleton decision."""
    logger.info(
        "Log-only bot on duty",
        extra={
            "action": "bot_on_duty",
            "strategy_instance_id": binding.strategy_instance_id,
            "broker": binding.broker,
            "symbol": binding.symbol,
            "run_id": binding.run_id,
        },
    )
    async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        logger.info(
            "Log-only bot decision",
            extra={
                "action": "bot_decision",
                "strategy_instance_id": binding.strategy_instance_id,
                "broker": binding.broker,
                "decision": "HOLD",
                "symbol": bar.symbol,
                "bar_start_ms": bar.start_ms,
                "bar_end_ms": bar.end_ms,
                "close": str(bar.close),
            },
        )

"""Pinned Polygon fixture replay through the production minute-bar path."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.marketdata.feed import FeedHealth, MarketDataBar
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.schemas.account_custody_synthetic_qualification import (
    SyntheticPolygonReplayEvidence,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy import strategy_evaluations
from app.services.session_authority import session_state_at_ms

POLYGON_FIXTURE_SHA256 = "36d3c9358f4bcbfa89b29ec12493f566fdbd71a61ee2ef9d6c7f3bdd6e90d961"
SYNTHETIC_REPLAY_FEED_ID = "polygon_replay"


@dataclass(frozen=True)
class PolygonMinuteAggregate:
    """One pinned Polygon minute aggregate at the replay boundary."""

    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class _PolygonReplayIb:
    """Read-only transport that gives the live bar path deterministic prints."""

    def __init__(self, bars: list[SimpleNamespace]) -> None:
        self.bars = bars
        self.cancelled = False

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        contract.conId = 1_415
        contract.primaryExchange = "ARCA"
        return [contract]

    def reqRealTimeBars(
        self,
        contract: Any,
        bar_size: int,
        what_to_show: str,
        *,
        useRTH: bool,
    ) -> list[SimpleNamespace]:
        if contract.symbol != "SPY" or bar_size != 5 or what_to_show != "TRADES":
            raise ValueError("Polygon replay only supports SPY 5-second TRADES bars")
        if not useRTH:
            raise ValueError("qualification replay must use canonical RTH filtering")
        return self.bars

    def cancelRealTimeBars(self, bars: list[SimpleNamespace]) -> None:
        if bars is not self.bars:
            raise ValueError("attempted to cancel an unknown replay subscription")
        self.cancelled = True


class _PolygonReplayClient:
    def __init__(self, bars: list[SimpleNamespace]) -> None:
        self.ib = _PolygonReplayIb(bars)
        self.connection_lost = False

    def require_connected(self) -> None:
        return

    def is_connected(self) -> bool:
        return True


class PolygonReplayMarketDataFeed:
    """Feed-port adapter around the exact live IBKR aggregation pipeline."""

    feed_id = SYNTHETIC_REPLAY_FEED_ID

    def __init__(self, raw_bars: list[SimpleNamespace]) -> None:
        self._client = _PolygonReplayClient(raw_bars)
        self._delegate = IbkrMarketDataFeed(self._client)  # type: ignore[arg-type]

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]:
        async for bar in self._delegate.stream_bars(symbol, use_rth=use_rth):
            yield bar.model_copy(update={"feed_id": self.feed_id})

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        # The qualification harness proves exact reproduction of a pinned
        # fixture (see POLYGON_FIXTURE_SHA256); it deliberately never grows
        # extra history the fixture doesn't carry, so warmup is always
        # empty here. The replayed IBKR client also has no
        # ``reqHistoricalDataAsync`` stub to serve one.
        del symbol, use_rth, lookback_days
        return []

    def health(self, symbol: str | None = None) -> FeedHealth:
        return self._delegate.health(symbol)

    @property
    def subscription_cancelled(self) -> bool:
        return self._client.ib.cancelled


def load_polygon_minute_fixture(
    fixture_directory: Path,
) -> tuple[dict[str, Any], tuple[PolygonMinuteAggregate, ...]]:
    """Load and authenticate the pinned Polygon capture without network I/O."""

    bars_path = fixture_directory / "bars.json"
    metadata_path = fixture_directory / "metadata.json"
    encoded = bars_path.read_bytes()
    observed_sha256 = hashlib.sha256(encoded).hexdigest()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if observed_sha256 != POLYGON_FIXTURE_SHA256:
        raise ValueError("Polygon replay fixture SHA-256 does not match the pinned capture")
    if metadata.get("bars_sha256") != observed_sha256:
        raise ValueError("Polygon replay metadata does not authenticate bars.json")
    raw_rows = json.loads(encoded)
    if not isinstance(raw_rows, list) or len(raw_rows) != metadata.get("bar_count"):
        raise ValueError("Polygon replay fixture bar count does not match metadata")

    aggregates: list[PolygonMinuteAggregate] = []
    previous_timestamp: int | None = None
    for row in raw_rows:
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("Polygon replay timestamps must be nonnegative int64 ms UTC")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("Polygon replay timestamps must be unique and strictly increasing")
        previous_timestamp = timestamp
        volume = Decimal(str(row.get("volume")))
        if volume != volume.to_integral_value() or volume < 0:
            raise ValueError("Polygon replay volume must be a nonnegative whole number")
        aggregate = PolygonMinuteAggregate(
            timestamp=timestamp,
            open=Decimal(str(row.get("open"))),
            high=Decimal(str(row.get("high"))),
            low=Decimal(str(row.get("low"))),
            close=Decimal(str(row.get("close"))),
            volume=int(volume),
        )
        if not aggregate.low <= min(aggregate.open, aggregate.close):
            raise ValueError("Polygon replay aggregate low is inconsistent with open/close")
        if not aggregate.high >= max(aggregate.open, aggregate.close):
            raise ValueError("Polygon replay aggregate high is inconsistent with open/close")
        aggregates.append(aggregate)
    return metadata, tuple(aggregates)


def synthesize_polygon_5s_bars(
    aggregate: PolygonMinuteAggregate,
) -> tuple[SimpleNamespace, ...]:
    """Compose one Polygon minute into twelve exactly equivalent 5s bars.

    Formula: timestamps are ``minute_start + 5,000*i`` for ``i=0..11``;
    integer volume is distributed by ``divmod(volume, 12)``; the first raw
    bar carries the minute high/low and open, while the final raw bar closes
    at the minute close. Aggregating the twelve contributions therefore
    reproduces exact OHLCV with no interpolation claim.
    Reference: issue #1415 and the pinned Polygon fixture attribution.
    Canonical implementation: this function.
    Validated against: test_polygon_fixture_replays_through_live_feed_path.
    """

    quotient, remainder = divmod(aggregate.volume, 12)
    bars: list[SimpleNamespace] = []
    for index in range(12):
        value = aggregate.open if index < 11 else aggregate.close
        high = aggregate.high if index == 0 else max(value, aggregate.open)
        low = aggregate.low if index == 0 else min(value, aggregate.open)
        bars.append(
            SimpleNamespace(
                time=aggregate.timestamp + index * 5_000,
                open=aggregate.open,
                high=high,
                low=low,
                close=value,
                volume=quotient + (1 if index < remainder else 0),
            )
        )
    return tuple(bars)


def run_polygon_replay(
    fixture_directory: Path,
    *,
    replayed_minute_count: int = 3,
) -> SyntheticPolygonReplayEvidence:
    """Replay real Polygon structure through stream_minute_bars and the feed port."""

    metadata, aggregates = load_polygon_minute_fixture(fixture_directory)
    selected = _first_consecutive_rth_minutes(
        aggregates,
        required_count=replayed_minute_count + 1,
    )
    raw_bars = [
        raw
        for aggregate in selected
        for raw in synthesize_polygon_5s_bars(aggregate)
    ]
    feed = PolygonReplayMarketDataFeed(raw_bars)

    binding = BrokerBotBinding(
        strategy_instance_id="qualification-polygon-replay",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="qualification-polygon-replay-run",
        created_at_ms=selected[0].timestamp,
    )

    async def collect() -> tuple[Any, ...]:
        stream = strategy_evaluations(binding, feed)
        try:
            return tuple([await anext(stream) for _ in range(replayed_minute_count)])
        finally:
            await stream.aclose()

    decisions = asyncio.run(collect())
    emitted = tuple(decision.bar for decision in decisions)
    decision_outcomes = tuple(
        decision.intents[0].kind.value if decision.intents else "HOLD"
        for decision in decisions
    )
    if not feed.subscription_cancelled:
        raise AssertionError("Polygon replay did not release the live subscription")
    for expected, observed in zip(selected, emitted, strict=False):
        if (
            observed.start_ms,
            observed.open,
            observed.high,
            observed.low,
            observed.close,
            observed.volume,
            observed.feed_id,
            observed.session_phase,
        ) != (
            expected.timestamp,
            expected.open,
            expected.high,
            expected.low,
            expected.close,
            expected.volume,
            SYNTHETIC_REPLAY_FEED_ID,
            "RTH",
        ):
            raise AssertionError("Polygon replay did not reproduce exact closed-minute OHLCV")
    return SyntheticPolygonReplayEvidence(
        fixture_ref=str(fixture_directory),
        fixture_sha256=metadata["bars_sha256"],
        fixture_bar_count=metadata["bar_count"],
        replayed_minute_count=replayed_minute_count,
        synthesized_5s_count=len(raw_bars),
        emitted_minute_count=len(emitted),
        decision_count=len(decisions),
        decision_outcomes=decision_outcomes,
    )


def _first_consecutive_rth_minutes(
    aggregates: tuple[PolygonMinuteAggregate, ...],
    *,
    required_count: int,
) -> tuple[PolygonMinuteAggregate, ...]:
    candidate: list[PolygonMinuteAggregate] = []
    for aggregate in aggregates:
        if session_state_at_ms(now_ms=aggregate.timestamp).phase != "RTH":
            candidate.clear()
            continue
        if candidate and aggregate.timestamp != candidate[-1].timestamp + 60_000:
            candidate.clear()
        candidate.append(aggregate)
        if len(candidate) == required_count:
            return tuple(candidate)
    raise ValueError(f"Polygon fixture has no {required_count}-minute consecutive RTH window")

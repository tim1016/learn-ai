"""Tests for GalleryHub snapshot composition (Task 2, bot gallery)."""

from __future__ import annotations

import pytest

from app.services.broker_v2_panel.gallery_hub import GalleryHub, running_symbols


class _Cat:
    def __init__(self, sid: str, symbol: str, running: bool) -> None:
        self.strategy_instance_id = sid
        self.symbol = symbol
        self.running = running


def test_running_symbols_dedupes_and_keeps_only_running() -> None:
    cat = [_Cat("a", "SPY", True), _Cat("b", "SPY", True), _Cat("c", "QQQ", True), _Cat("d", "IWM", False)]
    assert running_symbols(cat) == ["SPY", "QQQ"]


class _Cat2:
    """Richer catalog stub mirroring the ``BotCatalogView`` fields GalleryHub reads."""

    def __init__(
        self,
        sid: str,
        symbol: str,
        running: bool,
        realized_pnl_today: float,
        open_pnl: float,
        fills_today: int,
        *,
        strategy_label: str = "",
        needs_attention: bool = False,
    ) -> None:
        self.strategy_instance_id = sid
        self.symbol = symbol
        self.running = running
        self.strategy_label = strategy_label
        self.realized_pnl_today = realized_pnl_today
        self.open_pnl = open_pnl
        self.fills_today = fills_today
        self.needs_attention = needs_attention


class _FakeCatalogSource:
    def __init__(self, rows: list[_Cat2]) -> None:
        self._rows = rows

    async def get_catalog(self, broker: str, account_id: str) -> list[_Cat2]:
        return self._rows


class _FakeAggregator:
    def __init__(self) -> None:
        self.subscribed: list[str] = []

    def ensure_subscribed(self, symbol: str) -> None:
        self.subscribed.append(symbol)

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[object]:
        return [
            type(
                "B",
                (),
                {
                    "start_ms": 1_700_000_000_000,
                    "end_ms": 1_700_000_060_000,
                    "open": 1.0,
                    "high": 1.2,
                    "low": 0.9,
                    "close": 1.1,
                    "volume": 100,
                    "source": "ibkr",
                },
            )()
        ]


@pytest.mark.asyncio
async def test_build_snapshot_subscribes_once_per_symbol_and_projects_bots() -> None:
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12),
        _Cat2("Aug11-03", "SPY", True, 10.0, 0.0, 3),
    ]
    aggregator = _FakeAggregator()
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=aggregator,
    )

    snap = await hub.build_snapshot()

    assert [s.symbol for s in snap.symbols] == ["SPY"]  # deduped
    assert {b.sid for b in snap.bots} == {"Aug11-02", "Aug11-03"}
    assert snap.surface_version == 1
    assert snap.resolution == "1m"
    assert aggregator.subscribed == ["SPY"]  # subscribed exactly once

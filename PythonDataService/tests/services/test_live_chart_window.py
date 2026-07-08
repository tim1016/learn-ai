from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.data_lake.polygon_fetcher import PolygonBar
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.services import live_chart_window as chart_mod
from app.services.bar_persistence import BarPersistence
from app.services.live_chart_window import ChartWindowError, resolve_chart_window, validate_chart_window


def _bar(symbol: str, start_ms: int, close: str, *, source: str = "ibkr") -> IbkrMinuteBar:
    return IbkrMinuteBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start_ms + 60_000,
        source=source,
    )


class _FakeAggregator:
    def __init__(self, persistence: BarPersistence) -> None:
        self._persistence = persistence

    def snapshot(self, _symbol: str):
        return []

    def snapshot_5s(self, _symbol: str):
        return []

    def status(self, _symbol: str):
        return "idle", None, None

    def status_5s(self, _symbol: str):
        return "idle", None, None


@pytest.mark.asyncio
async def test_polygon_overlay_is_not_persisted_and_aggregates_from_minute_base(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = session_window_for_date(date(2026, 7, 7))
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar("SPY", session.open_ms_utc, "100.00"))

    async def fake_fetch(symbol: str, start: date, end: date, api_key: str):
        assert (symbol, start, end, api_key) == ("SPY", session.session_date, session.session_date, "key")
        return [
            PolygonBar(
                t_ms=session.open_ms_utc + i * 60_000,
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=10 + i,
                vwap=100.0 + i,
                n=1,
            )
            for i in range(3)
        ]

    monkeypatch.setattr(chart_mod, "fetch_minute_trade_aggregates", fake_fetch)

    result = await resolve_chart_window(
        symbol="SPY",
        timeframe="5m",
        from_ms=session.open_ms_utc,
        to_ms=session.open_ms_utc + 3 * 60_000,
        now_ms=session.close_ms_utc,
        polygon_api_key="key",
        live_aggregator=_FakeAggregator(store),
    )

    assert len(result.bars) == 1
    bucket = result.bars[0]
    assert bucket.source == "mixed"
    assert bucket.open == Decimal("100.00")
    assert bucket.close == Decimal("102.5")
    assert bucket.volume == 123
    assert result.overlay_notices == []
    assert [bar.start_ms for bar in store.replay("SPY", "1m", session.session_date)] == [
        session.open_ms_utc
    ]


def test_validate_chart_window_rejects_more_than_seven_days() -> None:
    now_ms = 1_783_531_200_000
    with pytest.raises(ChartWindowError, match="7 days"):
        validate_chart_window(
            from_ms=now_ms - chart_mod.MAX_CHART_RANGE_MS - 60_000,
            to_ms=now_ms,
            now_ms=now_ms,
        )

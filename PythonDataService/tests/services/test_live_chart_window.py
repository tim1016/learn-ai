from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from app.broker.ibkr.models import IbkrMinuteBar
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
        session_phase="RTH",
        use_rth=True,
    )


class _FakeAggregator:
    def __init__(self, persistence: BarPersistence) -> None:
        self._persistence = persistence

    def snapshot(self, _symbol: str) -> list[IbkrMinuteBar]:
        return []

    def snapshot_5s(self, _symbol: str) -> list[IbkrMinuteBar]:
        return []

    def status(self, _symbol: str) -> tuple[str, None, None]:
        return "idle", None, None

    def status_5s(self, _symbol: str) -> tuple[str, None, None]:
        return "idle", None, None


def _polygon_aggs_pattern(session: date) -> re.Pattern:
    day = session.isoformat()
    return re.compile(rf"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/{day}/{day}")


def _polygon_payload(session_open_ms: int, count: int) -> dict[str, object]:
    return {
        "ticker": "SPY",
        "status": "OK",
        "results": [
            {
                "t": session_open_ms + i * 60_000,
                "o": 100.0 + i,
                "h": 101.0 + i,
                "l": 99.0 + i,
                "c": 100.5 + i,
                "v": 10 + i,
                "vw": 100.0 + i,
                "n": 1,
            }
            for i in range(count)
        ],
    }


@pytest.fixture(autouse=True)
def clear_polygon_overlay_cache() -> None:
    chart_mod._POLYGON_OVERLAY_CACHE.clear()


@pytest.mark.asyncio
@respx.mock
async def test_polygon_overlay_is_not_persisted_and_aggregates_from_minute_base(
    tmp_path: Path,
) -> None:
    session = session_window_for_date(date(2026, 7, 7))
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar("SPY", session.open_ms_utc, "100.00"))
    route = respx.get(_polygon_aggs_pattern(session.session_date)).mock(
        return_value=httpx.Response(200, json=_polygon_payload(session.open_ms_utc, 3))
    )

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
    assert bucket.provenance == "mixed"
    assert bucket.venue == "MIXED"
    assert bucket.session_phase == "RTH"
    assert bucket.use_rth is True
    assert bucket.open == Decimal("100.00")
    assert bucket.close == Decimal("102.5")
    assert bucket.volume == 123
    assert result.overlay_notices == []
    assert [bar.start_ms for bar in store.replay("SPY", "1m", session.session_date)] == [
        session.open_ms_utc
    ]

    cached = await resolve_chart_window(
        symbol="SPY",
        timeframe="5m",
        from_ms=session.open_ms_utc,
        to_ms=session.open_ms_utc + 3 * 60_000,
        now_ms=session.close_ms_utc,
        polygon_api_key="key",
        live_aggregator=_FakeAggregator(store),
    )
    assert cached.bars[0].close == Decimal("102.5")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_polygon_overlay_excludes_the_in_progress_minute(tmp_path: Path) -> None:
    session = session_window_for_date(date(2026, 7, 7))
    route = respx.get(_polygon_aggs_pattern(session.session_date)).mock(
        return_value=httpx.Response(200, json=_polygon_payload(session.open_ms_utc, 3))
    )

    result = await resolve_chart_window(
        symbol="SPY",
        timeframe="1m",
        from_ms=session.open_ms_utc,
        to_ms=session.open_ms_utc + 150_000,
        now_ms=session.open_ms_utc + 150_000,
        polygon_api_key="key",
        live_aggregator=_FakeAggregator(BarPersistence(root=tmp_path)),
    )

    assert [bar.start_ms for bar in result.bars] == [
        session.open_ms_utc,
        session.open_ms_utc + 60_000,
    ]
    assert route.call_count == 1


def test_validate_chart_window_rejects_more_than_seven_days() -> None:
    now_ms = 1_783_531_200_000
    with pytest.raises(ChartWindowError, match="7 days"):
        validate_chart_window(
            from_ms=now_ms - chart_mod.MAX_CHART_RANGE_MS - 60_000,
            to_ms=now_ms,
            now_ms=now_ms,
        )

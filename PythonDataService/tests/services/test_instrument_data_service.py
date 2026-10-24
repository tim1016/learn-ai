"""``read_instrument_data``: one entry point that refuses before it reads and then reads together (#3503).

The coverage and the derived daily file are independent reads, so neither waits for the other, and a request the
coverage read refuses starts neither. The reads themselves are replaced here; ``test_instrument_data_admission``
drives the real ones.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest

from app.data_lake import catalog_client
from app.data_lake.coverage import CoverageRequestError, CoverageWindow
from app.data_lake.types import CoverageResponse
from app.schemas.instrument_data import DailyRollup
from app.services import instrument_data_service

WEEK_START, WEEK_END = date(2024, 5, 20), date(2024, 5, 24)


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)


def _empty_coverage(window: CoverageWindow) -> CoverageResponse:
    return CoverageResponse(
        market="usa",
        symbol=window.symbol,
        data_type="trade",
        resolution="minute",
        provider="polygon",
        price_adjustment_mode="raw",
        data_root_id=UUID(int=0),
        days=[],
    )


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The reads the service started, in order; the pool is a no-op."""
    started: list[str] = []

    async def _no_pool() -> None:
        started.append("pool")

    monkeypatch.setattr(catalog_client, "init_pool", _no_pool)
    return started


async def test_the_coverage_and_the_daily_file_are_read_together(reads: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    daily_started = asyncio.Event()

    async def _coverage(window: CoverageWindow, **_: Any) -> CoverageResponse:
        reads.append("coverage")
        # Done only once the daily read has started: a read that waited for this one first would never see it.
        await asyncio.wait_for(daily_started.wait(), timeout=2)
        return _empty_coverage(window)

    async def _daily(**_: Any) -> DailyRollup:
        reads.append("daily")
        daily_started.set()
        return DailyRollup(state="not_built")

    monkeypatch.setattr(instrument_data_service, "read_coverage", _coverage)
    monkeypatch.setattr(instrument_data_service, "read_daily_rollup", _daily)

    report = await instrument_data_service.read_instrument_data(
        symbol="SYNTH",
        start_trading_date_ms=_ms(WEEK_START),
        end_trading_date_ms=_ms(WEEK_END),
        price_adjustment_mode="raw",
    )

    assert sorted(reads) == ["coverage", "daily", "pool"]
    assert report.daily_rollup.state == "not_built"
    assert report.symbol == "SYNTH"


@pytest.mark.parametrize(
    ("symbol", "start", "end", "reason"),
    [
        ("not a symbol", _ms(WEEK_START), _ms(WEEK_END), "invalid_symbol"),
        ("SYNTH", _ms(WEEK_END), _ms(WEEK_START), "invalid_range"),
    ],
)
async def test_a_refused_request_starts_no_read(
    reads: list[str], monkeypatch: pytest.MonkeyPatch, symbol: str, start: int, end: int, reason: str
) -> None:
    async def _read(*_: Any, **__: Any) -> None:
        reads.append("read")
        raise AssertionError("a refused request must not reach a read")

    monkeypatch.setattr(instrument_data_service, "read_coverage", _read)
    monkeypatch.setattr(instrument_data_service, "read_daily_rollup", _read)

    with pytest.raises(CoverageRequestError) as refused:
        await instrument_data_service.read_instrument_data(
            symbol=symbol, start_trading_date_ms=start, end_trading_date_ms=end, price_adjustment_mode="raw"
        )

    assert refused.value.reason == reason
    assert refused.value.detail == {"reason": reason, "message": refused.value.message}
    assert reads == []

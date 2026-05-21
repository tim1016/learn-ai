"""Unit tests for ensure_data with fixture-backed Polygon."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataRunSpec


def _spec(symbols: list[str]) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        lean_image_digest="sha256:test",
    )


@pytest.mark.asyncio
async def test_known_symbol_produces_complete_result():
    result = await ensure_data(_spec(["SPY"]))
    assert result.overall_status == "complete"
    assert result.failures == []
    assert len(result.artifacts) > 0
    assert all(a.symbol in {None, "SPY"} for a in result.artifacts)


@pytest.mark.asyncio
async def test_unknown_symbol_produces_partial_with_failures():
    result = await ensure_data(_spec(["UNKNOWN"]))
    assert result.overall_status in {"partial", "failed"}
    assert len(result.failures) > 0
    assert any(f.reason == "unknown_symbol" for f in result.failures)


@pytest.mark.asyncio
async def test_two_identical_calls_produce_same_availability_hash():
    a = await ensure_data(_spec(["SPY"]))
    b = await ensure_data(_spec(["SPY"]))
    assert a.data_availability_hash == b.data_availability_hash

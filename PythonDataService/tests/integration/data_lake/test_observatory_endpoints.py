"""End-to-end tests for the Observatory read endpoints (issue #1835).

GET /api/data-lake/coverage, /artifacts/{id}, /storage-summary. Same pattern
as test_ensure_data_route.py: build a minimal FastAPI app that includes the
data_lake router directly, so no app.main reload or settings override is
needed to exercise the flag-on behavior. catalog_client's Postgres-backed
functions are monkeypatched at the module level so these tests run without a
live database.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.data_lake import catalog_client
from app.data_lake.catalog_client import (
    ArtifactCoverageRow,
    ArtifactDetailRow,
    StorageKindTotalRow,
    SymbolCoverageSpanRow,
)
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.routers.data_lake import router as data_lake_router

pytestmark = pytest.mark.asyncio


def _make_app(*, include_data_lake: bool) -> FastAPI:
    """Minimal FastAPI app that mirrors main.py's conditional router wiring."""
    app = FastAPI()
    if include_data_lake:
        app.include_router(data_lake_router)
    return app


async def _get(app: FastAPI, url: str) -> tuple[int, dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(url)
    return r.status_code, (r.json() if r.content else {})


# ---------------------------------------------------------------------------
# Flag-off behavior — routes 404 when the router is not registered.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
        "/api/data-lake/artifacts/1",
        "/api/data-lake/storage-summary",
    ],
)
async def test_observatory_routes_404_when_flag_off(url: str):
    flag_off_app = _make_app(include_data_lake=False)
    status_code, _ = await _get(flag_off_app, url)
    assert status_code == 404


# ---------------------------------------------------------------------------
# Coverage — calendar-keyed, honest-missing, honest-empty.
# ---------------------------------------------------------------------------


async def test_coverage_keys_days_by_canonical_calendar_sessions_only(monkeypatch: pytest.MonkeyPatch):
    """2024-05-20 (Mon) .. 2024-05-26 (Sun) has 5 NYSE sessions and a weekend.

    An empty catalog must report every session as "missing" (honest, not
    invented) and must never emit an entry for the weekend days.
    """

    async def _empty_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _empty_coverage)

    app = _make_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-26",
    )

    assert status_code == 200
    sessions = expected_sessions(date(2024, 5, 20), date(2024, 5, 26))
    assert sessions == [date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22), date(2024, 5, 23), date(2024, 5, 24)]
    assert len(body["days"]) == len(sessions)
    for day, session_date in zip(body["days"], sessions, strict=True):
        assert day["status"] == "missing"
        assert day["artifact_id"] is None
        assert isinstance(day["trading_date_ms"], int)
        assert day["trading_date_ms"] == session_open_ms_utc(session_date)


async def test_coverage_reflects_catalog_status_per_day(monkeypatch: pytest.MonkeyPatch):
    """A day with a catalog row reports that row's real status, not "missing"."""

    async def _mixed_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        return [
            ArtifactCoverageRow(trading_date=date(2024, 5, 20), status="complete", artifact_id=101),
            ArtifactCoverageRow(trading_date=date(2024, 5, 22), status="failed", artifact_id=102),
        ]

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _mixed_coverage)

    app = _make_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )

    assert status_code == 200
    by_ms = {d["trading_date_ms"]: d for d in body["days"]}
    complete_day = by_ms[session_open_ms_utc(date(2024, 5, 20))]
    failed_day = by_ms[session_open_ms_utc(date(2024, 5, 22))]
    still_missing_day = by_ms[session_open_ms_utc(date(2024, 5, 21))]

    assert complete_day["status"] == "complete"
    assert complete_day["artifact_id"] == 101
    assert failed_day["status"] == "failed"
    assert failed_day["artifact_id"] == 102
    assert still_missing_day["status"] == "missing"
    assert still_missing_day["artifact_id"] is None


async def test_coverage_422_when_range_inverted():
    app = _make_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-24&end_trading_date=2024-05-20",
    )
    assert status_code == 422
    assert body["detail"]["reason"] == "invalid_range"


# ---------------------------------------------------------------------------
# Artifact detail — full receipt, int64 ms UTC timestamps, honest 404.
# ---------------------------------------------------------------------------


async def test_artifact_detail_returns_full_receipt_with_int_ms_timestamps(monkeypatch: pytest.MonkeyPatch):
    async def _detail(artifact_id: int) -> ArtifactDetailRow:
        assert artifact_id == 101
        return ArtifactDetailRow(
            id=101,
            artifact_kind="time_series_bars",
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            resolution="minute",
            data_type="trade",
            provider="polygon",
            provider_params={"adjusted": "true"},
            price_adjustment_mode="raw",
            data_contract_hash="a" * 64,
            content_hash="b" * 64,
            file_path="equity/usa/minute/spy/20240520_trade.zip",
            file_size_bytes=123456,
            status="complete",
            row_count=390,
            first_bar_start_ms=1716196200000,
            last_bar_start_ms=1716219540000,
            fetched_at_ms=1716220000000,
            completed_at_ms=1716220050000,
        )

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _detail)

    app = _make_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/101")

    assert status_code == 200
    # Receipt completeness: content hash, data-contract hash, size, fetch
    # timestamp, and provider params are all present.
    assert body["content_hash"] == "b" * 64
    assert body["data_contract_hash"] == "a" * 64
    assert body["file_size_bytes"] == 123456
    assert body["provider_params"] == {"adjusted": "true"}
    assert body["status"] == "complete"
    # Every temporal field on the wire is int64 ms UTC — never an ISO string.
    for field in ("trading_date_ms", "fetched_at_ms", "completed_at_ms", "first_bar_start_ms", "last_bar_start_ms"):
        assert isinstance(body[field], int), f"{field} must be int64 ms UTC, got {type(body[field])}"
    assert body["trading_date_ms"] == session_open_ms_utc(date(2024, 5, 20))


async def test_artifact_detail_trading_date_ms_is_none_for_non_day_keyed_artifacts(monkeypatch: pytest.MonkeyPatch):
    """factor_file/map_file/metadata rows carry no TradingDate — must stay None, not fabricated."""

    async def _detail(artifact_id: int) -> ArtifactDetailRow:
        return ArtifactDetailRow(
            id=5,
            artifact_kind="metadata",
            market="usa",
            symbol="SPY",
            trading_date=None,
            resolution=None,
            data_type=None,
            provider="polygon",
            provider_params={},
            price_adjustment_mode=None,
            data_contract_hash="c" * 64,
            content_hash="",
            file_path="equity/usa/metadata/spy.json",
            file_size_bytes=512,
            status="complete",
            row_count=None,
            first_bar_start_ms=None,
            last_bar_start_ms=None,
            fetched_at_ms=1716220000000,
            completed_at_ms=1716220050000,
        )

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _detail)

    app = _make_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/5")

    assert status_code == 200
    assert body["trading_date_ms"] is None
    assert body["content_hash"] == ""


async def test_artifact_detail_404_when_row_does_not_exist(monkeypatch: pytest.MonkeyPatch):
    async def _missing(artifact_id: int) -> None:
        return None

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _missing)

    app = _make_app(include_data_lake=True)
    status_code, _ = await _get(app, "/api/data-lake/artifacts/999")
    assert status_code == 404


# ---------------------------------------------------------------------------
# Storage summary — counts/bytes by kind, per-symbol span, honest-empty.
# ---------------------------------------------------------------------------


async def test_storage_summary_reports_counts_bytes_and_symbol_spans(monkeypatch: pytest.MonkeyPatch):
    async def _kinds(market: str) -> list[StorageKindTotalRow]:
        return [
            StorageKindTotalRow(artifact_kind="time_series_bars", resolution="minute", artifact_count=42, total_bytes=1_048_576),
            StorageKindTotalRow(artifact_kind="factor_file", resolution=None, artifact_count=1, total_bytes=2048),
        ]

    async def _spans(market: str) -> list[SymbolCoverageSpanRow]:
        return [
            SymbolCoverageSpanRow(
                symbol="SPY",
                first_trading_date=date(2024, 5, 20),
                last_trading_date=date(2024, 5, 24),
                artifact_count=5,
            )
        ]

    monkeypatch.setattr(catalog_client, "select_storage_totals_by_kind", _kinds)
    monkeypatch.setattr(catalog_client, "select_symbol_coverage_spans", _spans)

    app = _make_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/storage-summary")

    assert status_code == 200
    assert body["market"] == "usa"
    kinds_by_name = {k["artifact_kind"]: k for k in body["kinds"]}
    assert kinds_by_name["time_series_bars"]["artifact_count"] == 42
    assert kinds_by_name["time_series_bars"]["total_bytes"] == 1_048_576
    assert kinds_by_name["factor_file"]["resolution"] is None

    assert len(body["symbols"]) == 1
    span = body["symbols"][0]
    assert span["symbol"] == "SPY"
    assert span["artifact_count"] == 5
    assert span["first_trading_date_ms"] == session_open_ms_utc(date(2024, 5, 20))
    assert span["last_trading_date_ms"] == session_open_ms_utc(date(2024, 5, 24))


async def test_storage_summary_honest_empty_on_empty_catalog(monkeypatch: pytest.MonkeyPatch):
    async def _no_kinds(market: str) -> list[StorageKindTotalRow]:
        return []

    async def _no_spans(market: str) -> list[SymbolCoverageSpanRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_storage_totals_by_kind", _no_kinds)
    monkeypatch.setattr(catalog_client, "select_symbol_coverage_spans", _no_spans)

    app = _make_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/storage-summary")

    assert status_code == 200
    assert body["kinds"] == []
    assert body["symbols"] == []

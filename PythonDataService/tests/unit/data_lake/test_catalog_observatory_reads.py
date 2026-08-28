"""Live-Postgres unit tests for the Task 5 observatory read projections.

Skips when POSTGRES_URL is unset (same pattern as test_catalog_write_ops.py
and test_schema_drift.py). Exercises select_artifact_coverage,
select_artifact_by_id, select_storage_totals_by_kind, and
select_symbol_coverage_spans against a real schema — the row under test is
inserted via the catalog's own claim/complete write path (claim_minute_bar +
complete_artifact), not hand-crafted SQL, so the read projections are
verified against exactly what the write path produces.
"""

from __future__ import annotations

import os
from datetime import date

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.types import ArtifactIdentity
from app.lean_sidecar.trading_calendar import session_open_ms_utc

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Truncate DataLakeArtifacts before+after each test."""
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    # Force-reset any stale pool left by a prior test (different event loop).
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


def _minute_identity(date_val: date = date(2024, 5, 20)) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date_val,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


async def test_observatory_selects_read_a_real_complete_artifact(clean_artifacts, pool):
    """One completed minute-bar row, read back through all four new SELECTs."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert isinstance(artifact_id, int)

    # Before completion: content_hash must be None, never "" — MAJOR 4 from
    # the #1835 review round 2 (select_artifact_by_id has no Status filter,
    # so a still-fetching row must not emit an empty string as if it were a
    # real hash on a documented receipt surface).
    fetching_detail = await catalog_client.select_artifact_by_id(artifact_id)
    assert fetching_detail is not None
    assert fetching_detail.status == "fetching"
    assert fetching_detail.content_hash is None
    assert fetching_detail.file_size_bytes is None

    await catalog_client.complete_artifact(
        artifact_id,
        row_count=390,
        first_bar_start_ms=1716196200000,
        last_bar_start_ms=1716219540000,
        file_size_bytes=123456,
        file_sha256="b" * 64,
    )

    # select_artifact_coverage: the completed day shows up with its real status.
    coverage = await catalog_client.select_artifact_coverage(
        market="usa",
        symbol="SPY",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
    )
    assert len(coverage) == 1
    assert coverage[0].trading_date == date(2024, 5, 20)
    assert coverage[0].status == "complete"
    assert coverage[0].artifact_id == artifact_id

    # select_artifact_by_id: full receipt round-trips through real Postgres,
    # including the jsonb ProviderParams decode.
    detail = await catalog_client.select_artifact_by_id(artifact_id)
    assert detail is not None
    assert detail.content_hash == "b" * 64
    assert detail.data_contract_hash == "a" * 64
    assert detail.file_size_bytes == 123456
    assert detail.provider_params == {}
    assert detail.status == "complete"
    assert detail.trading_date_ms == session_open_ms_utc(date(2024, 5, 20))
    # A row that never failed carries no diagnostics — attempt_count reflects
    # the single claim_minute_bar() insert, last_error/error_message are None.
    assert detail.attempt_count == 1
    assert detail.last_error is None
    assert detail.error_message is None

    # select_storage_totals_by_kind: one complete time_series_bars/minute row.
    totals = await catalog_client.select_storage_totals_by_kind("usa")
    assert len(totals) == 1
    assert totals[0].artifact_kind == "time_series_bars"
    assert totals[0].resolution == "minute"
    assert totals[0].artifact_count == 1
    assert totals[0].total_bytes == 123456

    # select_symbol_coverage_spans: SPY's span is the single completed day.
    spans = await catalog_client.select_symbol_coverage_spans("usa")
    assert len(spans) == 1
    assert spans[0].symbol == "SPY"
    assert spans[0].first_trading_date_ms == session_open_ms_utc(date(2024, 5, 20))
    assert spans[0].last_trading_date_ms == session_open_ms_utc(date(2024, 5, 20))
    assert spans[0].artifact_count == 1


async def test_observatory_selects_honest_empty_on_a_truncated_catalog(clean_artifacts, pool):
    """No rows at all: every projection returns an empty list, not an error."""
    coverage = await catalog_client.select_artifact_coverage(
        market="usa",
        symbol="SPY",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
    )
    assert coverage == []
    assert await catalog_client.select_artifact_by_id(1) is None
    assert await catalog_client.select_storage_totals_by_kind("usa") == []
    assert await catalog_client.select_symbol_coverage_spans("usa") == []


async def test_artifact_detail_carries_failure_diagnostics(clean_artifacts, pool):
    """#1845 P2-6: a failed row's receipt must carry what fail_artifact() persisted."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert isinstance(artifact_id, int)

    await catalog_client.fail_artifact(
        artifact_id,
        last_error="provider_rate_limited",
        error_message="429 Too Many Requests from Polygon",
    )

    detail = await catalog_client.select_artifact_by_id(artifact_id)
    assert detail is not None
    assert detail.status == "failed"
    assert detail.attempt_count == 1  # fail_artifact doesn't increment; only retry does
    assert detail.last_error == "provider_rate_limited"
    assert detail.error_message == "429 Too Many Requests from Polygon"
    assert detail.content_hash is None
    assert detail.file_size_bytes is None


async def test_symbol_coverage_spans_excludes_aggregated_only_symbols(clean_artifacts, pool):
    """#1845 P2-3: a symbol with ONLY a daily (aggregated) artifact produces no span row.

    Daily/hour time_series_bars rows carry TradingDate=NULL (one row covers
    the symbol's whole history — uq_data_lake_artifacts_aggregated_bars).
    Before the Resolution='minute' filter, GROUP BY Symbol still emitted a
    row for such a symbol with artifact_count=0 and null first/last dates —
    a fabricated placeholder, not the honest absence the span concept
    documents.
    """
    daily_identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="QQQ",
        trading_date=None,
        resolution="daily",
        data_type="trade",
        provider="learn_ai_derived",
        price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_aggregated_bar_artifact(
        identity=daily_identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="e" * 64,
        file_path="equity/usa/daily/qqq/trade.zip",
    )
    assert isinstance(artifact_id, int)
    await catalog_client.complete_artifact(
        artifact_id,
        row_count=1000,
        first_bar_start_ms=1577836800000,
        last_bar_start_ms=1716220000000,
        file_size_bytes=999,
        file_sha256="f" * 64,
    )

    spans = await catalog_client.select_symbol_coverage_spans("usa")
    assert spans == []

    # The kind/bytes rollup still counts it — only the day-keyed span excludes it.
    totals = await catalog_client.select_storage_totals_by_kind("usa")
    assert len(totals) == 1
    assert totals[0].artifact_kind == "time_series_bars"
    assert totals[0].resolution == "daily"
    assert totals[0].artifact_count == 1


async def test_coverage_finds_a_real_quote_artifact_seeded_under_learn_ai_derived(clean_artifacts, pool):
    """#1845 P1-2: quote coverage over a seeded quote artifact reports "complete".

    expand_required_artifacts catalogs quote minute-bars under
    Provider='learn_ai_derived' (see provider_for_data_type), not 'polygon'.
    Before the fix, the coverage endpoint always filtered on
    Provider='polygon' regardless of data_type, so this row could never
    match a quote coverage query.
    """
    quote_identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 21),
        resolution="minute",
        data_type="quote",
        provider="learn_ai_derived",
        price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_minute_bar(
        identity=quote_identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="g" * 64,
        file_path="equity/usa/minute/spy/20240521_quote.zip",
    )
    assert isinstance(artifact_id, int)
    await catalog_client.complete_artifact(
        artifact_id,
        row_count=390,
        first_bar_start_ms=1716282600000,
        last_bar_start_ms=1716305940000,
        file_size_bytes=54321,
        file_sha256="h" * 64,
    )

    coverage = await catalog_client.select_artifact_coverage(
        market="usa",
        symbol="SPY",
        data_type="quote",
        provider="learn_ai_derived",
        price_adjustment_mode="raw",
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
    )
    assert len(coverage) == 1
    assert coverage[0].trading_date == date(2024, 5, 21)
    assert coverage[0].status == "complete"
    assert coverage[0].artifact_id == artifact_id

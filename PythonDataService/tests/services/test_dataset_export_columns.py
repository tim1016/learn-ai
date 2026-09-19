"""dataset.csv column selection and the readable time column (owner decision 2026-09-19).

``unix_ts`` (int64 ms UTC) is always the first column and stays the
canonical time. An optional ``time_<zone>`` column right after it renders
the same instant as ``YYYY-MM-DD HH:MM:SS`` wall-clock in one IANA zone —
display only, never parsed back. A column selection keeps the canonical
projection order and fails loudly on any name the dataset cannot carry.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime
from typing import Any

import httpx
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from app.models.requests import DatasetGenerationRequest, DatasetPlanRequest
from app.routers import dataset as dataset_router
from app.services.dataset_plan_service import build_dataset_plan, prepare_generation_request
from app.services.dataset_service import (
    build_csv_bytes,
    select_output_columns,
    time_column_name,
)

_RECIPE: dict[str, Any] = {
    "ticker": "SPY",
    "from_date": "2025-03-07",
    "to_date": "2025-03-10",
    "indicator_entries": [{"name": "rsi", "params": {"length": 14}}],
    "include_previous_close": False,
}


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def _frame(timestamps: list[int]) -> pd.DataFrame:
    n = len(timestamps)
    close = pd.Series([100.0 + i for i in range(n)], dtype="float64")
    return pd.DataFrame(
        {
            "timestamp": pd.Series(timestamps, dtype="int64"),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": pd.Series([1_000] * n, dtype="int64"),
            "vwap": close,
            "transactions": pd.Series([10] * n, dtype="int64"),
            "session": "rth",
            "rsi_length14": pd.Series([50.0] * n, dtype="float64"),
        }
    )


_RSI_META: list[dict[str, Any]] = [
    {"column": "rsi_length14", "indicator": "rsi", "params": "length=14", "library": "pandas-ta"},
]


def _rows(csv_bytes: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))


# ── Column selection ─────────────────────────────────────────────

_PROJECTION = ["open", "high", "low", "close", "volume", "vwap", "transactions", "session", "rsi_length14"]


def test_select_output_columns_none_keeps_full_projection() -> None:
    assert select_output_columns(_PROJECTION, None) == _PROJECTION


def test_select_output_columns_keeps_canonical_order() -> None:
    assert select_output_columns(_PROJECTION, ["rsi_length14", "close", "open"]) == ["open", "close", "rsi_length14"]


def test_select_output_columns_empty_selection_exports_no_data_columns() -> None:
    assert select_output_columns(_PROJECTION, []) == []


def test_select_output_columns_unknown_name_fails_loudly() -> None:
    with pytest.raises(ValueError, match="ema_20"):
        select_output_columns(_PROJECTION, ["close", "ema_20"])


def test_select_output_columns_unix_ts_is_not_selectable() -> None:
    with pytest.raises(ValueError, match="unix_ts"):
        select_output_columns(_PROJECTION, ["unix_ts", "close"])


# ── Readable time column ─────────────────────────────────────────


@pytest.mark.parametrize(
    "zone,expected",
    [
        ("America/Chicago", "time_america_chicago"),
        ("UTC", "time_utc"),
        ("America/Argentina/Buenos_Aires", "time_america_argentina_buenos_aires"),
    ],
)
def test_time_column_name_is_the_zone_slug(zone: str, expected: str) -> None:
    assert time_column_name(zone) == expected


def test_build_csv_bytes_without_time_zone_keeps_unix_ts_only_header() -> None:
    rows = _rows(build_csv_bytes(_frame([_ms(2025, 3, 7, 14, 30)]), ["close"]))
    assert rows[0] == ["unix_ts", "close"]
    assert rows[1] == [str(_ms(2025, 3, 7, 14, 30)), "100.000000"]


def test_build_csv_bytes_time_column_follows_unix_ts_across_spring_forward() -> None:
    """09:30 New York is 14:30 UTC before the 2025-03-09 DST switch and 13:30 UTC after it."""
    before, after = _ms(2025, 3, 7, 14, 30), _ms(2025, 3, 10, 13, 30)
    df = _frame([before, after])

    new_york = _rows(build_csv_bytes(df, ["close"], time_zone="America/New_York"))
    assert new_york[0] == ["unix_ts", "time_america_new_york", "close"]
    assert new_york[1][:2] == [str(before), "2025-03-07 09:30:00"]
    assert new_york[2][:2] == [str(after), "2025-03-10 09:30:00"]

    chicago = _rows(build_csv_bytes(df, ["close"], time_zone="America/Chicago"))
    assert [row[1] for row in chicago[1:]] == ["2025-03-07 08:30:00", "2025-03-10 08:30:00"]


def test_build_csv_bytes_fall_back_hour_repeats_and_unix_ts_disambiguates() -> None:
    """01:30 happens twice in Chicago on 2025-11-02 (CDT, then CST). The plain
    readable format repeats; ``unix_ts`` is what keeps the two bars distinct."""
    first, second = _ms(2025, 11, 2, 6, 30), _ms(2025, 11, 2, 7, 30)
    rows = _rows(build_csv_bytes(_frame([first, second]), ["close"], time_zone="America/Chicago"))
    assert [row[1] for row in rows[1:]] == ["2025-11-02 01:30:00", "2025-11-02 01:30:00"]
    assert [row[0] for row in rows[1:]] == [str(first), str(second)]


def test_generation_request_rejects_unknown_time_zone() -> None:
    with pytest.raises(ValidationError, match="Mars/Olympus"):
        DatasetGenerationRequest(**_RECIPE, time_zone="Mars/Olympus")


# ── Plan receipt ─────────────────────────────────────────────────


def test_plan_reports_the_time_column_it_would_add() -> None:
    with_zone = build_dataset_plan(DatasetPlanRequest(**_RECIPE, time_zone="America/Chicago"))
    without_zone = build_dataset_plan(DatasetPlanRequest(**_RECIPE))
    assert with_zone.time_column == "time_america_chicago"
    assert without_zone.time_column is None
    # The selectable projection is independent of the time column.
    assert with_zone.output_columns == without_zone.output_columns


@pytest.mark.asyncio
async def test_plan_unknown_time_zone_is_422() -> None:
    api = FastAPI()
    api.include_router(dataset_router.router, prefix="/api/dataset")
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/api/dataset/plan", json={**_RECIPE, "time_zone": "Mars/Olympus"})
    assert response.status_code == 422


# ── Early validation (before any Polygon fetch) ──────────────────


def test_prepare_generation_request_accepts_a_planned_selection() -> None:
    request = DatasetGenerationRequest(**_RECIPE, columns=["close", "rsi_length14"])
    assert prepare_generation_request(request).columns == ["close", "rsi_length14"]


def test_plan_lists_a_long_window_indicator_the_real_run_produces() -> None:
    """Regression: the planning frame was a fixed 300 rows, so SMA(400) —
    configurable up to 500 — produced no column there while a real, warmed-up
    run did. The picker then could not offer it and an explicit selection
    silently dropped it from dataset.csv."""
    long_sma = {**_RECIPE, "indicator_entries": [{"name": "sma", "params": {"length": 500}}]}
    plan = build_dataset_plan(DatasetPlanRequest(**long_sma))
    assert "sma_length500" in plan.output_columns

    request = DatasetGenerationRequest(**long_sma, columns=["close", "sma_length500"])
    assert prepare_generation_request(request).columns == ["close", "sma_length500"]


def test_prepare_generation_request_rejects_a_column_the_recipe_cannot_produce() -> None:
    request = DatasetGenerationRequest(**_RECIPE, columns=["close", "ema_20"])
    with pytest.raises(ValueError, match="ema_20"):
        prepare_generation_request(request)


@pytest.mark.asyncio
async def test_generate_csv_unknown_column_is_422_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fetch_must_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a bad column selection must be rejected before fetching bars")

    monkeypatch.setattr(dataset_router, "_fetch_and_process", _fetch_must_not_run)
    api = FastAPI()
    api.include_router(dataset_router.router, prefix="/api/dataset")
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post(
            "/api/dataset/generate-csv", json={**_RECIPE, "columns": ["close", "ema_20"]}
        )
    assert response.status_code == 422
    assert "ema_20" in response.json()["detail"]


@pytest.mark.asyncio
async def test_dataset_zip_job_rejects_unknown_column_before_queueing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Data Lab page generates through this job — a bad selection must
    fail the POST, never queue a run that fetches bars and then dies."""
    from fastapi import HTTPException

    from app.routers import jobs as jobs_router

    def _must_not_queue(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the job must not be queued")

    monkeypatch.setattr(jobs_router, "run_in_thread", _must_not_queue)
    request = jobs_router.DatasetZipJobRequest(
        job_id="job-1", dataset={**_RECIPE, "columns": ["close", "ema_20"]}
    )
    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.start_dataset_zip_job(request)
    assert exc_info.value.status_code == 400
    assert "ema_20" in str(exc_info.value.detail)


# ── ZIP bundle ───────────────────────────────────────────────────


def _bundle(request: DatasetGenerationRequest, df: pd.DataFrame) -> dict[str, list[list[str]]]:
    zip_bytes, _ = dataset_router._build_zip_with_events(request, df, _RSI_META, raw_count=len(df))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {name: _rows(zf.read(name)) for name in ("dataset.csv", "columns.csv", "metadata.csv")}


def test_zip_bundle_honors_column_selection_and_time_zone() -> None:
    request = DatasetGenerationRequest(
        **_RECIPE, columns=["rsi_length14", "close", "open"], time_zone="America/Chicago"
    )
    files = _bundle(request, _frame([_ms(2025, 3, 7, 14, 30)]))

    header = ["unix_ts", "time_america_chicago", "open", "close", "rsi_length14"]
    assert files["dataset.csv"][0] == header
    assert files["dataset.csv"][1][1] == "2025-03-07 08:30:00"
    # columns.csv describes exactly the columns dataset.csv carries.
    assert [row[0] for row in files["columns.csv"][1:]] == header
    metadata = {row[0]: row[1] for row in files["metadata.csv"][1:]}
    assert metadata["time_zone"] == "America/Chicago"
    assert metadata["column_selection"] == "custom"
    assert metadata["indicator_1_column"] == "rsi_length14"


def test_zip_bundle_deselected_indicator_leaves_metadata() -> None:
    request = DatasetGenerationRequest(**_RECIPE, columns=["close"])
    files = _bundle(request, _frame([_ms(2025, 3, 7, 14, 30)]))

    assert files["dataset.csv"][0] == ["unix_ts", "close"]
    metadata = {row[0]: row[1] for row in files["metadata.csv"][1:]}
    assert "indicator_1_column" not in metadata
    assert "time_zone" not in metadata


def test_zip_bundle_default_request_exports_every_projected_column() -> None:
    files = _bundle(DatasetGenerationRequest(**_RECIPE), _frame([_ms(2025, 3, 7, 14, 30)]))
    assert files["dataset.csv"][0] == ["unix_ts", *_PROJECTION]
    metadata = {row[0]: row[1] for row in files["metadata.csv"][1:]}
    assert metadata["column_selection"] == "all"


def test_zip_bundle_selected_column_missing_from_the_run_fails_loudly() -> None:
    """The plan projected vwap, but this run's bars carry none — the export
    must not silently ship a file without a column the owner asked for."""
    request = DatasetGenerationRequest(**_RECIPE, columns=["close", "vwap"])
    df = _frame([_ms(2025, 3, 7, 14, 30)]).drop(columns=["vwap"])
    with pytest.raises(ValueError, match="vwap"):
        _bundle(request, df)

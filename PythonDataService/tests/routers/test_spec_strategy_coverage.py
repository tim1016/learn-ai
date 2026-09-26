"""Strategy Spec refuses a window its data source does not fully cover (#2445).

Spec now reads the lake through Strategy Lab's materializer (#2446), while
retaining #2445's content-coverage refusals. Before those checks a missing
window reported success with untouched cash, indistinguishable from a
strategy that never fired.

A zip on disk is not yet a session: one holding no regular-hours bar is
missing to the regular-session reader, and one the reader cannot decode is
refused as unreadable, by path. A window can still be admitted and read
nothing — it holds no trading session at all (a weekend, a holiday). The run
then evaluated zero bars, and says so the way Strategy Lab does instead of
reporting the untouched starting cash as a result.

These tests drive the real default data-source factory (no dependency
override) against a temporary lake root, so the refusal is proven where
production meets it.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.data_lake import run_materialization
from app.data_lake.ensure_data import _compute_data_availability_hash
from app.data_lake.path_policy import lake_subpath
from app.data_lake.run_materialization import EngineRunMaterialization
from app.data_lake.types import ArtifactFailure, ArtifactRecord, DataAvailabilityResult, DataRunSpec
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine, BacktestResult
from app.engine.strategy.spec import StrategySpec
from app.lean_sidecar.trading_calendar import expected_sessions, is_early_close
from app.main import app
from app.routers.spec_strategy import _FIXTURES_DIR, get_data_source_factory
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services import engine_backtest_service
from tests._helpers.lean_store import record_fixture_adjustment, seed_pre_market_day, seed_store_day

# Thanksgiving fortnight: 2024-11-28 is a closure and 2024-11-29 closes at
# 13:00 ET, so the window carries both calendar cases the check must honour.
WINDOW = (date(2024, 11, 25), date(2024, 12, 6))
THANKSGIVING = date(2024, 11, 28)
BLACK_FRIDAY = date(2024, 11, 29)
# What Strategy Lab and Spec both say about a run that read nothing (#2445).
ZERO_BARS = "missing data: backtest evaluated zero bars for the requested window"


@pytest.fixture
def lean_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real materializer/gate/reader; the catalog's result is an offline fixture."""
    writer = tmp_path / "writer"
    root = writer / lake_subpath("polygon_split_adjusted")
    root.mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(writer))
    monkeypatch.delenv("LEAN_DATA_ROOT", raising=False)
    monkeypatch.delenv("LEAN_DATA_CACHE", raising=False)

    def materialize(spec: DataRunSpec, **_: Any) -> DataAvailabilityResult:
        return _lake_result(root, spec.symbols[0])

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", materialize)
    return root


def _lake_result(root: Path, symbol: str) -> DataAvailabilityResult:
    """Catalog receipts describe the exact seeded zip bytes, not invented hashes."""
    records: list[ArtifactRecord] = []
    for day in expected_sessions(*WINDOW):
        path = root / "equity" / "usa" / "minute" / symbol.lower() / f"{day:%Y%m%d}_trade.zip"
        if not path.is_file():
            continue
        payload = path.read_bytes()
        records.append(
            ArtifactRecord(
                id=len(records) + 1,
                artifact_kind="time_series_bars",
                market="usa",
                symbol=symbol,
                trading_date=day,
                resolution="minute",
                data_type="trade",
                provider="polygon",
                price_adjustment_mode="polygon_split_adjusted",
                data_contract_hash="c" * 64,
                file_path=str(path.relative_to(root)),
                file_sha256=hashlib.sha256(payload).hexdigest(),
                file_size_bytes=len(payload),
                row_count=None,
                first_bar_start_ms=None,
                last_bar_start_ms=None,
            )
        )
    return DataAvailabilityResult(
        request_id=UUID(int=1),
        overall_status="complete",
        lean_data_root_path=str(root),
        data_availability_hash=_compute_data_availability_hash(records),
        artifacts=records,
        reused_artifact_count=len(records),
        completed_at_ms=0,
        duration_ms=0,
    )


def _sma_spec(symbol: str = "SPY") -> dict[str, Any]:
    spec = StrategySpec.model_validate_json((_FIXTURES_DIR / "sma_crossover.spec.json").read_text(encoding="utf-8"))
    payload = spec.model_dump(mode="json")
    payload["symbols"] = [symbol]
    return payload


async def _post(spec: dict[str, Any], start: date, end: date) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/spec-strategy/backtest",
            json={"spec": spec, "start_date": start.isoformat(), "end_date": end.isoformat()},
        )


async def _post_backtest(spec: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    resp = await _post(spec, start, end)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed(root: Path, symbol: str, days: list[date]) -> None:
    for day in days:
        seed_store_day(root, symbol, day)


async def test_a_lake_held_non_spy_symbol_runs_without_legacy_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2446: Spec must consume the same lake-backed store as Strategy Lab."""
    writer = tmp_path / "writer"
    root = writer / lake_subpath("polygon_split_adjusted")
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(writer))
    monkeypatch.delenv("LEAN_DATA_ROOT", raising=False)
    monkeypatch.delenv("LEAN_DATA_CACHE", raising=False)
    _seed(root, "QQQ", expected_sessions(*WINDOW))
    calls: list[dict[str, Any]] = []

    def materialize(**kwargs: Any) -> EngineRunMaterialization:
        calls.append(kwargs)
        return EngineRunMaterialization("a" * 64, 0, len(expected_sessions(*WINDOW)), None)

    monkeypatch.setattr(run_materialization, "materialize_engine_run", materialize)

    response = await _post(_sma_spec("QQQ"), *WINDOW)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["total_trades"] > 0
    assert body["lake_data_availability_hash"] == "a" * 64
    assert len(calls) == 1
    assert calls[0]["symbol"] == "QQQ"
    assert calls[0]["start"] == WINDOW[0]
    assert calls[0]["end"] == WINDOW[1]
    assert calls[0]["resolution"] == "minute"
    assert calls[0]["price_adjustment_mode"] == "polygon_split_adjusted"


async def test_spec_backtest_on_a_symbol_with_no_data_fails_naming_the_gap(lean_root: Path) -> None:
    """The root holds SPY for the whole window; the spec asks for QQQ."""
    sessions = expected_sessions(*WINDOW)
    _seed(lean_root, "SPY", sessions)

    body = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert body["success"] is False, body
    assert body["total_trades"] == 0
    error = body["error"]
    assert "QQQ" in error
    assert f"{len(sessions)} of {len(sessions)}" in error
    assert "missing 2024-11-25..2024-12-06" in error


async def test_spec_backtest_on_a_partially_covered_window_fails_naming_each_gap(lean_root: Path) -> None:
    """Holes inside the window are named as session ranges; a closure is not a hole.

    Missing: 11-27 and the 11-29 half day (one run of sessions, since the
    Thanksgiving closure between them is no session), 12-04 and 12-06.
    """
    missing = {date(2024, 11, 27), BLACK_FRIDAY, date(2024, 12, 4), date(2024, 12, 6)}
    sessions = expected_sessions(*WINDOW)
    assert THANKSGIVING not in sessions
    assert BLACK_FRIDAY in sessions and is_early_close(BLACK_FRIDAY)
    _seed(lean_root, "SPY", [day for day in sessions if day not in missing])

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["total_trades"] == 0
    error = body["error"]
    assert "SPY" in error
    assert f"4 of {len(sessions)}" in error
    assert "missing 2024-11-27..2024-11-29, 2024-12-04, 2024-12-06" in error
    assert THANKSGIVING.isoformat() not in error


async def test_spec_backtest_on_a_fully_covered_window_is_unchanged(lean_root: Path) -> None:
    """Admission adds nothing to a covered run: same response as a bare reader over the same root."""
    _seed(lean_root, "SPY", expected_sessions(*WINDOW))

    admitted = await _post_backtest(_sma_spec(), *WINDOW)

    app.dependency_overrides[get_data_source_factory] = lambda: (
        lambda symbol, start, end: LeanMinuteDataReader([lean_root])
    )
    try:
        bare = await _post_backtest(_sma_spec(), *WINDOW)
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)

    assert admitted["success"] is True, admitted
    assert admitted["total_trades"] > 0, "the covered window must exercise a real run, not an empty one"
    assert admitted.pop("lake_data_availability_hash") == _lake_result(lean_root, "SPY").data_availability_hash
    assert bare.pop("lake_data_availability_hash") is None
    assert admitted.pop("log_lines")[1:] == bare.pop("log_lines")
    assert admitted == bare


@pytest.mark.parametrize("status", ["partial", "failed"])
async def test_lake_coverage_gate_refuses_before_the_engine_reads(
    lean_root: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # Even complete files cannot bypass a refusal from the shared lake gate.
    _seed(lean_root, "QQQ", expected_sessions(*WINDOW))
    result = _lake_result(lean_root, "QQQ").model_copy(
        update={
            "overall_status": status,
            "failures": [
                ArtifactFailure(
                    artifact_kind="time_series_bars",
                    symbol="QQQ",
                    trading_date=WINDOW[0],
                    data_type="trade",
                    reason="provider_no_data",
                )
            ],
        }
    )
    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", lambda *args, **kwargs: result)

    def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A refused window must not reach the engine")

    monkeypatch.setattr("app.routers.spec_strategy.BacktestEngine.run", must_not_run)
    body = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert body["success"] is False
    assert body["error"].startswith("Lake refused this run:")
    assert "QQQ" in body["error"] and "provider_no_data" in body["error"]
    assert body["lake_data_availability_hash"] is None


async def test_spec_and_strategy_lab_consume_identical_lake_bars_and_fingerprint(
    lean_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact equality of every consumed TradeBar, including Decimal OHLCV and UTC clocks."""
    _seed(lean_root, "QQQ", expected_sessions(*WINDOW))
    observed: list[BacktestResult] = []
    original_run = BacktestEngine.run

    def run_and_capture(self: BacktestEngine, *args: Any, **kwargs: Any) -> BacktestResult:
        result = original_run(self, *args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(BacktestEngine, "run", run_and_capture)
    monkeypatch.setattr(engine_backtest_service, "persist_engine_response_sync", lambda **kwargs: None)

    spec_result = await _post_backtest(_sma_spec("QQQ"), *WINDOW)
    lab_result = await asyncio.to_thread(
        engine_backtest_service.execute_engine_backtest,
        request=EngineBacktestRequest(
            strategy_name="sma_crossover",
            params={"symbol": "QQQ"},
            from_date=WINDOW[0].isoformat(),
            to_date=WINDOW[1].isoformat(),
            auto_fetch=True,
        ),
        on_phase=lambda _: None,
        on_log=lambda _: None,
    )

    assert spec_result["success"], spec_result["error"]
    assert lab_result.success, lab_result.error
    assert len(observed) == 2
    assert observed[0].bars and observed[0].bars == observed[1].bars
    assert {bar.symbol for bar in observed[0].bars} == {"QQQ"}
    expected_hash = _lake_result(lean_root, "QQQ").data_availability_hash
    assert spec_result["lake_data_availability_hash"] == lab_result.lake_data_availability_hash == expected_hash


async def test_a_cold_symbol_is_materialized_before_the_reader_opens_it(
    lean_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def capture(spec: DataRunSpec, **_: Any) -> DataAvailabilityResult:
        assert spec.symbols == ["QQQ"]
        _seed(lean_root, "QQQ", expected_sessions(*WINDOW))
        return _lake_result(lean_root, "QQQ")

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", capture)
    result = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert result["success"], result["error"]
    assert result["total_trades"] > 0
    assert result["lake_data_availability_hash"] == _lake_result(lean_root, "QQQ").data_availability_hash


async def test_harmless_lake_incompleteness_is_reported_beside_successful_results(
    lean_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(lean_root, "QQQ", expected_sessions(*WINDOW))
    receipt = _lake_result(lean_root, "QQQ").model_copy(
        update={
            "overall_status": "partial",
            "failures": [
                ArtifactFailure(
                    artifact_kind="metadata",
                    symbol=None,
                    trading_date=None,
                    data_type=None,
                    reason="io_error",
                )
            ],
        }
    )
    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", lambda *args, **kwargs: receipt)

    body = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert body["success"], body["error"]
    assert body["lake_data_availability_hash"] == receipt.data_availability_hash
    assert any("incomplete" in line and "metadata/io_error" in line for line in body["log_lines"])


async def test_stale_adjustment_receipts_still_refuse_spec_runs(lean_root: Path) -> None:
    _seed(lean_root, "QQQ", expected_sessions(*WINDOW))
    # Change a valid zip without refreshing its corporate-action companion.
    target = lean_root / "equity" / "usa" / "minute" / "qqq" / "20241203_trade.zip"
    target.write_bytes(target.read_bytes() + b"changed")

    body = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert body["success"] is False
    assert "stale or mixed corporate-action version" in body["error"]
    assert body["lake_data_availability_hash"] is None


@pytest.mark.parametrize(
    "window",
    [(date(2024, 11, 30), date(2024, 12, 1)), (THANKSGIVING, THANKSGIVING)],
    ids=["weekend", "holiday"],
)
async def test_spec_backtest_on_a_window_with_no_session_fails_instead_of_reporting_the_starting_cash(
    lean_root: Path, window: tuple[date, date]
) -> None:
    """No session is missing from a window that holds none, so admission passes; the run read nothing."""
    assert expected_sessions(*window) == []

    body = await _post_backtest(_sma_spec("ZZZZ"), *window)

    assert body["success"] is False, body
    assert body["error"] == ZERO_BARS
    assert body["final_equity"] == 0.0


async def test_spec_backtest_on_zips_with_no_regular_hours_bar_is_refused_as_missing(lean_root: Path) -> None:
    """Every session's zip is on disk, but the regular-session reader reads no bar from any of them."""
    sessions = expected_sessions(*WINDOW)
    for day in sessions:
        seed_pre_market_day(lean_root, "SPY", day)

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["error"] == (
        f"missing data: SPY has no minute bars for {len(sessions)} of {len(sessions)} trading sessions "
        "in 2024-11-25..2024-12-06 — missing 2024-11-25..2024-12-06"
    )


async def test_spec_backtest_with_one_session_holding_no_regular_hours_bar_fails_naming_it(lean_root: Path) -> None:
    """The other sessions produce bars, so the zero-bar backstop cannot catch the hole (#2475 review, Codex P1).

    Before the fix the zip's mere presence admitted the session, and the run
    completed on the eight sessions the reader did read.
    """
    sessions = expected_sessions(*WINDOW)
    hole = date(2024, 12, 3)
    _seed(lean_root, "SPY", [day for day in sessions if day != hole])
    seed_pre_market_day(lean_root, "SPY", hole)

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["error"] == (
        f"missing data: SPY has no minute bars for 1 of {len(sessions)} trading sessions "
        "in 2024-11-25..2024-12-06 — missing 2024-12-03"
    )


async def test_spec_backtest_on_a_zip_the_reader_cannot_decode_fails_naming_the_file(lean_root: Path) -> None:
    """A damaged zip is reported as unreadable, by path — not as a missing session a backfill would fill."""
    sessions = expected_sessions(*WINDOW)
    _seed(lean_root, "SPY", sessions)
    damaged = lean_root / "equity" / "usa" / "minute" / "spy" / "20241203_trade.zip"
    damaged.write_bytes(b"not a zip")
    # Keep the adjustment receipt current so this fixture reaches decoding.
    record_fixture_adjustment(damaged, "SPY")

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["error"] == (
        f"unreadable data: SPY minute data for 1 of {len(sessions)} trading sessions in 2024-11-25..2024-12-06 "
        f"is on disk but cannot be read — {damaged} (BadZipFile: File is not a zip file)"
    )


async def test_spec_backtest_with_an_end_before_its_start_is_a_bad_request(lean_root: Path) -> None:
    resp = await _post(_sma_spec(), WINDOW[1], WINDOW[0])

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "end_date must not precede start_date (got start=2024-12-06, end=2024-11-25)"

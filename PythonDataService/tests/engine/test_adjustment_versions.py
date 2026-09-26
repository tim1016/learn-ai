"""Corporate actions arriving between captures must not create a false gap (#2454)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.data_lake import ensure_data as pipeline
from app.data_lake.adjustment_versions import AdjustmentVersionError, CorporateActionSnapshot, companion_path
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent
from app.data_lake.polygon_fetcher import PolygonBar, PolygonFetchError
from app.data_lake.types import DataRunSpec, trading_date_to_calendar_anchor_ms
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from tests._helpers.fake_lake_catalog import install_fake_catalog

BEFORE = date(2024, 6, 7)
AFTER = date(2024, 6, 10)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Keep the real writer/derivation/readers; replace only DB and provider I/O."""
    install_fake_catalog(monkeypatch)
    state = SimpleNamespace(split=False, dividend=False, unavailable=False, change_during_fetch=False, fetches=[])

    async def fetch(*, symbol: str, start: date, adjusted: bool, **kwargs: object) -> list[PolygonBar]:
        state.fetches.append((start, adjusted))
        if state.unavailable and start == BEFORE:
            raise PolygonFetchError("provider cannot rebuild the old session")
        price = 10.0 if start >= AFTER or (adjusted and state.split) else 100.0
        if state.change_during_fetch:
            state.split = True
        return [PolygonBar(t_ms=session_open_ms_utc(start), open=price, high=price,
                           low=price, close=price, volume=100, vwap=price, n=1)]

    async def splits(**kwargs: object) -> list[SplitEvent]:
        return [SplitEvent("2024-06-10", 1, 10)] if state.split else []

    async def dividends(**kwargs: object) -> list[DividendEvent]:
        return [DividendEvent("2024-06-10", 1.0)] if state.dividend else []

    monkeypatch.setattr(pipeline, "ensure_lean_metadata_bundle", AsyncMock(return_value=(
        (None, False, "io_error", "isolated test"),
        (None, False, "io_error", "isolated test"),
        (None, False, "provider_no_data", None),
    )))
    monkeypatch.setattr(pipeline, "fetch_minute_trade_aggregates", fetch)
    monkeypatch.setattr(pipeline, "fetch_splits", splits)
    monkeypatch.setattr(pipeline, "fetch_dividends", dividends)
    return state


def spec(day: date, *, adjusted: bool = True) -> DataRunSpec:
    return DataRunSpec(
        request_id=uuid4(), run_type="python_lab", symbols=["NVDA"],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(day),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(day),
        lean_image_digest="sha256:test", include_factor_files=False, include_map_files=False,
        price_adjustment_mode="polygon_split_adjusted" if adjusted else "raw",
    )


@pytest.mark.asyncio
async def test_split_between_captures_does_not_create_a_false_price_gap(capture: SimpleNamespace) -> None:
    first = await pipeline.ensure_data(spec(BEFORE))
    root = Path(first.lean_data_root_path)
    assert LeanMinuteDataReader(root).read_day("NVDA", BEFORE)[0].close == 100
    capture.split = True

    latest = await pipeline.ensure_data(spec(AFTER))
    assert latest.corporate_action_versions != first.corporate_action_versions
    assert all(r.corporate_action_version == latest.corporate_action_versions["NVDA"] for r in latest.artifacts)

    minute = list(LeanMinuteDataReader(root).iter_bars("NVDA", BEFORE, AFTER))
    daily = list(LeanDailyDataReader(root).iter_bars("NVDA", BEFORE, AFTER))
    assert [bar.close for bar in minute] == [10, 10], "a split must rebuild older adjusted days"
    assert [bar.close for bar in daily] == [10, 10], "daily rollups must use one adjustment version"


@pytest.mark.asyncio
async def test_raw_history_and_receipts_do_not_change_when_actions_arrive(capture: SimpleNamespace) -> None:
    first = await pipeline.ensure_data(spec(BEFORE, adjusted=False))
    root = Path(first.lean_data_root_path)
    original = {r.file_path: (root / r.file_path).read_bytes() for r in first.artifacts}
    capture.split = True
    same = await pipeline.ensure_data(spec(BEFORE, adjusted=False))
    assert same.data_availability_hash == first.data_availability_hash
    assert same.corporate_action_versions == {}
    assert {r.file_path: (root / r.file_path).read_bytes() for r in same.artifacts} == original
    assert capture.fetches == [(BEFORE, False)]
    await pipeline.ensure_data(spec(AFTER, adjusted=False))
    assert [b.close for b in LeanMinuteDataReader(root).iter_bars("NVDA", BEFORE, AFTER)] == [100, 10]


@pytest.mark.asyncio
async def test_dividend_revision_rebuilds_even_when_split_adjusted_prices_are_unchanged(capture: SimpleNamespace) -> None:
    first = await pipeline.ensure_data(spec(BEFORE))
    capture.dividend = True
    second = await pipeline.ensure_data(spec(BEFORE))
    assert second.corporate_action_versions != first.corporate_action_versions
    assert second.data_availability_hash != first.data_availability_hash
    assert [r.file_sha256 for r in first.artifacts] == [r.file_sha256 for r in second.artifacts]
    assert [r.data_contract_hash for r in first.artifacts] != [r.data_contract_hash for r in second.artifacts]
    assert capture.fetches == [(BEFORE, True), (BEFORE, True)]
    again = await pipeline.ensure_data(spec(BEFORE))
    assert again.corporate_action_versions == second.corporate_action_versions
    assert len(capture.fetches) == 2


@pytest.mark.asyncio
async def test_failed_rebuild_invalidates_both_old_minute_and_daily_data(capture: SimpleNamespace) -> None:
    first = await pipeline.ensure_data(spec(BEFORE))
    root = Path(first.lean_data_root_path)
    capture.split = True
    capture.unavailable = True
    latest = await pipeline.ensure_data(spec(AFTER))
    assert any(f.reason == "corp_action_revision_mismatch" for f in latest.failures)
    assert not any(r.resolution == "daily" for r in latest.artifacts)
    for reader in (LeanMinuteDataReader(root), LeanDailyDataReader(root)):
        with pytest.raises(AdjustmentVersionError, match="corporate-action version"):
            list(reader.iter_bars("NVDA", BEFORE, AFTER))


@pytest.mark.asyncio
async def test_retry_rebuilds_previously_published_days_after_a_failed_refresh(capture: SimpleNamespace) -> None:
    first = await pipeline.ensure_data(spec(BEFORE))
    root = Path(first.lean_data_root_path)
    capture.split = True
    capture.unavailable = True
    await pipeline.ensure_data(spec(AFTER))
    capture.unavailable = False

    repaired = await pipeline.ensure_data(spec(AFTER))

    assert not any(f.reason == "corp_action_revision_mismatch" for f in repaired.failures)
    assert [b.close for b in LeanDailyDataReader(root).iter_bars("NVDA", BEFORE, AFTER)] == [10, 10]
    assert [b.close for b in LeanMinuteDataReader(root).iter_bars("NVDA", BEFORE, AFTER)] == [10, 10]


@pytest.mark.asyncio
async def test_reader_never_switches_version_mid_run(capture: SimpleNamespace) -> None:
    first = await pipeline.ensure_data(spec(BEFORE))
    reader = LeanMinuteDataReader(Path(first.lean_data_root_path))
    assert reader.read_day("NVDA", BEFORE)[0].close == 100
    capture.split = True
    await pipeline.ensure_data(spec(AFTER))
    with pytest.raises(AdjustmentVersionError, match="mixed corporate-action versions"):
        reader.read_day("NVDA", AFTER)


@pytest.mark.asyncio
async def test_actions_changing_during_capture_refuse_and_invalidate_that_capture(capture: SimpleNamespace) -> None:
    capture.change_during_fetch = True
    result = await pipeline.ensure_data(spec(BEFORE))
    assert result.overall_status == "failed"
    assert result.failures[0].reason == "corp_action_revision_mismatch"
    assert "changed during capture" in result.failures[0].detail
    with pytest.raises(AdjustmentVersionError, match="corporate-action version"):
        LeanMinuteDataReader(Path(result.lean_data_root_path)).read_day("NVDA", BEFORE)
    capture.change_during_fetch = False
    repaired = await pipeline.ensure_data(spec(BEFORE))
    assert LeanMinuteDataReader(Path(repaired.lean_data_root_path)).read_day("NVDA", BEFORE)[0].close == 10


@pytest.mark.parametrize("damage", ["absent", "wrong_hash"])
@pytest.mark.asyncio
async def test_unversioned_or_torn_cache_is_refused_then_rebuilt(capture: SimpleNamespace, damage: str) -> None:
    result = await pipeline.ensure_data(spec(BEFORE))
    root = Path(result.lean_data_root_path)
    for record in result.artifacts:
        path = companion_path(root / record.file_path)
        if damage == "absent":
            path.unlink()
        else:
            payload = json.loads(path.read_bytes())
            payload["file_sha256"] = "0" * 64
            path.write_text(json.dumps(payload))
    with pytest.raises(AdjustmentVersionError, match="corporate-action version"):
        LeanMinuteDataReader(root).read_day("NVDA", BEFORE)
    repaired = await pipeline.ensure_data(spec(BEFORE))
    assert repaired.corporate_action_versions == result.corporate_action_versions
    assert LeanMinuteDataReader(root).read_day("NVDA", BEFORE)[0].close == 100
    assert len(list(LeanDailyDataReader(root).iter_bars("NVDA", BEFORE, BEFORE))) == 1
    assert len(capture.fetches) == 2


@pytest.mark.asyncio
async def test_overlapping_captures_share_one_version_and_one_fetch(capture: SimpleNamespace) -> None:
    a, b = await asyncio.gather(pipeline.ensure_data(spec(BEFORE)), pipeline.ensure_data(spec(BEFORE)))
    assert a.corporate_action_versions == b.corporate_action_versions
    assert capture.fetches == [(BEFORE, True)]


@pytest.mark.asyncio
async def test_quote_cache_rebuilds_from_the_new_adjusted_source(capture: SimpleNamespace) -> None:
    request = spec(BEFORE).model_copy(update={"data_types": ["trade", "quote"]})
    first = await pipeline.ensure_data(request)
    capture.split = True
    second = await pipeline.ensure_data(request)
    quote_before = next(r for r in first.artifacts if r.data_type == "quote")
    quote_after = next(r for r in second.artifacts if r.data_type == "quote")
    assert quote_before.file_sha256 != quote_after.file_sha256
    record = json.loads(companion_path(Path(second.lean_data_root_path) / quote_after.file_path).read_bytes())
    assert record["corporate_action_version"] == second.corporate_action_versions["NVDA"]


@pytest.mark.asyncio
async def test_sweep_receipt_pins_versions_even_if_a_dividend_leaves_prices_identical(capture: SimpleNamespace) -> None:
    from app.research.sweep.snapshot import (
        DataSnapshot,
        DataSnapshotMismatchError,
        ManifestBoundMinuteReader,
        capture_data_snapshot,
    )

    result = await pipeline.ensure_data(spec(BEFORE))
    root = Path(result.lean_data_root_path)
    snapshot = capture_data_snapshot(roots=[root], symbol="NVDA", resolution="minute", data_start=BEFORE, data_end=BEFORE)
    assert snapshot.corporate_action_versions == result.corporate_action_versions
    assert DataSnapshot.from_dict(snapshot.as_dict()) == snapshot
    capture.dividend = True
    await pipeline.ensure_data(spec(BEFORE))
    reader = ManifestBoundMinuteReader(root, snapshot.artifacts)
    with pytest.raises(DataSnapshotMismatchError, match="changed since the snapshot"):
        reader.read_day("NVDA", BEFORE)


def test_snapshot_version_is_order_independent_and_changes_when_announced_action_becomes_effective() -> None:
    a = SplitEvent("2024-06-10", 1, 10)
    b = SplitEvent("2024-07-01", 1, 2)
    old = CorporateActionSnapshot((a, b), (), BEFORE)
    same = CorporateActionSnapshot((b, a), (), BEFORE)
    activated = CorporateActionSnapshot((a, b), (), AFTER)
    assert same.version == old.version
    assert activated.version != old.version
    assert b"2024-06-10" not in old.payload(), "persisted action dates must be epoch milliseconds"


@pytest.mark.parametrize("resolution", ["minute", "daily"])
@pytest.mark.asyncio
async def test_run_records_the_version_it_read_in_its_saved_receipt(
    capture: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, resolution: str,
) -> None:
    from app.schemas.engine_backtest import EngineBacktestRequest
    from app.services import engine_backtest_service as engine_service

    await pipeline.ensure_data(spec(BEFORE))
    capture.split = True
    latest = await pipeline.ensure_data(spec(AFTER))
    saved: list[dict] = []
    monkeypatch.setattr(engine_service._STRATEGY_REGISTRY["sma_crossover"], "supported_resolutions", {"minute", "daily"})

    def save(**kwargs: object) -> int:
        saved.append(kwargs)
        return 42

    monkeypatch.setattr(engine_service, "persist_engine_response_sync", save)
    monkeypatch.setattr(engine_service, "_dispatch_requested_parity_companion", lambda **kwargs: None)
    request = EngineBacktestRequest(
        strategy_name="sma_crossover", params={"symbol": "NVDA", "short_window": 2, "long_window": 3},
        from_date=BEFORE.isoformat(), to_date=AFTER.isoformat(), resolution=resolution,
        auto_fetch=False, save_study=True,
    )
    response = await asyncio.to_thread(
        engine_service.execute_engine_backtest, request=request, on_phase=lambda _: None, on_log=lambda _: None,
    )
    assert response.success, response.error
    assert response.corporate_action_versions == latest.corporate_action_versions
    assert saved[0]["execution_config"]["corporate_action_versions"] == latest.corporate_action_versions


@pytest.mark.asyncio
async def test_availability_does_not_reuse_a_verdict_after_version_invalidation(capture: SimpleNamespace) -> None:
    from app.engine.data.availability import check_availability

    result = await pipeline.ensure_data(spec(BEFORE))
    root = Path(result.lean_data_root_path)
    assert check_availability([root], "NVDA", BEFORE, BEFORE).is_complete
    capture.split = True
    capture.unavailable = True
    await pipeline.ensure_data(spec(AFTER))
    report = check_availability([root], "NVDA", BEFORE, BEFORE)
    assert not report.is_complete
    assert report.unreadable_days == [BEFORE]
    assert "corporate-action version" in report.unreadable_files[0].reason


@pytest.mark.asyncio
async def test_action_history_retains_the_receipted_snapshot(capture: SimpleNamespace) -> None:
    from app.data_lake.adjustment_versions import current_snapshot_path

    first = await pipeline.ensure_data(spec(BEFORE))
    capture.split = True
    latest = await pipeline.ensure_data(spec(AFTER))
    root = Path(latest.lean_data_root_path)
    for result in (first, latest):
        version = result.corporate_action_versions["NVDA"]
        history = root / current_snapshot_path("NVDA").parent / "nvda" / f"{version}.json"
        import hashlib
        assert hashlib.sha256(history.read_bytes()).hexdigest() == version

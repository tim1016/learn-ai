"""Receipts cross real acquisition threads and the provider/admission boundaries."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.data_lake.work_progress import observe_work
from app.options_backtest import service
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.timings import RunTimings
from app.utils.timestamps import now_ms_utc
from tests.options_backtest import test_golden_spy_2025_06_10 as golden
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


async def test_real_provider_and_admission_emit_receipts_across_thread_and_background_loop(
    fake_catalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    receipts = []
    given = golden.a_setup()
    hooks = service.RunHooks(lambda phase: None, lambda done, total: None, lambda message: None, lambda: False)
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH), observe_work(receipts.append):
        await asyncio.to_thread(service.prepare_run_data, given, (golden.TRADING_DATE,), hooks, now_ms_utc, timings=RunTimings())
        frozen = await asyncio.to_thread(freeze_run_inputs, given, (golden.TRADING_DATE,), LakeRunData("SPY"), tmp_path / "inputs")
    work = [event for event in receipts if event["kind"] == "work"]
    assert {event["work_kind"] for event in work} == {"provider_response", "admitted_artifact"}
    assert len({event["identity"] for event in work if event["work_kind"] == "provider_response"}) >= 3
    waits = [event["bound_ms"] for event in receipts if event["kind"] == "external_wait"]
    assert {700_000, 660_000, 1_860_000} <= set(waits)
    assert len(waits) == sum(event["kind"] == "external_wait_end" for event in receipts)
    assert frozen.retained_bytes > 0

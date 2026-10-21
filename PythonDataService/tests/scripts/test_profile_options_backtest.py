"""The frozen-replay profiler runs on a captured temporary lake and writes cumulative functions (#3417)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.options_backtest.lake_data import LakeRunData
from scripts.profile_options_backtest import profile_run
from tests.options_backtest import test_golden_spy_2025_06_10 as golden
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


async def test_profiler_uses_captured_temporary_lake_and_reports_cumulative_functions(
    fake_catalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH):
        await golden.capture_the_recording()
    output = tmp_path / "profile.txt"
    await asyncio.to_thread(profile_run, golden.a_setup(), (golden.TRADING_DATE,), LakeRunData("SPY"),
                            tmp_path / "inputs", output)
    report = output.read_text()
    assert "Frozen input inventory: " in report and "cumulative" in report

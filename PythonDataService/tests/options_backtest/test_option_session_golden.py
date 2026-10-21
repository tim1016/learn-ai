"""Unmodified vendor recordings prove both layouts, old pins, and retained proofs."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionMinuteBarPath
from app.options_backtest.data_check import SessionVerdict, check_data
from app.options_backtest.fetching import ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import run_sessions
from app.options_backtest.run_json import option_days_to_json, pins_from_json
from app.options_backtest.session_series import read_session_series
from app.options_bots.golden_proof import build_proof, reprove, result_sha256
from app.research.golden_search.proof import BlobStore
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


@pytest.mark.parametrize("recording,stop", [("2025_06_10", False), ("2026_10_09_sold_spread", False),
                                           ("2026_10_09_sold_spread", True)])
async def test_recorded_golden_results_ladders_pins_and_proofs_survive_repack(
    recording: str, stop: bool, fake_catalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    source = importlib.import_module(f"tests.options_backtest.test_golden_spy_{recording}")
    setup = source.a_setup() if recording == "2025_06_10" else source.a_call_credit_spread(stop_percent="10" if stop else None)
    with recorded_vendor(source.RECORDING, source.RECORDED_FILES, source.STOCK_PATH) as vendor:
        await source.capture_the_recording()
        before = len(vendor.calls)
        legacy = await asyncio.to_thread(run_sessions, setup, [source.TRADING_DATE], LakeRunData("SPY"))
        old_series = await asyncio.to_thread(read_session_series, legacy.sessions[0], run_id=1,
                                             underlying="SPY", data=LakeRunData("SPY"))
        store = BlobStore(tmp_path / "proof_blobs")
        old_proof = await asyncio.to_thread(build_proof, legacy, blobs=store)
        repacked = await repack_option_session("SPY", source.TRADING_DATE)
        assert repacked.failure is None
        # Remove legacy files to prove every replay and old byte pin uses retained
        # exact member bytes, without making the provider available again.
        for pin in legacy.sessions[0].pins.option_days:
            path = raw_root(tmp_lake) / OptionMinuteBarPath(pin.contract.root, pin.contract.occ_symbol,
                                                          source.TRADING_DATE).relative_path()
            path.unlink()
        day_file = await asyncio.to_thread(run_sessions, setup, [source.TRADING_DATE], LakeRunData("SPY"))
        assert result_sha256(day_file) == result_sha256(legacy)
        assert day_file.slippage == legacy.slippage
        assert day_file.summary.cash_ladder == legacy.summary.cash_ladder
        for old, new in zip(legacy.sessions, day_file.sessions, strict=True):
            assert old.outcome == new.outcome
            assert old.cash_after_usd == new.cash_after_usd
            assert all(pin.reference is not None for pin in new.pins.option_days)
            encoded = option_days_to_json(new.pins)
            assert pins_from_json(new.pins.underlying_sha256, new.pins.listing_sha256, encoded) == new.pins
        assert await asyncio.to_thread(read_session_series, legacy.sessions[0], run_id=1,
                                       underlying="SPY", data=LakeRunData("SPY")) == old_series
        new_proof = await asyncio.to_thread(build_proof, day_file, blobs=store)
        assert any("/session/" in path for path in new_proof["manifest"])
        await asyncio.to_thread(reprove, legacy, old_proof, blobs=store)
        await asyncio.to_thread(reprove, day_file, new_proof, blobs=store)
        future_now = 1_893_528_000_000
        check = await asyncio.to_thread(check_data, setup, [source.TRADING_DATE], now_ms=future_now, data=LakeCheckData("SPY"))
        assert check.sessions[0].verdict is SessionVerdict.READY
        await asyncio.to_thread(ensure_run_data, setup, [source.TRADING_DATE], now_ms=future_now, should_stop=lambda: False)
        assert len(vendor.calls) == before

"""A freeze retains one admitted session generation across storage transitions."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.data_lake.option_session import read_option_session
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionSessionPath
from app.options.contract import OptionContract
from app.options_backtest.frozen_inputs import DecodedOptionCache, FrozenRunData, freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import run_sessions
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    raw_root,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest.setup_fixtures import leg, setup


@pytest.mark.parametrize("transition", ["container_refresh", "legacy_to_container", "container_to_legacy", "stable_partial"])
@respx.mock
async def test_freeze_refuses_changed_session_sources_but_accepts_stable_partial_fallback(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, transition: str,
) -> None:
    call = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
    put = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)
    await store_listing([vendor_listing_row(call), vendor_listing_row(put)])
    await store_underlying_day(fake_catalog, tmp_lake,
        [stock_bar(FRIDAY_OPEN_MS + 4 * 60_000, "500", "500", "500", "500")])
    await store_option_day(call, [])
    if transition != "legacy_to_container":
        first = await repack_option_session("SPY", FRIDAY)
        assert first.failure is None
    await store_option_day(put, [])
    if transition == "container_to_legacy":
        assert (await repack_option_session("SPY", FRIDAY)).failure is None
    given = setup(legs=[leg(expiry={"kind": "same_day"}),
                        leg(leg_id="put", right="put", expiry={"kind": "same_day"})])
    reads: list[dict[str, str] | None] = []
    checkpoints = []

    class ChangingData(LakeRunData):
        def option_day(self, contract: OptionContract, trading_date: date) -> Any:
            found = super().option_day(contract, trading_date)
            assert found is not None
            reads.append(found.reference)
            if len(reads) == 1:
                if transition in ("container_refresh", "legacy_to_container"):
                    # A real publication adds the second contract under exactly
                    # the same admitted listing between the two required reads.
                    refreshed = asyncio.run(repack_option_session("SPY", FRIDAY))
                    assert refreshed.failure is None
                elif transition == "container_to_legacy":
                    (raw_root(tmp_lake) / OptionSessionPath("SPY", FRIDAY).relative_path()).unlink()
            return found

    if transition == "stable_partial":
        frozen = await asyncio.to_thread(freeze_run_inputs, given, (FRIDAY,), ChangingData("SPY"), tmp_path / "frozen")
        assert reads[0] is not None and reads[1] is None
        actual = await asyncio.to_thread(run_sessions, given, (FRIDAY,), FrozenRunData(frozen, DecodedOptionCache(1024**2)))
        assert actual == await asyncio.to_thread(run_sessions, given, (FRIDAY,), LakeRunData("SPY"))
        return

    with pytest.raises(ValueError, match="generation changed"):
        await asyncio.to_thread(freeze_run_inputs, given, (FRIDAY,), ChangingData("SPY"), tmp_path / "mixed",
                                on_checkpoint=checkpoints.append)
    assert checkpoints == []
    assert len(reads) >= 2
    if transition == "container_refresh":
        assert reads[0]["listing_sha256"] == reads[1]["listing_sha256"]
        assert reads[0]["container_sha256"] != reads[1]["container_sha256"]
    else:
        assert (reads[0] is None) != (reads[1] is None)
    # A fresh freeze under the resulting coherent layout remains usable.
    frozen = await asyncio.to_thread(freeze_run_inputs, given, (FRIDAY,), LakeRunData("SPY"), tmp_path / "coherent")
    actual = await asyncio.to_thread(run_sessions, given, (FRIDAY,), FrozenRunData(frozen, DecodedOptionCache(1024**2)))
    assert actual == await asyncio.to_thread(run_sessions, given, (FRIDAY,), LakeRunData("SPY"))
    current = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    references = [pin.reference for pin in actual.sessions[0].pins.option_days]
    if current is None:
        assert all(ref is None for ref in references)
    else:
        assert all(ref["container_sha256"] == current.sha256 for ref in references)

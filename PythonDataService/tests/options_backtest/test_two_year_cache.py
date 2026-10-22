"""A worker's two-year walk stays bounded through cold, warm and evicted session reads (#3429)."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath

import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_bars import option_minute_trade_dch
from app.data_lake.option_listing import build_option_listing_zip_bytes, listing_from_admitted_bytes, option_listing_dch
from app.data_lake.option_session import build_option_session_bytes, option_session_dch
from app.data_lake.path_policy import OptionListingPath, OptionMinuteBarPath, OptionSessionPath
from app.data_lake.polygon_option_contracts import PolygonOptionContract
from app.data_lake.types import ArtifactIdentity
from app.engine.data.decoded_cache import DecodedArtifactCache
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options.contract import OptionContract
from app.options_backtest.frozen_inputs import FrozenRunData, freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import run_sessions
from app.research.golden_search.proof import ProofMismatchError
from app.utils.timestamps import ny_datetime
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root, replace_the_stored_file, stock_bar, store_underlying_day
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest.setup_fixtures import leg, setup


async def _seed(fake_catalog: FakeCatalog, root: Path, relative: PurePosixPath, payload: bytes,
                identity: ArtifactIdentity, recipe: str, *, row_count: int = 2) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    claim = fake_catalog.claim_metadata_artifact if identity.artifact_kind == "metadata" else fake_catalog.claim_minute_bar
    artifact_id = await claim(identity, "test-cache-seed", 60_000, recipe, str(relative))
    assert artifact_id is not None
    assert await fake_catalog.complete_artifact(artifact_id, row_count, None, None, len(payload),
        hashlib.sha256(payload).hexdigest(), lease_generation=catalog_client.INITIAL_LEASE_GENERATION)


def _option_bytes(contract: OptionContract, day: date, *, exit_price: str = "1.10") -> bytes:
    bars = []
    for minute, price in enumerate(("1.00", exit_price)):
        value = Decimal(price)
        bars.append(MinuteTradeBar(ny_datetime(session_open_ms_utc(day) + minute * 60_000),
                                  value, value, value, value, Decimal(1)))
    return build_minute_trade_zip_bytes(contract.occ_symbol, f"{day:%Y%m%d}", bars)


@pytest.mark.parametrize("layout", ["legacy", "container"])
@respx.mock
async def test_two_year_replay_is_exact_with_bounded_eviction_and_warm_reuse(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, layout: str,
) -> None:
    sessions = tuple(expected_sessions(date(2024, 10, 21), date(2026, 10, 20)))
    root = raw_root(tmp_lake)
    contracts = {}
    for day in sessions:
        contract = contracts[day] = OptionContract.of(root="SPY", expiry_date=day, right="call", strike=500)
        rows = [PolygonOptionContract(f"O:{contract.occ_symbol}", "call", Decimal(500), day.isoformat(),
                                      "american", Decimal(100), False)]
        listing_bytes = build_option_listing_zip_bytes("SPY", day, rows)
        await _seed(fake_catalog, root, OptionListingPath("SPY", day).relative_path(), listing_bytes,
                    ArtifactIdentity(artifact_kind="metadata", market="usa_options", symbol="SPY", provider="polygon",
                                     price_adjustment_mode="raw"), option_listing_dch("SPY", day), row_count=1)
        await store_underlying_day(fake_catalog, tmp_lake,
            [stock_bar(session_open_ms_utc(day) + minute * 60_000, "500.20", "500.20", "500.20", "500.20")
             for minute in range(2)], day=day)
        identity = ArtifactIdentity(artifact_kind="time_series_bars", market="usa_options", symbol=contract.occ_symbol,
                                    trading_date=day, resolution="minute", data_type="trade", provider="polygon",
                                    price_adjustment_mode="raw")
        payload = _option_bytes(contract, day)
        if layout == "container":
            payload = build_option_session_bytes(listing_from_admitted_bytes("SPY", day, listing_bytes),
                                                 {contract.occ_symbol: payload})
            identity = identity.model_copy(update={"symbol": "SPY"})
            path, recipe = OptionSessionPath("SPY", day).relative_path(), option_session_dch()
        else:
            path = OptionMinuteBarPath("SPY", contract.occ_symbol, day).relative_path()
            recipe = option_minute_trade_dch()
        await _seed(fake_catalog, root, path, payload, identity, recipe)

    given = setup(legs=[leg(expiry={"kind": "same_day"})],
                  entry={"anchor": "open", "offset_ms": 60_000}, exit={"anchor": "open", "offset_ms": 120_000})
    frozen = await asyncio.to_thread(freeze_run_inputs, given, sessions, LakeRunData("SPY"), tmp_path / "frozen")
    uncached = DecodedArtifactCache(max_bytes=1)
    expected = run_sessions(given, sessions, FrozenRunData(frozen, uncached), slippage_ladder=(), cash_ladder=())
    assert uncached.metrics()["resident_entries"] == 0
    assert uncached.metrics()["uncached_decodes"] == len(sessions)
    sizing = DecodedArtifactCache(max_bytes=64 * 1024**2)
    FrozenRunData(frozen, sizing).listing(sessions[0])
    bound = 2 * sizing.metrics()["resident_bytes"]  # room for two sessions, so the 501 must evict
    cache = DecodedArtifactCache(max_bytes=bound)
    first = FrozenRunData(frozen, cache).listing(sessions[0])

    def check_residency(done: int, total: int, day: date) -> None:
        # A fresh run reader must reuse the same worker entry, rather than its own one-day slot.
        hits = cache.metrics()["hits"]
        assert FrozenRunData(frozen, cache).listing(day) is not None
        assert cache.metrics()["hits"] == hits + 1
        assert cache.take_peak_bytes() <= bound

    assert run_sessions(given, sessions, FrozenRunData(frozen, cache), on_session=check_residency,
                        slippage_ladder=(), cash_ladder=()) == expected
    assert cache.metrics()["evictions"] > 0
    misses = cache.metrics()["misses"]
    assert FrozenRunData(frozen, cache).listing(sessions[0]) == first
    assert cache.metrics()["misses"] == misses + 1

    day = sessions[-1]
    contract = contracts[day]
    payload = _option_bytes(contract, day, exit_price="1.50")
    if layout == "container":
        listing_bytes = (root / OptionListingPath("SPY", day).relative_path()).read_bytes()
        payload = build_option_session_bytes(listing_from_admitted_bytes("SPY", day, listing_bytes),
                                             {contract.occ_symbol: payload})
        path = OptionSessionPath("SPY", day).relative_path()
    else:
        path = OptionMinuteBarPath("SPY", contract.occ_symbol, day).relative_path()
    replace_the_stored_file(fake_catalog, root, str(path), payload, admit=True)
    changed = await asyncio.to_thread(freeze_run_inputs, given, (day,), LakeRunData("SPY"), tmp_path / "changed")
    assert changed.sessions[session_open_ms_utc(day)] != frozen.sessions[session_open_ms_utc(day)]
    misses = cache.metrics()["misses"]
    assert FrozenRunData(changed, cache).option_day(contract, day).value[-1].close == Decimal("1.50")
    assert cache.metrics()["misses"] == misses + 1
    digest = next(iter(changed.sessions[session_open_ms_utc(day)].snapshot.manifest.values()))
    (Path(changed.root) / digest[:2] / digest).write_bytes(b"changed bytes")
    with pytest.raises(ProofMismatchError):
        FrozenRunData(changed, cache).listing(day)

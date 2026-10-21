"""Retained bytes and explicit absences remain authoritative after lake repairs."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
import respx

from app.data_lake.option_listing import read_option_listing
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionListingPath, OptionMinuteBarPath, OptionSessionPath
from app.options.contract import OptionContract
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from tests.options_backtest._lake import FRIDAY, raw_root, store_listing, store_option_day, vendor_listing_row
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


def test_snapshot_absence_never_falls_through_and_uninventoried_read_refuses(tmp_path):
    day = date(2024, 1, 2)
    relative = str(OptionListingPath("SPY", day).relative_path())
    data = SnapshotRunData("SPY", Snapshot(manifest={}, absent=(relative,), retained_bytes=0), BlobStore(tmp_path))
    assert data.listing(day) is None
    assert relative in data.actual_reads
    with pytest.raises(ValueError, match="outside"):
        data.listing(date(2024, 1, 3))


def test_snapshot_byte_identity_refuses_corruption(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"retained original")
    snapshot = Snapshot(manifest={"original": digest}, absent=(), retained_bytes=17)
    data = SnapshotRunData("SPY", snapshot, store)
    assert data._read("original") == (b"retained original", digest)
    (tmp_path / digest[:2] / digest).write_bytes(b"corrupt")
    with pytest.raises(ProofMismatchError, match="corrupt"):
        data._read("original")


@pytest.mark.parametrize("container", [False, True])
@respx.mock
async def test_snapshot_actual_reads_identify_only_the_retained_format_used(fake_catalog, tmp_lake, tmp_path, container):
    contract = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
    await store_listing([vendor_listing_row(contract)])
    await store_option_day(contract, [])
    listing = await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)
    listing_path = str(OptionListingPath("SPY", FRIDAY).relative_path())
    legacy_path = str(OptionMinuteBarPath("SPY", contract.occ_symbol, FRIDAY).relative_path())
    if container:
        assert (await repack_option_session("SPY", FRIDAY)).failure is None
        option_path = str(OptionSessionPath("SPY", FRIDAY).relative_path())
        # Retained proof decoding must work after the source legacy file is gone.
        (raw_root(tmp_lake) / legacy_path).unlink()
    else:
        option_path = legacy_path
    store = BlobStore(tmp_path / "proof-blobs")
    payload = (raw_root(tmp_lake) / option_path).read_bytes()
    manifest = {option_path: store.put(payload)}
    if container:
        manifest[listing_path] = store.put(listing.source_bytes)
    data = SnapshotRunData("SPY", Snapshot(version=2 if container else 1, manifest=manifest,
        absent=(), retained_bytes=len(payload) + (len(listing.source_bytes) if container else 0)), store)
    captured = data.option_day(contract, FRIDAY)
    assert captured is not None and captured.value == ()
    assert data.actual_reads == ({option_path, listing_path} if container else {legacy_path})
    assert all(path in data.manifest for path in data.actual_reads)

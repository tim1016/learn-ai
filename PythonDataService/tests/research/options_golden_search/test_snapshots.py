"""Retained bytes and explicit absences remain authoritative after lake repairs."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date

import pytest
import respx

from app.data_lake.option_listing import read_option_listing
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionListingPath, OptionMinuteBarPath, OptionSessionPath
from app.options.contract import OptionContract
from app.options_backtest.lake_data import ArtifactBoundExceeded
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRecorder, SnapshotRunData
from tests.options_backtest._archived_data import HeldArtifacts
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


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_recorder_stores_each_unique_original_once_and_versions_a_named_container(tmp_path):
    shared, other, session = b"one original", b"another original", b"a session container"
    held = HeldArtifacts({"a": shared, "b": shared, "c": other, "session": session})
    admitted: list[int] = []
    recorder = SnapshotRecorder(held, BlobStore(tmp_path), max_bytes=1000, admit=lambda digest, size: admitted.append(size))
    for relative, payload in (("a", shared), ("b", shared), ("c", other), ("a", shared)):
        recorder.record(relative, _sha(payload))
    recorder.record("no-session", None, container=True)
    snapshot = recorder.snapshot()
    assert (snapshot.version, snapshot.absent) == (1, ("no-session",))
    assert snapshot.manifest == {"a": _sha(shared), "b": _sha(shared), "c": _sha(other)}
    assert (snapshot.retained_bytes, admitted) == (len(shared) + len(other), [len(shared), len(other)])
    recorder.record("session", _sha(session), container=True)
    assert recorder.snapshot().version == 2


@pytest.mark.parametrize(("files", "records"), [
    ({"a": b"edited"}, [("a", b"original")]),
    ({}, [("a", b"original")]),
    ({"a": b"original"}, [("a", None)]),
    ({"a": b"original"}, [("a", b"original"), ("a", b"another")]),
], ids=["edited", "vanished", "appeared", "rerecorded"])
def test_recorder_refuses_every_kind_of_changed_original(tmp_path, files, records):
    recorder = SnapshotRecorder(HeldArtifacts(files), BlobStore(tmp_path), max_bytes=1000)
    with pytest.raises(ValueError, match="generation changed"):
        for relative, payload in records:
            recorder.record(relative, None if payload is None else _sha(payload))


def test_recorder_refuses_past_its_bounds_before_storing_and_counts_a_repeated_blob_as_nothing(tmp_path):
    held = HeldArtifacts({"a": b"12345", "b": b"123456", "same": b"12345"})
    admitted: list[int] = []
    recorder = SnapshotRecorder(held, BlobStore(tmp_path), max_bytes=10, max_files=2,
                                admit=lambda digest, size: admitted.append(size))
    recorder.record("a", _sha(b"12345"))
    with pytest.raises(ArtifactBoundExceeded, match="byte bound"):
        recorder.record("b", _sha(b"123456"))
    assert admitted == [5] and not (tmp_path / _sha(b"123456")[:2] / _sha(b"123456")).exists()
    recorder.record("same", _sha(b"12345"))
    with pytest.raises(ArtifactBoundExceeded, match="file bound"):
        recorder.record("b", _sha(b"123456"))
    assert (recorder.snapshot().retained_bytes, admitted) == (5, [5])

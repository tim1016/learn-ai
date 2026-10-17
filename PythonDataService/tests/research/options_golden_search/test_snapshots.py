"""Retained bytes and explicit absences remain authoritative after lake repairs."""
from __future__ import annotations

from datetime import date

import pytest

from app.data_lake.path_policy import OptionListingPath
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData


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

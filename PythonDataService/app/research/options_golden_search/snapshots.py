"""Retained Options input inventory with explicit absence and no mutable-lake fallback."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from app.data_lake.admission import read_committed_bytes
from app.data_lake.option_lake import option_read_root
from app.data_lake.path_policy import LeanMinuteBarPath, OptionListingPath, OptionMinuteBarPath
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.contract import OptionContract
from app.options_backtest.data import Captured
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.picking import Listing, prepare_leg
from app.options_bots.golden_proof import ArchivedRunData
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search.protocol import FrozenModel, OptionsSearchProtocol
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import et_date_at_ms


class Snapshot(FrozenModel):
    version: int = 1
    manifest: dict[str, str]
    absent: tuple[str, ...]
    retained_bytes: int

    @property
    def snapshot_id(self) -> str:
        return hash_payload(self.model_dump(mode="json"))


class SnapshotRunData(ArchivedRunData):
    """Only recorded inventory is readable; recorded absences stay absent after lake repairs."""

    def __init__(self, underlying: str, snapshot: Snapshot, blobs: BlobStore) -> None:
        super().__init__(underlying, snapshot.manifest, blobs)
        if set(snapshot.manifest) & set(snapshot.absent):
            raise ValueError("snapshot cannot contain and omit the same artifact")
        self.absent = frozenset(snapshot.absent)
        self.actual_reads: set[str] = set()

    def _read(self, relative: str) -> tuple[bytes, str]:
        self.actual_reads.add(relative)
        return super()._read(relative)

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        relative = str(LeanMinuteBarPath("usa", self.underlying, trading_date, "trade").relative_path())
        self.actual_reads.add(relative)
        return None if relative in self.absent else super().underlying_day(trading_date)

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        relative = str(OptionListingPath(self.underlying, trading_date).relative_path())
        self.actual_reads.add(relative)
        return None if relative in self.absent else super().listing(trading_date)

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        relative = str(OptionMinuteBarPath(contract.root, contract.occ_symbol, trading_date).relative_path())
        self.actual_reads.add(relative)
        return None if relative in self.absent else super().option_day(contract, trading_date)


def capture_snapshot(protocol: OptionsSearchProtocol, blobs: BlobStore, *, lake_root: Path | None = None) -> Snapshot:
    """Retain an admitted superset of every contract reachable under the frozen selection rules.

    All underlying minute prices are considered for every saved contract-selection
    rule. This conservatively includes every possible entry and re-entry even when
    a searched rule enables re-entry absent from the original. It reads no P&L.
    """
    root = option_read_root(lake_root)
    source = LakeRunData(protocol.setup.underlying, lake_root=lake_root)
    manifest: dict[str, str] = {}
    absent: set[str] = set()
    retained = 0

    def record(relative: str, expected: str | None) -> None:
        nonlocal retained
        if relative in manifest or relative in absent:
            return
        if len(manifest) + len(absent) >= protocol.budget.max_snapshot_files:
            raise ValueError("snapshot file bound exceeded; reduce the study interval")
        if expected is None:
            absent.add(relative)
            return
        path = root / relative
        # Refuse before loading an oversized input into memory.
        if path.stat().st_size > protocol.budget.max_snapshot_bytes - retained:
            raise ValueError("snapshot byte bound exceeded; reduce the study interval")
        payload = read_committed_bytes(path)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("an admitted input changed during snapshot capture; retry planning")
        if retained + len(payload) > protocol.budget.max_snapshot_bytes:
            raise ValueError("snapshot byte bound exceeded")
        digest = blobs.put(payload)
        if digest != expected:
            raise ValueError("snapshot blob identity mismatch")
        manifest[relative] = digest
        retained += len(payload)

    sessions = sorted(set(protocol.development.session_ms) | set(protocol.final.session_ms))
    for session_open in sessions:
        day = et_date_at_ms(session_open)
        underlying = source.underlying_day(day)
        record(str(LeanMinuteBarPath("usa", protocol.setup.underlying, day, "trade").relative_path()), None if underlying is None else underlying.sha256)
        listing = source.listing(day)
        record(str(OptionListingPath(protocol.setup.underlying, day).relative_path()), None if listing is None else listing.sha256)
        if underlying is None or listing is None:
            continue
        prices = {bar.close for bar in underlying.value if session_open < bar.close_ms <= session_close_ms_utc(day)}
        contracts = set()
        for leg in protocol.setup.legs:
            prepared = prepare_leg(leg, listing=listing.value, trading_date=day)
            if isinstance(prepared, PickRefused):
                continue
            for price in prices:
                picked = prepared.pick(price)
                if isinstance(picked, Picked):
                    contracts.add(picked.contract)
        for contract in sorted(contracts, key=lambda c: c.occ_symbol):
            captured = source.option_day(contract, day)
            record(str(OptionMinuteBarPath(contract.root, contract.occ_symbol, day).relative_path()), None if captured is None else captured.sha256)
    return Snapshot(manifest=manifest, absent=tuple(sorted(absent)), retained_bytes=retained)

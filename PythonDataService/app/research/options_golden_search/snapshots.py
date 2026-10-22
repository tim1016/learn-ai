"""Retained Options input inventory with explicit absence and no mutable-lake fallback."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from app.data_lake.path_policy import LeanMinuteBarPath, OptionListingPath, OptionMinuteBarPath, OptionSessionPath
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.contract import OptionContract
from app.options_backtest.data import Captured
from app.options_backtest.lake_data import ArtifactBoundExceeded, LakeRunData
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.picking import Listing, prepare_leg
from app.options_bots.golden_proof import ArchivedRunData
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search.protocol import FrozenModel, OptionsSearchProtocol
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import et_date_at_ms

_GENERATION_CHANGED = "input artifact generation changed while it was recorded; retry with one generation"


class Snapshot(FrozenModel):
    version: Literal[1, 2] = 1
    manifest: dict[str, str]
    absent: tuple[str, ...]
    retained_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _inventory_and_absence_are_disjoint(self) -> Self:
        if set(self.manifest) & set(self.absent):
            raise ValueError("snapshot cannot contain and omit the same artifact")
        return self

    @property
    def snapshot_id(self) -> str:
        return hash_payload(self.model_dump(mode="json"))


class OriginalArtifacts(Protocol):
    def read_artifact(self, relative: str, *, max_bytes: int) -> bytes | None:
        """The exact original bytes of ``relative``, ``None`` when none exist; larger than ``max_bytes`` is refused."""
        ...


class SnapshotRecorder:
    """The one write side of a Snapshot: admitted originals into a BlobStore, with explicit absences (#3440).

    ``record`` re-reads the original (bounded before it is loaded), requires it to hash to what the caller admitted,
    and stores it under that hash; ``None`` records an absence only while the source still holds nothing there.
    Recording a path again under another hash is a generation change. ``max_bytes`` bounds the snapshot's unique
    blobs; ``admit`` sees each blob new to this snapshot before it is stored and may refuse it.
    """

    def __init__(self, source: OriginalArtifacts, blobs: BlobStore, *, max_bytes: int, max_files: int | None = None,
                 admit: Callable[[str, int], None] = lambda digest, size: None) -> None:
        self._source, self._blobs, self._max_bytes, self._max_files, self._admit = source, blobs, max_bytes, max_files, admit
        self._manifest: dict[str, str] = {}
        self._absent: set[str] = set()
        self._blobs_held: set[str] = set()
        self._retained = 0
        self._names_container = False

    def record(self, relative: str, expected: str | None, *, container: bool = False) -> None:
        if relative in self._manifest or relative in self._absent:
            if self._manifest.get(relative) != expected:
                raise ValueError(_GENERATION_CHANGED)
            return
        if self._max_files is not None and len(self._manifest) + len(self._absent) >= self._max_files:
            raise ArtifactBoundExceeded("snapshot file bound exceeded")
        known = expected in self._blobs_held
        payload = self._source.read_artifact(
            relative, max_bytes=self._max_bytes if known else self._max_bytes - self._retained)
        if expected is None:
            if payload is not None:
                raise ValueError(_GENERATION_CHANGED)
            self._absent.add(relative)
            return
        if payload is None or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(_GENERATION_CHANGED)
        if not known:
            self._admit(expected, len(payload))
            self._blobs.put(payload)
            self._blobs_held.add(expected)
            self._retained += len(payload)
        self._manifest[relative] = expected
        self._names_container = self._names_container or container

    def snapshot(self) -> Snapshot:
        # Version 2 marks a manifest that names a session container; older snapshots keep version 1 and their id.
        return Snapshot(version=2 if self._names_container else 1, manifest=dict(self._manifest),
                        absent=tuple(sorted(self._absent)), retained_bytes=self._retained)


class SnapshotRunData(ArchivedRunData):
    """Only recorded inventory is readable; recorded absences stay absent after lake repairs."""

    def __init__(self, underlying: str, snapshot: Snapshot, blobs: BlobStore) -> None:
        super().__init__(underlying, snapshot.manifest, blobs)
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

    def option_days(
        self, contracts: Sequence[OptionContract], trading_date: date,
    ) -> dict[str, Captured[Sequence[MinuteBar]] | None]:
        recorded = {contract.occ_symbol: str(OptionMinuteBarPath(contract.root, contract.occ_symbol, trading_date).relative_path())
                    for contract in contracts}
        self.actual_reads.update(relative for relative in recorded.values() if relative in self.absent)
        answered = super().option_days([c for c in contracts if recorded[c.occ_symbol] not in self.absent], trading_date)
        return {contract.occ_symbol: answered.get(contract.occ_symbol) for contract in contracts}


def capture_snapshot(protocol: OptionsSearchProtocol, blobs: BlobStore, *, lake_root: Path | None = None) -> Snapshot:
    """Retain an admitted superset of every contract reachable under the frozen selection rules.

    All underlying minute prices are considered for every saved contract-selection
    rule. This conservatively includes every possible entry and re-entry even when
    a searched rule enables re-entry absent from the original. It reads no P&L.
    """
    source = LakeRunData(protocol.setup.underlying, lake_root=lake_root)
    recorder = SnapshotRecorder(source, blobs, max_bytes=protocol.budget.max_snapshot_bytes,
                                max_files=protocol.budget.max_snapshot_files)

    def record(relative: str, expected: str | None, *, container: bool = False) -> None:
        try:
            recorder.record(relative, expected, container=container)
        except ArtifactBoundExceeded as exc:
            raise ValueError(f"{exc}; reduce the study interval") from exc

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
            if captured is not None and captured.reference is not None:
                record(str(OptionSessionPath(protocol.setup.underlying, day).relative_path()),
                       captured.reference.container_sha256, container=True)
            else:
                record(str(OptionMinuteBarPath(contract.root, contract.occ_symbol, day).relative_path()), None if captured is None else captured.sha256)
    return recorder.snapshot()

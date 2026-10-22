"""Retain admitted original artifacts for bounded local replay (#3440).

Setup discovery stays with the canonical run. A session keeps the existing
Snapshot manifest and explicit absences; workers use SnapshotRunData's original
ZIP readers. Temporary checkpoints from the former decoded codec are refused.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import active_root_id
from app.data_lake.path_policy import LeanMinuteBarPath, OptionListingPath, OptionMinuteBarPath, OptionSessionPath
from app.engine.data.decoded_cache import DecodedArtifactCache
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.data import Captured, RunData
from app.options_backtest.limits import MAX_RANGE_SESSIONS
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import Listing
from app.options_backtest.run import RunStopped, load_session_market, validate_run_sessions
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_backtest.timings import RunTimings
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search.protocol import point_hash
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import MAX_TIMESTAMP_MS

DECODER_VERSION = "options-original-artifacts/v2"
RECIPE = "admitted-options-originals/v2"
MAX_SESSION_BYTES = 128 * 1024**2
MAX_FROZEN_BYTES = 2 * 1024**3

# Deterministic conservative accounting for the existing decoded-cache bound.
_MINUTE_BAR_ESTIMATED_BYTES = 850
_LISTED_CONTRACT_ESTIMATED_BYTES = 1000
_SESSION_OVERHEAD_BYTES = 64


class OriginalRunData(RunData, Protocol):
    def read_artifact(self, relative: str, *, max_bytes: int) -> bytes | None: ...


class FrozenSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot: Snapshot
    contracts: tuple[str, ...]

    @property
    def digest(self) -> str:
        return hash_payload(self.model_dump(mode="json"))


class FrozenRunInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = DECODER_VERSION
    root: str
    underlying: str
    setup_digest: str = Field(pattern="^[a-f0-9]{64}$")
    source_root_id: str
    recipe: str = RECIPE
    sessions: dict[int, FrozenSession]
    retained_bytes: int = Field(ge=0, le=MAX_FROZEN_BYTES)

    @model_validator(mode="before")
    @classmethod
    def _compatible_generation(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and (value.get("version", DECODER_VERSION) != DECODER_VERSION
                or value.get("recipe", RECIPE) != RECIPE):
            raise ValueError("unsupported frozen input recipe or decoder; restart preparation")
        return value

    @model_validator(mode="after")
    def _known_decoder_and_bounded_inventory(self) -> Self:
        if self.version != DECODER_VERSION or self.recipe != RECIPE:
            raise ValueError("unsupported frozen input recipe or decoder; restart preparation")
        if len(self.sessions) > MAX_RANGE_SESSIONS:
            raise ValueError("frozen session inventory exceeds the range bound")
        if list(self.sessions) != sorted(self.sessions):
            raise ValueError("frozen sessions must be ordered")
        if any(type(anchor) is not int or not 0 <= anchor <= MAX_TIMESTAMP_MS for anchor in self.sessions):
            raise ValueError("frozen session anchors must be bounded int64 UTC milliseconds")
        for session in self.sessions.values():
            if not 0 <= session.snapshot.retained_bytes <= MAX_SESSION_BYTES:
                raise ValueError("one frozen session exceeds the original input byte bound")
            if session.snapshot.version not in (1, 2):
                raise ValueError("unsupported retained original snapshot version")
            if set(session.snapshot.manifest) & set(session.snapshot.absent):
                raise ValueError("snapshot cannot contain and omit the same artifact")
        return self

    @property
    def blob_digests(self) -> frozenset[str]:
        """The complete deduplicated inventory used by staging, resume and verification."""
        return frozenset(digest for session in self.sessions.values() for digest in session.snapshot.manifest.values())

    def verify_blobs(self) -> dict[str, int]:
        sizes = _verified_sizes(BlobStore(Path(self.root)), self.blob_digests)
        if sum(sizes.values()) != self.retained_bytes:
            raise ValueError("frozen preparation checkpoint byte count differs")
        for session in self.sessions.values():
            if sum(sizes[digest] for digest in set(session.snapshot.manifest.values())) != session.snapshot.retained_bytes:
                raise ValueError("frozen session checkpoint byte count differs")
        return sizes

    @property
    def digest(self) -> str:
        return hash_payload(self.model_dump(mode="json", exclude={"root"}))


def _verified_sizes(blobs: BlobStore, digests: Sequence[str] | frozenset[str]) -> dict[str, int]:
    sizes = {}
    for digest in digests:
        path = blobs.root / digest[:2] / digest
        if path.is_file() and path.stat().st_size > MAX_SESSION_BYTES:
            raise ValueError("original artifact exceeds the frozen input byte bound")
        payload = blobs.get(digest)
        if len(payload) > MAX_SESSION_BYTES:
            raise ValueError("original artifact exceeds the frozen input byte bound")
        sizes[digest] = len(payload)
    return sizes


class _RecordingData:
    def __init__(self, source: OriginalRunData, underlying: str) -> None:
        self.source, self.underlying = source, underlying
        self.artifacts: dict[str, str | None] = {}
        self.stock: Captured[Sequence[MinuteBar]] | None = None
        self.listed: Captured[Listing] | None = None
        self.options: dict[str, Captured[Sequence[MinuteBar]] | None] = {}
        self.container_recorded = False
        self._session_checked = False

    def record(self, relative: str, expected: str | None) -> None:
        if relative in self.artifacts and self.artifacts[relative] != expected:
            raise ValueError("input artifact generation changed during input freeze")
        self.artifacts[relative] = expected

    def underlying_day(self, day: date) -> Captured[Sequence[MinuteBar]] | None:
        found = self.source.underlying_day(day)
        self.stock = found
        self.record(str(LeanMinuteBarPath("usa", self.underlying, day, "trade").relative_path()), None if found is None else found.sha256)
        return found

    def listing(self, day: date) -> Captured[Listing] | None:
        self.listed = self.source.listing(day)
        self.record(str(OptionListingPath(self.underlying, day).relative_path()), None if self.listed is None else self.listed.sha256)
        return self.listed

    def option_day(self, contract: OptionContract, day: date) -> Captured[Sequence[MinuteBar]] | None:
        if not self._session_checked:
            relative = str(OptionSessionPath(self.underlying, day).relative_path())
            payload = self.source.read_artifact(relative, max_bytes=MAX_SESSION_BYTES)
            self.record(relative, None if payload is None else hashlib.sha256(payload).hexdigest())
            self.container_recorded = payload is not None
            self._session_checked = True
        found = self.source.option_day(contract, day)
        self.options[contract.occ_symbol] = found
        if found is not None and found.reference is not None:
            self.record(str(OptionSessionPath(self.underlying, day).relative_path()), found.reference.container_sha256)
        else:
            self.record(str(OptionMinuteBarPath(contract.root, contract.occ_symbol, day).relative_path()), None if found is None else found.sha256)
        return found


def _verify_session_generation(data: RunData, day: date, recording: _RecordingData) -> None:
    if recording.listed is None:
        return
    original = recording.listed.sha256
    generations = set()
    representative = None
    for symbol, found in recording.options.items():
        if found is not None and found.reference is not None:
            reference = found.reference.to_json()
            if reference["listing_sha256"] != original:
                raise ValueError("a day-file listing generation changed during input freeze")
            generations.add(tuple(reference[key] for key in
                ("version", "layout", "data_root_id", "recipe", "listing_sha256", "container_sha256")))
            representative = symbol
    if len(generations) > 1:
        raise ValueError("option container generation changed during input freeze; retry with one generation")
    for symbol, found in recording.options.items():
        if found is not None and found.reference is not None and symbol != representative:
            continue
        current = data.option_day(OptionContract.from_occ_symbol(symbol), day)
        if (current is None) != (found is None) or (current is not None and found is not None
                and (current.sha256, current.reference) != (found.sha256, found.reference)):
            raise ValueError("option source generation changed during input freeze; retry with one generation")
    current = data.listing(day)
    if current is None or current.sha256 != original:
        raise ValueError("listing generation changed during input freeze; retry with one generation")


class _SessionRetention:
    def __init__(self, data: OriginalRunData, blobs: BlobStore, sizes: dict[str, int],
                 reserve_bytes: Callable[[int], None]) -> None:
        self.data, self.blobs, self.sizes, self.reserve_bytes = data, blobs, sizes, reserve_bytes
        self.manifest: dict[str, str] = {}
        self.absent: set[str] = set()
        self.session_sizes: dict[str, int] = {}

    def record(self, relative: str, expected: str | None) -> None:
        if relative in self.manifest or relative in self.absent:
            if self.manifest.get(relative) != expected:
                raise ValueError("input artifact generation changed during input freeze")
            return
        if expected is None:
            if self.data.read_artifact(relative, max_bytes=MAX_SESSION_BYTES) is not None:
                raise ValueError("input artifact generation changed during input freeze")
            self.absent.add(relative)
            return
        remaining = min(MAX_SESSION_BYTES - sum(self.session_sizes.values()),
                        MAX_FROZEN_BYTES - sum(self.sizes.values()))
        # A duplicate blob still needs admission but consumes no new space.
        limit = MAX_SESSION_BYTES if expected in self.sizes else remaining
        payload = self.data.read_artifact(relative, max_bytes=limit)
        if payload is None or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("input artifact generation changed during input freeze; retry with one generation")
        session_size = sum(self.session_sizes.values()) + (0 if expected in self.session_sizes else len(payload))
        retained = sum(self.sizes.values()) + (0 if expected in self.sizes else len(payload))
        if session_size > MAX_SESSION_BYTES:
            raise ValueError("one frozen session exceeds the original input byte bound")
        if retained > MAX_FROZEN_BYTES:
            raise ValueError("frozen run exceeds its retained original input byte bound")
        self.reserve_bytes(retained)
        if self.blobs.put(payload) != expected:
            raise ValueError("frozen original blob identity mismatch")
        self.sizes[expected] = self.session_sizes[expected] = len(payload)
        self.manifest[relative] = expected

def freeze_run_inputs(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    data: OriginalRunData,
    root: Path,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_session: Callable[[int, int, date], None] = lambda done, total, day: None,
    reserve_bytes: Callable[[int], None] = lambda retained: None,
    resume: FrozenRunInputs | None = None,
    on_checkpoint: Callable[[FrozenRunInputs], None] = lambda frozen: None,
) -> FrozenRunInputs:
    """Retain exact admitted originals, refusing newly oversized sessions/runs.

    The 128 MiB session and 2 GiB run bounds count unique original blob bytes.
    Whole containers count even when discovery needs only one of their members.
    """
    validate_run_sessions(setup, sessions)
    blobs = BlobStore(root)
    inventory: dict[int, FrozenSession] = {}
    sizes: dict[str, int] = {}
    setup_digest = point_hash(setup)
    if resume is not None:
        anchors = [session_window_for_date(day).open_ms_utc for day in sessions]
        if (resume.root != str(root.resolve()) or resume.setup_digest != setup_digest
                or resume.source_root_id != str(active_root_id()) or resume.underlying != setup.underlying
                or list(resume.sessions) != anchors[:len(resume.sessions)]):
            raise ValueError("frozen preparation checkpoint belongs to another input identity")
        sizes = resume.verify_blobs()
        inventory.update(resume.sessions)

    def checkpoint() -> FrozenRunInputs:
        return FrozenRunInputs(root=str(root.resolve()), underlying=setup.underlying,
            source_root_id=str(active_root_id()), setup_digest=setup_digest,
            sessions=dict(inventory), retained_bytes=sum(sizes.values()))

    for done, day in enumerate(sessions):
        if should_stop():
            raise RunStopped(done)
        anchor = session_window_for_date(day).open_ms_utc
        if anchor in inventory:
            on_session(done + 1, len(sessions), day)
            continue
        retention = _SessionRetention(data, blobs, sizes, reserve_bytes)
        recording = _RecordingData(data, setup.underlying)
        load_session_market(setup, day, recording)
        _verify_session_generation(data, day, recording)
        for relative, expected in recording.artifacts.items():
            retention.record(relative, expected)
        session = FrozenSession(snapshot=Snapshot(version=2 if recording.container_recorded else 1,
            manifest=retention.manifest, absent=tuple(sorted(retention.absent)),
            retained_bytes=sum(retention.session_sizes.values())),
            contracts=tuple(recording.options))
        retained = _decode(session, setup.underlying, day, blobs)
        if (retained.get("underlying") != recording.stock or retained.get("listing") != recording.listed
                or retained["options"] != recording.options):
            raise ValueError("retained originals differ from the admitted setup answers")
        inventory[anchor] = session
        on_checkpoint(checkpoint())
        on_session(done + 1, len(sessions), day)
    return checkpoint()


def _decode(session: FrozenSession, underlying: str, day: date, blobs: BlobStore) -> dict[str, Any]:
    # The whole container decode is scoped to this call. Only selected replay
    # values survive in the worker's accounted decoded cache (#3440).
    data = SnapshotRunData(underlying, session.snapshot, blobs)
    rows: dict[str, Any] = {"options": {}}
    if session.snapshot.manifest or session.snapshot.absent:
        rows["underlying"] = data.underlying_day(day)
        rows["listing"] = data.listing(day)
        rows["options"] = {symbol: data.option_day(OptionContract.from_occ_symbol(symbol), day)
                           for symbol in session.contracts}
    return rows


def _estimated_bytes(session: Mapping[str, Any]) -> int:
    bars = sum(len(found.value) for found in session["options"].values() if found is not None)
    if session.get("underlying") is not None:
        bars += len(session["underlying"].value)
    contracts = len(session["listing"].value.contracts) if session.get("listing") is not None else 0
    return _SESSION_OVERHEAD_BYTES + bars * _MINUTE_BAR_ESTIMATED_BYTES + contracts * _LISTED_CONTRACT_ESTIMATED_BYTES


@dataclass(frozen=True)
class FrozenSessionKey:
    """One admitted inventory's decode, reusable across temporary job directories."""
    content_digest: str
    source_root_id: str
    underlying: str
    relative_path: str
    session_open_ms: int
    decoder_version: str = DECODER_VERSION


class FrozenRunData:
    def __init__(self, frozen: FrozenRunInputs, cache: DecodedArtifactCache, *,
                 timings: RunTimings | None = None, chain_key: str | None = None) -> None:
        self.frozen, self.cache = frozen, cache
        self.timings, self.chain_key = timings, chain_key
        self._last: tuple[date, dict[str, Any]] | None = None

    def _read(self, session: FrozenSession, day: date) -> dict[str, Any]:
        blobs = BlobStore(Path(self.frozen.root))
        sizes = _verified_sizes(blobs, frozenset(session.snapshot.manifest.values()))
        if sum(sizes.values()) != session.snapshot.retained_bytes:
            raise ValueError("frozen session checkpoint byte count differs")
        digest = session.digest
        anchor = session_window_for_date(day).open_ms_utc
        key = FrozenSessionKey(digest, self.frozen.source_root_id, self.frozen.underlying,
                               f"sessions/{anchor}/{digest}", anchor)
        return self.cache.get_or_decode_value(key, lambda: _decode(session, self.frozen.underlying, day, blobs),
                                              estimate=_estimated_bytes)

    def _day(self, day: date) -> dict[str, Any]:
        if self._last is None or self._last[0] != day:
            anchor = session_window_for_date(day).open_ms_utc
            if anchor not in self.frozen.sessions:
                raise ValueError("session is outside the frozen run input inventory")
            if self.timings is None:
                self._last = (day, self._read(self.frozen.sessions[anchor], day))
            else:
                with self.timings.measure("admit_decode", chain_key=self.chain_key):
                    self._last = (day, self._read(self.frozen.sessions[anchor], day))
        return self._last[1]

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        return self._day(trading_date).get("underlying")

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        return self._day(trading_date).get("listing")

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        return self._day(trading_date)["options"].get(contract.occ_symbol)

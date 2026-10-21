"""Freeze admitted replay inputs; workers read only digest-bound local blobs (#3423).

The freeze records the canonical RunData answers, including original byte pins
and optional day/member references. It therefore supports either lake layout
without teaching workers a second lake parser or giving them a catalog client.
One bounded session is decoded at a time. All times on disk are ms UTC.
"""
from __future__ import annotations

import json
import sys
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import active_root_id
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.data import Captured, RunData
from app.options_backtest.limits import MAX_RANGE_SESSIONS
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import ListedContract, Listing
from app.options_backtest.run import RunStopped, load_session_market, validate_run_sessions
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_backtest.timings import RunTimings
from app.research.golden_search.proof import BlobStore
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

DECODER_VERSION = "options-run-data/v1"
MAX_SESSION_BYTES = 128 * 1024**2
MAX_FROZEN_BYTES = 2 * 1024**3


class FrozenRunInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = DECODER_VERSION
    root: str
    underlying: str
    setup_digest: str = Field(pattern="^[a-f0-9]{64}$")
    source_root_id: str
    recipe: str = "admitted-options-run/v1"
    sessions: dict[int, str]
    retained_bytes: int = Field(ge=0, le=MAX_FROZEN_BYTES)

    @model_validator(mode="after")
    def _known_decoder_and_bounded_inventory(self) -> Self:
        if self.version != DECODER_VERSION or self.recipe != "admitted-options-run/v1":
            raise ValueError("unsupported frozen input recipe or decoder")
        if len(self.sessions) > MAX_RANGE_SESSIONS:
            raise ValueError("frozen session inventory exceeds the range bound")
        if list(self.sessions) != sorted(self.sessions):
            raise ValueError("frozen sessions must be ordered")
        if any(type(anchor) is not int or not 0 <= anchor <= MAX_TIMESTAMP_MS for anchor in self.sessions):
            raise ValueError("frozen session anchors must be bounded int64 UTC milliseconds")
        return self

    @property
    def digest(self) -> str:
        return hash_payload(self.model_dump(mode="json", exclude={"root"}))


def _bar_wire(bar: MinuteBar) -> list[int | str]:
    return [bar.close_ms, str(bar.open), str(bar.high), str(bar.low), str(bar.close)]


def _captured_wire(found: Captured[Any], value: Any) -> dict[str, Any]:
    return {"sha256": found.sha256, "reference": getattr(found, "reference", None), "value": value}


class _RecordingData:
    def __init__(self, source: RunData, day: date) -> None:
        self.source, self.day = source, day
        self.rows: dict[str, Any] = {}

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        found = self.source.underlying_day(trading_date)
        if found is not None:
            self.rows["underlying"] = _captured_wire(found, [_bar_wire(bar) for bar in found.value])
        return found

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        found = self.source.listing(trading_date)
        if found is not None:
            listed = found.value
            value = {"underlying": listed.underlying, "session_open_ms": session_window_for_date(listed.as_of).open_ms_utc,
                     "complete": listed.complete,
                     "contracts": [{"contract": row.contract.model_dump(mode="json"),
                                    "multiplier": row.multiplier, "standard": row.standard} for row in listed.contracts]}
            self.rows["listing"] = _captured_wire(found, value)
        return found

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        found = self.source.option_day(contract, trading_date)
        if found is not None:
            self.rows.setdefault("options", {})[contract.occ_symbol] = _captured_wire(found, [_bar_wire(bar) for bar in found.value])
        return found


def freeze_run_inputs(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    data: RunData,
    root: Path,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_session: Callable[[int, int, date], None] = lambda done, total, day: None,
    reserve_bytes: Callable[[int], None] = lambda retained: None,
    resume: FrozenRunInputs | None = None,
    on_checkpoint: Callable[[FrozenRunInputs], None] = lambda frozen: None,
) -> FrozenRunInputs:
    """Admit, validate and retain each required answer once before dispatch.

    Data acquisition stays with the API. A missing required answer or invalid
    session fails the entire freeze, so no worker can silently download it.
    """
    validate_run_sessions(setup, sessions)
    blobs = BlobStore(root)
    inventory: dict[int, str] = {}
    retained = 0
    setup_digest = hash_payload(setup.model_dump(mode="json"))
    if resume is not None:
        anchors = [session_window_for_date(day).open_ms_utc for day in sessions]
        if (resume.root != str(root.resolve()) or resume.setup_digest != setup_digest
                or resume.source_root_id != str(active_root_id()) or resume.underlying != setup.underlying
                or list(resume.sessions) != anchors[:len(resume.sessions)]):
            raise ValueError("frozen preparation checkpoint belongs to another input identity")
        for anchor, digest in resume.sessions.items():
            payload = blobs.get(digest)
            _decode(payload)
            retained += len(payload)
            inventory[anchor] = digest
        if retained != resume.retained_bytes:
            raise ValueError("frozen preparation checkpoint byte count differs")

    def checkpoint() -> FrozenRunInputs:
        return FrozenRunInputs(root=str(root.resolve()), underlying=setup.underlying,
            source_root_id=str(active_root_id()), setup_digest=setup_digest,
            sessions=dict(inventory), retained_bytes=retained)

    for done, day in enumerate(sessions):
        if should_stop():
            raise RunStopped(done)
        if session_window_for_date(day).open_ms_utc in inventory:
            on_session(done + 1, len(sessions), day)
            continue
        recording = _RecordingData(data, day)
        load_session_market(setup, day, recording)
        if "listing" in recording.rows:
            original = recording.rows["listing"]["sha256"]
            for row in recording.rows.get("options", {}).values():
                reference = row.get("reference") or {}
                if reference.get("listing_sha256", original) != original:
                    raise ValueError("a day-file listing generation changed during input freeze")
            current = data.listing(day)
            if current is None or current.sha256 != original:
                raise ValueError("listing changed during input freeze; retry with one generation")
        raw = json.dumps(recording.rows, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_SESSION_BYTES:
            raise ValueError("one frozen session exceeds the decoded input bound")
        payload = zlib.compress(raw)
        retained += len(payload)
        if retained > MAX_FROZEN_BYTES:
            raise ValueError("frozen run exceeds its retained input byte bound")
        reserve_bytes(retained)
        inventory[session_window_for_date(day).open_ms_utc] = blobs.put(payload)
        on_checkpoint(checkpoint())
        on_session(done + 1, len(sessions), day)
    return checkpoint()


def _size(value: Any, seen: set[int] | None = None) -> int:
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if isinstance(value, Mapping):
        total += sum(_size(key, seen) + _size(item, seen) for key, item in value.items())
    elif isinstance(value, (tuple, list)):
        total += sum(_size(item, seen) for item in value)
    elif hasattr(value, "__dict__"):
        total += _size(vars(value), seen)
    return total


def _decode_bars(row: Mapping[str, Any]) -> Captured[tuple[MinuteBar, ...]]:
    from decimal import Decimal

    bars = tuple(MinuteBar(int(values[0]), *(Decimal(value) for value in values[1:])) for values in row["value"])
    if row.get("reference") is None:
        return Captured(bars, str(row["sha256"]))
    return Captured(bars, str(row["sha256"]), reference=row["reference"])


def _decode(payload: bytes) -> dict[str, Any]:
    decoder = zlib.decompressobj()
    raw = decoder.decompress(payload, MAX_SESSION_BYTES + 1)
    if len(raw) > MAX_SESSION_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("frozen session exceeds its decoded bound or has trailing/truncated bytes")
    rows = json.loads(raw)
    result: dict[str, Any] = {"options": {symbol: _decode_bars(row) for symbol, row in rows.get("options", {}).items()}}
    if "underlying" in rows:
        result["underlying"] = _decode_bars(rows["underlying"])
    if "listing" in rows:
        captured, value = rows["listing"], rows["listing"]["value"]
        anchor = value["session_open_ms"]
        if isinstance(anchor, bool) or not isinstance(anchor, int) or not 0 <= anchor <= MAX_TIMESTAMP_MS:
            raise ValueError("invalid frozen listing session anchor")
        listing = Listing(value["underlying"], et_date_at_ms(anchor),
                          tuple(ListedContract(OptionContract.model_validate(row["contract"]), row["multiplier"], row["standard"])
                                for row in value["contracts"]), value["complete"])
        result["listing"] = Captured(listing, captured["sha256"])
    return result


class DecodedOptionCache:
    """Worker-local LRU; compressed bytes plus full decoded object size are charged."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("cache byte limit must be nonnegative")
        self.max_bytes = max_bytes
        self.bytes = 0
        self.peak_bytes = 0
        self.hits = 0
        self.misses = 0
        self._entries: OrderedDict[tuple[str, ...], tuple[dict[str, Any], int]] = OrderedDict()

    def clear(self) -> None:
        self._entries.clear()
        self.bytes = 0

    def reset_peak(self) -> None:
        self.peak_bytes = self.bytes

    def read(self, frozen: FrozenRunInputs, digest: str) -> dict[str, Any]:
        # Even a warm read verifies the immutable admission identity. Corrupt
        # replacement bytes never inherit the previous entry's admission.
        blobs = BlobStore(Path(frozen.root))
        path = blobs.root / digest[:2] / digest
        if path.stat().st_size > MAX_SESSION_BYTES:
            raise ValueError("frozen session exceeds the compressed input bound")
        payload = blobs.get(digest)
        key = (frozen.source_root_id, frozen.recipe,
               frozen.version, frozen.underlying, digest, DECODER_VERSION)
        if key in self._entries:
            self.hits += 1
            self._entries.move_to_end(key)
            return self._entries[key][0]
        self.misses += 1
        decoded = _decode(payload)
        cost = len(payload) + _size(decoded)
        if cost <= self.max_bytes:
            while self._entries and self.bytes + cost > self.max_bytes:
                _, (_, removed) = self._entries.popitem(last=False)
                self.bytes -= removed
            self._entries[key] = (decoded, cost)
            self.bytes += cost
            self.peak_bytes = max(self.peak_bytes, self.bytes)
        return decoded


class FrozenRunData:
    def __init__(self, frozen: FrozenRunInputs, cache: DecodedOptionCache, *,
                 timings: RunTimings | None = None, chain_key: str | None = None) -> None:
        self.frozen, self.cache = frozen, cache
        self.timings, self.chain_key = timings, chain_key
        self._last: tuple[date, dict[str, Any]] | None = None

    def _day(self, day: date) -> dict[str, Any]:
        if self._last is None or self._last[0] != day:
            anchor = session_window_for_date(day).open_ms_utc
            if anchor not in self.frozen.sessions:
                raise ValueError("session is outside the frozen run input inventory")
            if self.timings is None:
                self._last = (day, self.cache.read(self.frozen, self.frozen.sessions[anchor]))
            else:
                with self.timings.measure("admit_decode", chain_key=self.chain_key):
                    self._last = (day, self.cache.read(self.frozen, self.frozen.sessions[anchor]))
        return self._last[1]

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        return self._day(trading_date).get("underlying")

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        return self._day(trading_date).get("listing")

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        return self._day(trading_date)["options"].get(contract.occ_symbol)

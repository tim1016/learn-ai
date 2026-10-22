"""Admitted option-session containers, preserving exact legacy bytes (ADR 0078).

Each ticker member is the original deterministic minute-trade zip. Keeping it
intact lets an old saved run verify its original byte pin after compaction.
The manifest distinguishes validated empty answers from unresolved contracts.
Every member is validated before any contract is returned, including members
that the caller did not request. A damaged published container never falls
back to legacy files. Readers ask one contract at a time, so a read admits the
container's bytes (and the listing's) every time and shares the decode of the
same admitted bytes with every earlier read (#3435).
"""
from __future__ import annotations

import hashlib
import io
import json
import lzma
import re
import threading
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from app.data_lake.admission import read_committed_bytes
from app.data_lake.data_contract import data_contract_hash
from app.data_lake.lean_writer import _DETERMINISTIC_ZIP_DATE_TIME
from app.data_lake.option_bars import OptionBarsFileError, OptionDayBars, SessionMemberRef, parse_option_day_zip
from app.data_lake.option_lake import OPTION_PROVIDER, option_read_root
from app.data_lake.option_listing import (
    OptionListing,
    listing_from_admitted_bytes,
    parse_option_listing_zip,
    read_admitted_listing_bytes,
)
from app.data_lake.path_policy import OptionSessionPath
from app.data_lake.root_identity import active_root_id, read_marker
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.session_anchors import et_date_at_ms

OPTION_SESSION_FORMAT = 1
MAX_SESSION_MEMBERS = 100_002
MAX_SESSION_COMPRESSED_BYTES = 128 * 1024 * 1024
MAX_SESSION_DECODED_BYTES = 512 * 1024 * 1024
MAX_TICKER_DECODED_BYTES = 8 * 1024 * 1024
MAX_LISTING_DECODED_BYTES = 32 * 1024 * 1024
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]*")
_MANIFEST = "manifest.json"
_LISTING = "listing.zip"


class OptionSessionFileError(OptionBarsFileError):
    """The entire session is unreadable; legacy bytes must not conceal it."""


def option_session_dch() -> str:
    return data_contract_hash(
        provider=OPTION_PROVIDER,
        provider_params={"asset_class": "option", "layout": "session", "format": OPTION_SESSION_FORMAT},
        price_adjustment_mode="raw", session_policy="full", lean_format_version=1,
    )


def _symbols(listing: OptionListing) -> frozenset[str]:
    symbols = [row.ticker.removeprefix("O:") for row in listing.rows]
    if len(symbols) != len(set(symbols)) or any(not _SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise OptionSessionFileError("listing holds duplicate or unsafe ticker identities")
    return frozenset(symbols)


def _member(symbol: str) -> str:
    if not _SYMBOL.fullmatch(symbol):
        raise OptionSessionFileError("unsafe contract identity")
    return f"contracts/{symbol}.zip"


def _json(payload: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OptionSessionFileError("duplicate manifest field")
            result[key] = value
        return result
    try:
        value = json.loads(payload, object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise OptionSessionFileError("invalid session manifest") from exc
    if not isinstance(value, dict):
        raise OptionSessionFileError("session manifest must be an object")
    return value


@dataclass(frozen=True)
class OptionSession:
    underlying: str
    root_id: str
    session_open_ms: int
    listing_sha256: str
    sha256: str
    days: Mapping[str, OptionDayBars]
    original_bytes: Mapping[str, bytes]
    unresolved: frozenset[str]

    @property
    def contracts_complete(self) -> bool:
        """Every listed contract is answered; whole-session readiness also needs the underlying day."""
        return not self.unresolved

    @property
    def captured(self) -> frozenset[str]:
        return frozenset(symbol for symbol, day in self.days.items() if day.bars)

    @property
    def confirmed_empty(self) -> frozenset[str]:
        return frozenset(self.days) - self.captured

    @property
    def listed_count(self) -> int:
        return len(self.days) + len(self.unresolved)

    @property
    def answered_count(self) -> int:
        return len(self.days)

    def legacy_bytes(self, symbol: str) -> bytes:
        """Exact original zip bytes retained for pre-container pins."""
        return self.original_bytes[symbol]

    def option_day(self, symbol: str) -> OptionDayBars | None:
        return self.days.get(symbol)

    def reference(self, symbol: str) -> SessionMemberRef:
        day = self.days[symbol]
        return SessionMemberRef(self.sha256, day.sha256, self.listing_sha256, option_session_dch(), self.root_id,
                                "captured" if day.bars else "confirmed_empty")


def build_option_session_bytes(listing: OptionListing, captured: Mapping[str, bytes], *, root_id: UUID | str | None = None) -> bytes:
    """Encode admitted original ticker zips without inventing any empty outcome."""
    listed = _symbols(listing)
    if listing.source_bytes is None or hashlib.sha256(listing.source_bytes).hexdigest() != listing.sha256:
        raise OptionSessionFileError("the exact pinned listing bytes must be retained")
    listing_decoded = check_member_size(listing.source_bytes, max_decoded_bytes=MAX_LISTING_DECODED_BYTES)
    if parse_option_listing_zip(listing.source_bytes, listing.underlying, listing.as_of) != listing.rows:
        raise OptionSessionFileError("listing bytes and parsed identity disagree")
    if not set(captured) <= listed:
        raise OptionSessionFileError("captured ticker is outside the pinned listing")
    members: dict[str, bytes] = {_LISTING: listing.source_bytes}
    states: dict[str, dict[str, Any]] = {}
    decoded = listing_decoded
    for symbol, payload in sorted(captured.items()):
        decoded += check_member_size(payload)
        if decoded > MAX_SESSION_DECODED_BYTES:
            raise OptionSessionFileError("session exceeds decoded-bar bound")
        bars = parse_option_day_zip(payload, symbol, listing.as_of)
        members[_member(symbol)] = payload
        states[symbol] = {"member": _member(symbol), "sha256": hashlib.sha256(payload).hexdigest(),
                          "bars": len(bars), "state": "captured" if bars else "confirmed_empty"}
    manifest = {"data_root_id": str(root_id or active_root_id()), "format": OPTION_SESSION_FORMAT, "recipe": option_session_dch(), "underlying": listing.underlying,
                "session_open_ms": session_open_ms_utc(listing.as_of), "listing_sha256": listing.sha256,
                "listed_count": len(listed), "answered_count": len(captured), "contracts": states,
                "unresolved": sorted(listed - set(captured))}
    members[_MANIFEST] = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(members) > MAX_SESSION_MEMBERS or sum(map(len, members.values())) > MAX_SESSION_DECODED_BYTES:
        raise OptionSessionFileError("session exceeds member or decoded-byte bound")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=_DETERMINISTIC_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    payload = out.getvalue()
    if len(payload) > MAX_SESSION_COMPRESSED_BYTES:
        raise OptionSessionFileError("session exceeds compressed-byte bound")
    return payload


def check_member_size(payload: bytes, *, max_decoded_bytes: int | None = None) -> int:
    """The decoded size of a member zip's one entry; any other shape, or one past the bound, is refused."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            bound = MAX_TICKER_DECODED_BYTES if max_decoded_bytes is None else max_decoded_bytes
            if len(archive.infolist()) != 1 or archive.infolist()[0].file_size > bound:
                raise OptionSessionFileError("member exceeds decoded-member bound")
            return archive.infolist()[0].file_size
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise OptionSessionFileError("invalid ticker member") from exc


def parse_option_session(payload: bytes, listing: OptionListing, *, root_id: UUID | str | None = None) -> OptionSession:
    """Validate all coverage and all members, before exposing any one ticker."""
    if len(payload) > MAX_SESSION_COMPRESSED_BYTES:
        raise OptionSessionFileError("session exceeds compressed-byte bound")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (len(infos) > MAX_SESSION_MEMBERS or len(names) != len(set(names)) or _MANIFEST not in names or _LISTING not in names
                    or any(info.flag_bits & 1 or info.is_dir() for info in infos)
                    or sum(info.file_size for info in infos) > MAX_SESSION_DECODED_BYTES):
                raise OptionSessionFileError("invalid or oversized session members")
            manifest = _json(archive.read(_MANIFEST))
            listing_raw = archive.read(_LISTING)
            listing_decoded = check_member_size(listing_raw, max_decoded_bytes=MAX_LISTING_DECODED_BYTES)
            if (hashlib.sha256(listing_raw).hexdigest() != listing.sha256
                    or parse_option_listing_zip(listing_raw, listing.underlying, listing.as_of) != listing.rows):
                raise OptionSessionFileError("embedded listing does not match its pinned identity")
            expected = (str(root_id or active_root_id()), OPTION_SESSION_FORMAT, option_session_dch(), listing.underlying,
                        session_open_ms_utc(listing.as_of), listing.sha256)
            actual = tuple(manifest.get(key) for key in ("data_root_id", "format", "recipe", "underlying", "session_open_ms", "listing_sha256"))
            if actual != expected or type(manifest.get("format")) is not int:
                raise OptionSessionFileError("session recipe, listing or identity mismatch")
            states, pending = manifest.get("contracts"), manifest.get("unresolved")
            if not isinstance(states, dict) or not isinstance(pending, list) or any(not isinstance(s, str) for s in pending):
                raise OptionSessionFileError("invalid session coverage")
            listed = _symbols(listing)
            if (len(pending) != len(set(pending)) or set(states) & set(pending)
                    or set(states) | set(pending) != listed or manifest.get("listed_count") != len(listed)
                    or manifest.get("answered_count") != len(states)
                    or type(manifest.get("listed_count")) is not int or type(manifest.get("answered_count")) is not int):
                raise OptionSessionFileError("coverage does not partition the pinned listing")
            if set(names) != {_MANIFEST, _LISTING, *(_member(symbol) for symbol in states)}:
                raise OptionSessionFileError("unknown or unsafe session member")
            days: dict[str, OptionDayBars] = {}
            originals: dict[str, bytes] = {}
            decoded = listing_decoded
            for symbol, entry in states.items():
                if not isinstance(entry, dict) or entry.get("member") != _member(symbol):
                    raise OptionSessionFileError("invalid contract member identity")
                raw = archive.read(_member(symbol))
                decoded += check_member_size(raw)
                if decoded > MAX_SESSION_DECODED_BYTES:
                    raise OptionSessionFileError("session exceeds decoded-bar bound")
                digest = hashlib.sha256(raw).hexdigest()
                bars = parse_option_day_zip(raw, symbol, listing.as_of)
                if any(et_date_at_ms(bar.start_ms + 60_000) != listing.as_of for bar in bars):
                    raise OptionSessionFileError("a member bar closes outside its session date")
                if (entry.get("sha256") != digest or type(entry.get("bars")) is not int or entry["bars"] != len(bars)
                        or entry.get("state") != ("captured" if bars else "confirmed_empty")):
                    raise OptionSessionFileError("contract hash, count or absence proof mismatch")
                days[symbol] = OptionDayBars(symbol, listing.as_of, bars, digest)
                originals[symbol] = raw
    except OptionSessionFileError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError, EOFError, zlib.error, lzma.LZMAError) as exc:
        raise OptionSessionFileError(f"unreadable option session ({type(exc).__name__})") from exc
    return OptionSession(listing.underlying, str(manifest["data_root_id"]), session_open_ms_utc(listing.as_of), listing.sha256,
                         hashlib.sha256(payload).hexdigest(), days, originals, frozenset(pending))


def _admitted_session_bytes(underlying: str, trading_date: date, admission: Callable[[Path], bytes],
                            lake_root: Path | None) -> bytes | None:
    """The published container's admitted bytes, ``None`` when there is none, refused past the compressed bound."""
    path = option_read_root(lake_root) / OptionSessionPath(underlying, trading_date).relative_path()
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_SESSION_COMPRESSED_BYTES:
        raise OptionSessionFileError("session exceeds compressed-byte bound")
    return admission(path)


def _root_id(lake_root: Path | None) -> UUID:
    marker = read_marker(option_read_root(lake_root).parent.parent)
    if marker is None:
        raise OptionSessionFileError("session root has no identity marker")
    return marker.data_root_id


@dataclass(frozen=True)
class _SessionKey:
    """All a decoded session depends on: the admitted container and listing bytes, and the root and session read as."""

    content_digest: str
    listing_sha256: str
    root_id: str
    underlying: str
    trading_date: date


class OptionSessionDecodeScope:
    """The session decoded most recently, so contract-at-a-time reads of it decode once (#3435).

    One slot, not a cache: a session is large and these readers ask about one session at a time. The slot is emptied
    before a different session is decoded, so at most one stays resident. Readers run on worker threads: one lock
    serialises the slot and is held through a decode, so concurrent reads of one session decode it once.
    Retained readers own their scope, releasing the full container with the reader (#3440).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: tuple[_SessionKey, OptionSession] | None = None

    def get_or_decode(self, key: _SessionKey, decode: Callable[[], OptionSession]) -> OptionSession:
        with self._lock:
            if self._held is not None and self._held[0] == key:
                return self._held[1]
            self._held = None
            session = decode()
            self._held = (key, session)
            return session


_LAST_SESSION = OptionSessionDecodeScope()


def _decoded(payload: bytes, listing_sha256: str, listing: Callable[[], OptionListing], underlying: str,
             trading_date: date, root_id: UUID, *, decode_scope: OptionSessionDecodeScope | None = None) -> OptionSession:
    """``parse_option_session`` of admitted bytes, shared by every read of the same admitted bytes (#3435).

    The caller has admitted ``payload`` and the listing whose bytes hash to ``listing_sha256``; a hit reuses only
    the decode. ``listing`` is called only on a miss, so a hit parses neither the container nor the listing.
    """
    key = _SessionKey(hashlib.sha256(payload).hexdigest(), listing_sha256, str(root_id), underlying, trading_date)
    scope = _LAST_SESSION if decode_scope is None else decode_scope
    return scope.get_or_decode(key, lambda: parse_option_session(payload, listing(), root_id=root_id))


def read_option_session(underlying: str, trading_date: date, *, admission: Callable[[Path], bytes] = read_committed_bytes,
                        lake_root: Path | None = None, listing: OptionListing | None = None) -> OptionSession | None:
    """Read contract coverage. Option bytes remain readable when underlying bars are missing.

    Every read admits the container's bytes, and the listing's unless one is handed in; the decode is shared with
    every earlier read of the same admitted bytes while that session is the last one decoded.
    """
    payload = _admitted_session_bytes(underlying, trading_date, admission, lake_root)
    if payload is None:
        return None
    if listing is not None:
        return _decoded(payload, listing.sha256, lambda: listing, underlying, trading_date, _root_id(lake_root))
    listing_bytes = read_admitted_listing_bytes(underlying, trading_date, admission=admission, lake_root=lake_root)
    if listing_bytes is None:
        raise OptionSessionFileError("published session has no admitted listing")
    listing_sha256 = hashlib.sha256(listing_bytes).hexdigest()

    return _decoded(payload, listing_sha256, lambda: listing_from_admitted_bytes(underlying, trading_date, listing_bytes),
                    underlying, trading_date, _root_id(lake_root))


def parse_retained_option_session(payload: bytes, listing: OptionListing, *,
                                  decode_scope: OptionSessionDecodeScope | None = None) -> OptionSession:
    """Decode a digest-verified proof blob in its original root identity.

    Unlike a lake read, a retained proof is portable and has no physical root.
    Its trusted identity is the immutable blob digest from its frozen manifest.
    """
    if len(payload) > MAX_SESSION_COMPRESSED_BYTES:
        raise OptionSessionFileError("retained session exceeds compressed-byte bound")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            root_id = UUID(_json(archive.read(_MANIFEST))["data_root_id"])
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        raise OptionSessionFileError("retained session has no valid original root identity") from exc
    return _decoded(payload, listing.sha256, lambda: listing, listing.underlying, listing.as_of, root_id,
                    decode_scope=decode_scope)


def read_option_session_for_listing_rebind(underlying: str, trading_date: date) -> OptionSession | None:
    """Explicit maintenance only: validate the original admitted listing and bytes.

    Ordinary readers always require today's admitted listing. This entry point
    permits a deliberate repack after a listing refresh while preserving old
    resolved member bytes; corruption is still a whole-container refusal.
    """
    payload = _admitted_session_bytes(underlying, trading_date, read_committed_bytes, None)
    if payload is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_SESSION_MEMBERS or sum(info.file_size for info in infos) > MAX_SESSION_DECODED_BYTES:
                raise OptionSessionFileError("invalid or oversized session members")
            raw = archive.read(_LISTING)
    except (KeyError, zipfile.BadZipFile, ValueError, zlib.error, OSError) as exc:
        raise OptionSessionFileError("original listing cannot be recovered from the session") from exc
    check_member_size(raw, max_decoded_bytes=MAX_LISTING_DECODED_BYTES)
    listing = OptionListing(underlying, trading_date, parse_option_listing_zip(raw, underlying, trading_date),
                            hashlib.sha256(raw).hexdigest(), raw)
    return parse_option_session(payload, listing, root_id=_root_id(None))

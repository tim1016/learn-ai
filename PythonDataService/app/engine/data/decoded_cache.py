"""Process-local reuse of decoded artifacts across sequential research evaluations.

One decode per artifact per cache residency, never one decode per evaluation
(#3038, PRD #3034 slice 3). The cache sits strictly *after* admission: a
caller hands it a key only once the artifact's bytes have passed the whole
current chain — committed-lake receipt, the manifest comparison where the
reader is bound to one, and the adjustment/companion verification — so a hit
reuses the decode, never the admission verdict. Every read of every
evaluation re-runs that chain and re-hashes the source bytes; what the cache
removes is only the zip decode behind an unchanged digest.

What an entry is: the immutable decoded bars of one *artifact* — a day's
minute zip, or a symbol's whole daily-history zip. A window is an ordered
selection of artifacts, so overlapping windows and different run-up depths
reuse the same entries. Study identity, point, scored window and run-up
depth are deliberately absent from the key: two evaluations that read the
same admitted bytes under the same session policy and calendar get the same
decode, which is the reuse the PRD asks for. Strategy/portfolio state,
manifests and adjustment pins never enter the cache; each evaluation builds
its own readers and bindings as before.

Bounds and honesty: retention is measured in estimated decoded bytes
(``_TRADE_BAR_ESTIMATED_BYTES`` per bar, a fixed conservative estimate so
accounting is deterministic), capped at ``max_bytes``. In-use entries are
pinned and skipped by eviction; a new entry that cannot fit after evicting
every unpinned one — or that alone exceeds the whole bound — is decoded
through the uncached path and not retained, and the metrics say so. Bars
handed out are the shared immutable ``TradeBar`` objects (frozen dataclass)
in a fresh list, so one evaluation cannot mutate what another sees.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.data_lake.adjustment_versions import adjusted_root_for
from app.data_lake.path_policy import lake_root_for
from app.engine.data.trade_bar import TradeBar

# Bump when ``parse_day_zip``/``parse_history_zip`` output for identical bytes
# changes (a decode fix, a schema change), so existing resident entries can
# never be reused across decoder generations (#3038).
DECODED_ARTIFACT_SCHEMA_VERSION = 1

# Fixed per-bar estimate of a decoded TradeBar's retained footprint on
# CPython: the frozen instance with its attribute dict plus its four Decimal
# fields measure ~760 bytes in practice; 800 adds headroom for large volume
# ints without pretending to a precision ``sys.getsizeof`` chains cannot
# honestly give. Deterministic by construction, so accounting and eviction
# are reproducible.
_TRADE_BAR_ESTIMATED_BYTES = 800
_LIST_OVERHEAD_BYTES = 64


@dataclass(frozen=True)
class ArtifactKey:
    """Everything two reads must agree on before their decodes may be shared.

    Content digest pins the exact bytes; root identity/mode and the canonical
    relative path pin *which* physical artifact they came from (the same
    digest under two roots is two artifacts, each admitted on its own
    receipt); session policy, calendar identity/version and the decoder
    version pin everything that shapes the decoded bars but lives outside the
    zip (the RTH filter follows the calendar; the decoder version bounds the
    parse code itself). Adjustment evidence is the corporate-action basis the
    reader's guard pinned for this read, or ``""`` for raw/reference trees.
    """

    root_id: str
    root_mode: Literal["reference", "lake-raw", "lake-adjusted"]
    relative_path: str
    content_digest: str
    instrument: str
    resolution: Literal["minute", "daily"]
    adjustment_evidence: str
    session_policy: str
    calendar_identity: str
    calendar_version: str
    decoder_version: str = str(DECODED_ARTIFACT_SCHEMA_VERSION)


def artifact_key(
    *,
    artifact_path: Path,
    data_roots: Sequence[Path],
    content_digest: str,
    instrument: str,
    resolution: Literal["minute", "daily"],
    adjustment_evidence: str,
    session_policy: str,
) -> ArtifactKey:
    """Build one artifact's cache key from the read the caller just verified.

    ``artifact_path`` is the exact file the reader resolved and admitted;
    ``data_roots`` is the reader's root order, used to name a reference
    fixture's owning root. The calendar identity comes from the canonical
    calendar module (ADR 0022) — imported lazily so importing this module
    costs no pandas.
    """
    # Lazy: lean_format imports this module, and its readers must not pay for
    # the calendar stack until bars are actually read through a cache.
    from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION

    root = lake_root_for(artifact_path) or lake_root_for(artifact_path.resolve())
    if root is not None:
        mode: Literal["reference", "lake-raw", "lake-adjusted"] = (
            "lake-adjusted" if adjusted_root_for(artifact_path) is not None else "lake-raw"
        )
        relative = artifact_path.resolve().relative_to(root.resolve()).as_posix()
        root_id = os.path.realpath(root)
    else:
        mode = "reference"
        resolved = artifact_path.resolve()
        owner = next(
            (candidate for candidate in data_roots if resolved.is_relative_to(candidate.resolve())),
            artifact_path.parent,
        )
        root_id = os.path.realpath(owner)
        relative = (
            resolved.relative_to(owner.resolve()).as_posix()
            if resolved.is_relative_to(owner.resolve())
            else artifact_path.name
        )
    return ArtifactKey(
        root_id=root_id,
        root_mode=mode,
        relative_path=relative,
        content_digest=content_digest,
        instrument=instrument.upper(),
        resolution=resolution,
        adjustment_evidence=adjustment_evidence,
        session_policy=session_policy,
        calendar_identity=CALENDAR_IDENTITY,
        calendar_version=CALENDAR_PACKAGE_VERSION,
    )


def _estimated_bytes(bars: Sequence[TradeBar]) -> int:
    return _LIST_OVERHEAD_BYTES + len(bars) * _TRADE_BAR_ESTIMATED_BYTES


@dataclass
class _Entry:
    bars: tuple[TradeBar, ...]
    estimated_bytes: int
    pins: int = 0


@dataclass
class _CacheMetrics:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    # Decodes that ran because the result could not be retained: the entry
    # alone exceeds the bound, or nothing evictable could make room.
    uncached_decodes: int = 0
    resident_entries: int = 0
    resident_bytes: int = 0
    pinned_bytes: int = 0
    max_bytes: int = 0
    schema_version: int = DECODED_ARTIFACT_SCHEMA_VERSION
    duplicate_residency_bytes: int = 0
    keys_seen: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "uncached_decodes": self.uncached_decodes,
            "resident_entries": self.resident_entries,
            "resident_bytes": self.resident_bytes,
            "pinned_bytes": self.pinned_bytes,
            "max_bytes": self.max_bytes,
            "schema_version": self.schema_version,
            "duplicate_residency_bytes": self.duplicate_residency_bytes,
            "keys_seen": self.keys_seen,
        }


class DecodedArtifactCache:
    """A bounded store of admitted decodes shared by one process's evaluations.

    Construct one where sequential evaluations are owned (a benchmark run, a
    research coordinator); pass it to each evaluation's reader. Nothing
    global holds a cache, so a caller that passes none keeps today's
    decode-per-read behavior byte for byte (#3038: behind explicit research
    configuration until qualification). No internal locking: the serial
    research path owns it; the parallel runtime's slice owns cross-worker
    coordination and may not share one instance across threads.
    """

    def __init__(self, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError(f"decoded-artifact cache bound must be positive, got {max_bytes}")
        self._max_bytes = max_bytes
        # Oldest-used first (LRU): hits and stores move entries to the end.
        self._entries: OrderedDict[ArtifactKey, _Entry] = OrderedDict()
        self._metrics = _CacheMetrics(max_bytes=max_bytes)
        # Content digests currently resident, for duplicate-residency honesty:
        # the same bytes admitted under two roots hold twice in this process.
        self._digest_residency: dict[str, int] = {}
        # Keys already handed out by ``take_residency_delta``: a key reports
        # once when it becomes resident, and again if it is evicted and
        # re-decoded — the delta must stay a delta, not a census (#3048).
        self._reported: set[ArtifactKey] = set()
        # Victims evicted since the last delta take, so the parent's warm
        # hints can forget what no child holds (#3048 review).
        self._evicted_since: list[ArtifactKey] = []

    def get_or_decode(self, key: ArtifactKey, decode: Callable[[], list[TradeBar]]) -> list[TradeBar]:
        """Return ``key``'s bars, decoding through ``decode`` only on a miss.

        The caller has already run the admission chain on the artifact's
        current bytes; this method never reads a file and never re-decides
        admission. Hits hand back the shared immutable bars in a fresh list.
        """
        self._metrics.keys_seen += 1
        entry = self._entries.get(key)
        if entry is not None:
            self._metrics.hits += 1
            self._entries.move_to_end(key)
            return list(entry.bars)
        self._metrics.misses += 1
        bars = decode()
        estimated = _estimated_bytes(bars)
        if estimated > self._max_bytes:
            # One artifact cannot fit in the whole allowance by construction:
            # the bounded-uncached path, not a bound violation.
            self._metrics.uncached_decodes += 1
            return bars
        self._evict_until(estimated)
        if self._resident_bytes() + estimated > self._max_bytes:
            # Pinned entries leave no room; decode without retaining rather
            # than exceed the bound or evict an in-use iterator's entry.
            self._metrics.uncached_decodes += 1
            return bars
        self._store(key, bars, estimated)
        return list(bars)

    def pin(self, key: ArtifactKey) -> None:
        """Mark ``key``'s entry in use; eviction skips it until unpinned."""
        self._entries[key].pins += 1

    def unpin(self, key: ArtifactKey) -> None:
        entry = self._entries[key]
        if entry.pins <= 0:
            raise ValueError(f"unpin of an unpinned entry: {key.relative_path}")
        entry.pins -= 1

    def metrics(self) -> dict[str, Any]:
        """A snapshot of the accounting the benchmark reports (#3038)."""
        self._refresh_residency()
        return self._metrics.as_dict()

    def take_residency_delta(self) -> tuple[list[tuple[ArtifactKey, int]], list[ArtifactKey]]:
        """Additions and removals since the last take — the warm-affinity hint (#3048).

        The shared pool's scheduler uses the additions to prefer a lane
        whose child already holds an artifact's decode, and the removals to
        forget what an eviction or a bound no longer holds — a hint that
        only grows would route toward lanes that went cold and report
        residency no child has (#3048 review). A stale hint still costs one
        re-decode and nothing else: admission stays per-read whatever a
        hint claimed, so residency is never correctness.
        """
        added = [(key, entry.estimated_bytes) for key, entry in self._entries.items() if key not in self._reported]
        self._reported.update(key for key, _ in added)
        evicted = [key for key in self._evicted_since if key in self._reported]
        self._reported.difference_update(self._evicted_since)
        self._evicted_since = []
        return added, evicted

    def _store(self, key: ArtifactKey, bars: list[TradeBar], estimated: int) -> None:
        self._entries[key] = _Entry(bars=tuple(bars), estimated_bytes=estimated)
        self._digest_residency[key.content_digest] = self._digest_residency.get(key.content_digest, 0) + 1

    def _resident_bytes(self) -> int:
        return sum(entry.estimated_bytes for entry in self._entries.values())

    def _evict_until(self, needed: int) -> None:
        while self._resident_bytes() + needed > self._max_bytes:
            victim = next((key for key, entry in self._entries.items() if entry.pins == 0), None)
            if victim is None:
                return
            self._entries.pop(victim)
            self._evicted_since.append(victim)
            self._metrics.evictions += 1
            remaining = self._digest_residency.get(victim.content_digest, 0) - 1
            if remaining > 0:
                self._digest_residency[victim.content_digest] = remaining
            else:
                self._digest_residency.pop(victim.content_digest, None)

    def _refresh_residency(self) -> None:
        self._metrics.resident_entries = len(self._entries)
        self._metrics.resident_bytes = self._resident_bytes()
        self._metrics.pinned_bytes = sum(entry.estimated_bytes for entry in self._entries.values() if entry.pins > 0)
        # Current duplicate residency: bytes held more than once in this
        # process because the same admitted content sits under two keys.
        duplicated = {digest for digest, count in self._digest_residency.items() if count > 1}
        self._metrics.duplicate_residency_bytes = sum(
            entry.estimated_bytes for key, entry in self._entries.items() if key.content_digest in duplicated
        )

"""One-time, idempotent import of the existing lean-cache into the lake catalog.

Adopts zips already produced by the pre-lake Polygon->LEAN cache
(``PythonDataService/lean-cache/<policy>/``) without a single provider call:
for each existing zip, verify it, hash it, place the *unmodified* bytes under
the lake's on-disk layout, and insert a catalog row under the zip's *true*
adjustment mode (read from the cache's per-symbol provenance file, never
guessed from a directory-name convention).

Issue: #1832. Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.

Cache layout expected under ``--cache-root`` (one "policy root" at a time,
e.g. ``lean-cache/polygon-adjusted/`` or ``lean-cache/polygon-raw/``): each
minute-trade zip sits at the same relative path
``app.data_lake.path_policy.LeanMinuteBarPath`` would construct for it, plus a
sibling ``provenance/<symbol_lower>.json`` document (schema:
``{"schema_version": 1, "symbol": "SPY", "policy": {"source": "polygon",
"adjusted": bool}, "fetches": [{"resolution": "minute", "from_date": ...,
"to_date": ..., "fetched_at_ms": ...}, ...]}``). The whole document is
validated, not just ``policy.adjusted``: schema_version, the document's own
``symbol`` matching the directory it was found under, ``policy.source`` being
``'polygon'`` (the only provider this importer understands), and -- per
discovered zip -- at least one ``resolution="minute"`` fetch range covering
that zip's trading date. Any violation refuses the affected symbol or
artifact rather than silently claiming a catalog row a provenance document
doesn't actually attest to.

``--lake-root`` is any root carrying a valid, marked ``.data-root.json``
(issue #1878, PR B of #1861) -- it no longer needs to equal the process's
configured write root. Root-aware since PR A gave every physical root a
portable identity (``app.data_lake.root_identity``): this importer opens and
validates the *selected* root's marker via ``root_identity.read_marker``,
uses that marker's ``data_root_id`` for every catalog claim/lookup, and
resolves paths relative to that root -- never the configured
``settings.LEAN_DATA_WRITE_ROOT``. An unmarked or malformed root is refused
(``LakeRootIdentityError``, re-exported from ``root_identity``) rather than
silently stamped; an operator marks a root first via
``scripts/manage_data_root.py`` (``init`` for a brand-new root, ``stamp
--force`` for an existing populated one) -- the same explicit administrative
path every other root-identity consumer goes through. Because the catalog's
uniqueness now leads with ``DataRootId`` (the index rebuild this PR ships),
importing into a second physical root with the same symbol/date/artifact
identity as a first no longer collides with it, and no longer risks
"phantom coverage": each root's rows resolve only against that root's own
``data_root_id``. Artifacts land under
``<selected-root>/lake/<price-adjustment-mode>/...`` (the LEAN-relative tail
is ``app.data_lake.path_policy.LeanMinuteBarPath`` unchanged; the mode segment
sits above it), staged through ``<selected-root>/staging/...`` per
``app.data_lake.atomic``. Staging is deliberately not mode-keyed: its paths
are already unique per ``(request_id, worker_id, attempt)``.

Idempotency and no-overwrite are decided by ``decide_claim_outcome`` (pure,
unit-tested in isolation): re-running the import re-derives the same content
hash for every zip and finds the prior complete row already claims that
identity, so nothing is re-written or re-inserted -- but the physical
destination file is still inspected before trusting that claim (see layer 2
below): a missing file gets restored from the cache zip; a file with
*different* bytes than the catalog's recorded hash is a refusal, never
silently trusted. A catalog row whose existing hash differs from the cache
zip's current hash is a refusal (``FailedArtifact`` with
``reason="hash_conflict"``) -- the row and the on-disk file are left exactly
as they were; overwriting them would silently discard whichever version is
later correct.

Schema note: the v1 ``ck_raw_only_for_canonical_data_root`` CHECK constraint
(``Backend/Migrations/20260521033222_AddDataLakeArtifactsAndRuns.cs``)
originally allowed only ``PriceAdjustmentMode = 'raw'`` for every non-metadata
row -- a temporary widening
(``Backend/Migrations/20260827120000_AllowImportedNonRawAdjustmentModes.cs``)
later admitted ``'polygon_split_adjusted'`` too. PR B
(``Backend/Migrations/20260830120000_ActivateDataRootScopedCatalogIdentity.cs``)
drops the constraint outright, exactly as its own comment anticipated
("Relaxed in v2 by adding data_root_id and dropping this constraint"):
adjustment mode is a physical path segment under a root-scoped identity now,
so the single-canonical-root restriction no longer represents reality.

One lake root per adjustment mode — structurally, since #1839. The mode is a
path segment above the LEAN tree
(``path_policy.lake_root_within(base_root, price_adjustment_mode)``), so a 'raw' and a
'polygon_split_adjusted' artifact for the same (market, symbol, date, type)
resolve to different absolute paths and simply cannot overwrite one another.
Each symbol's mode comes from its own provenance document, so one invocation
may import a mixture; the roots never meet.

Three layers used to stand where that sentence now does, because
``LeanMinuteBarPath`` carried no mode component and both artifacts resolved to
the *identical* path: a per-symbol marker-and-emptiness gate under a
cross-process advisory lock, an operator ``--claim-unmarked-root-as`` escape
hatch for an unmarked-but-populated root, and a shared low-level refusal in
``atomic_write_and_promote``. All three are deleted. They guarded a collision
the path shape now makes impossible, and the ``data_root_id`` redesign the
schema note anticipates is no longer needed for this: catalog ``FilePath`` is
root-relative, so the mode segment cost zero catalog rows.

What remains, and is not redundant with the above:

  **File-level guard** (``decide_destination_outcome``, pure and unit-tested
  like ``decide_claim_outcome``; ``_import_one_zip`` does the surrounding I/O
  and the ``fail_artifact`` side effect, all inside the same guarded section
  a write failure uses -- an unreadable destination refuses that one artifact
  and continues, it does not abort the whole import). Before promoting any
  zip, if a file already sits at the destination ``LeanMinuteBarPath``:
  identical content hash → treated as an idempotent no-op (the freshly-claimed
  row is still completed, just without rewriting the bytes); *different* hash
  → a typed refusal (``FailedArtifact`` with
  ``reason="destination_file_conflict"``) and ``atomic_write_and_promote``
  (hence ``os.replace``) is never called. The same guard also runs on the
  idempotent re-run path (catalog already complete with a matching hash): a
  missing physical file is restored from the cache zip rather than trusted on
  the catalog's word alone; a mismatched one is refused the same way. This
  survives because it guards a different failure — two *same-mode* writers
  disagreeing about content — which no path segment can prevent.

Out of scope: the pre-policy legacy minute-bar tree directly under
``lean-cache/`` (no sibling ``provenance/``) is not a policy root this
importer recognizes — pointing ``--cache-root`` at it (or at a policy root
missing a symbol's provenance file) surfaces as a typed
``missing_provenance`` refusal, never a guessed adjustment mode. Whether to
adopt or discard that legacy tree is a decision for the data-lake
integration slice, not this one-time tool.

Recovery after a claimed-but-failed write: if a zip claims its catalog row
successfully but then fails to write to the lake or to complete (disk full,
a dropped DB connection, an unreadable destination file, ...), the row is
explicitly marked ``'failed'`` via ``catalog_client.fail_artifact`` rather
than left stranded in ``'fetching'`` forever. This importer does not itself
retry a failed or in-flight row on a later run (it has no lease-stealing
loop); recovering one requires an external tool calling
``catalog_client.steal_or_retry_minute_bar`` (or the sweep), the same as any
other stuck artifact in the catalog.

Usage::

    python -m app.data_lake.cache_import \\
        --cache-root PythonDataService/lean-cache/polygon-adjusted \\
        --lake-root /app/data-lake
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.data_lake import catalog_client, root_identity
from app.data_lake.atomic import (
    ArtifactLeaseLostError,
    atomic_write_and_promote,
    write_lease_gated_artifact,
)
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.path_policy import (
    LeanMinuteBarPath,
    ensure_lean_readable_layout,
    lake_root_within,
    minute_bar_market_root,
    staging_root_within,
)

# Re-exported so existing `from app.data_lake.cache_import import
# LakeRootIdentityError` call sites (this module's own callers and its
# tests) keep working unchanged -- it is the same type root_identity's other
# administrative consumers (scripts/manage_data_root.py) already raise, not
# a parallel hierarchy.
from app.data_lake.root_identity import LakeRootIdentityError as LakeRootIdentityError
from app.data_lake.types import ArtifactIdentity, ArtifactRecord, polygon_mode_for
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.timestamps import now_ms_utc, to_ms_utc

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_WORKER_ID = os.environ.get("HOSTNAME", "cache-import")
_LEASE_TTL_MS = 300_000
_TRADE_ZIP_RE = re.compile(r"^(\d{8})_trade\.zip$")
_SUPPORTED_PROVENANCE_SCHEMA_VERSION = 1
_SUPPORTED_PROVENANCE_PROVIDER = "polygon"

# data_contract_hash provider params for an imported (not live-fetched)
# minute-trade artifact. 'import_source' distinguishes these from a live
# Polygon fetch's DCH (app.data_lake.ensure_data._minute_trade_dch) even
# when the underlying recipe (Polygon minute aggs) is otherwise identical —
# the DCH is part of the provenance trail, so "this was imported" belongs in
# it, not only in ProviderParams.
_IMPORT_MINUTE_TRADE_PARAMS_RAW = {
    "adjusted": False,
    "timespan": "minute",
    "multiplier": 1,
    "endpoint": "v2/aggs",
    "import_source": "lean_cache",
}
_IMPORT_MINUTE_TRADE_PARAMS_ADJUSTED = {
    "adjusted": True,
    "timespan": "minute",
    "multiplier": 1,
    "endpoint": "v2/aggs",
    "import_source": "lean_cache",
}


class MissingProvenanceError(RuntimeError):
    """A symbol's ``provenance/<symbol>.json`` is absent or fails validation.

    Despite the name, this covers "present but doesn't validate" too --
    wrong schema_version, a symbol field that disagrees with the directory
    the file was found under, a policy.source other than 'polygon', or a
    missing/malformed policy.adjusted -- not just "the file is missing". Any
    of these refuses the whole symbol rather than defaulting the adjustment
    mode to 'raw' or trusting a document that doesn't actually attest to
    what we're about to import.
    """


class CorruptCacheZipError(RuntimeError):
    """A cache zip cannot be verified: unreadable, missing its expected CSV
    member, encrypted or CRC-corrupted, or containing a malformed,
    negative-priced, duplicate-timestamped, or out-of-order row.

    Refused with no catalog row and no lake write — never silently repaired
    or partially imported.
    """


class ProvenanceCoverageError(RuntimeError):
    """No minute-resolution fetch range in a symbol's provenance document
    covers a discovered zip's trading date.

    A provenance document that validates its own shape (schema, symbol,
    policy.source, policy.adjusted) can still simply not *attest* to a
    particular day's data -- e.g. a hand-edited or partially-regenerated
    provenance file. Refused per-artifact rather than trusted on the
    strength of the rest of the document being well-formed.
    """


@dataclass(frozen=True)
class CacheZipRef:
    symbol: str
    trading_date: date
    zip_path: Path


@dataclass(frozen=True)
class UnrecognizedCacheEntry:
    """A ``*_trade.zip`` under the minute-bar tree that doesn't parse as a
    LEAN minute-trade filename (wrong date-prefix shape, or an invalid
    calendar date). Surfaced as a failure, never silently skipped — an
    unparseable file existing in the cache is exactly the kind of thing that
    would otherwise let "every existing cache zip appears as a complete
    catalog row" quietly stop being true."""

    symbol: str
    path: Path
    detail: str


@dataclass(frozen=True)
class VerifiedZip:
    row_count: int
    first_bar_start_ms: int
    last_bar_start_ms: int
    raw_bytes: bytes


@dataclass(frozen=True)
class ClaimDecision:
    action: Literal["proceed", "skip_duplicate", "conflict", "in_flight_or_incomplete"]
    detail: str | None = None


@dataclass(frozen=True)
class DestinationDecision:
    action: Literal["write", "already_present", "conflict"]
    detail: str | None = None


@dataclass(frozen=True)
class ImportedArtifact:
    symbol: str
    trading_date: date
    price_adjustment_mode: str
    artifact_id: int
    file_sha256: str
    row_count: int


@dataclass(frozen=True)
class SkippedArtifact:
    symbol: str
    trading_date: date
    reason: Literal["already_imported_same_hash"]


ImportFailureReason = Literal[
    "missing_provenance",
    "unrecognized_filename",
    "corrupt_zip",
    "hash_conflict",
    "in_flight_or_incomplete",
    "write_failed",
    "destination_file_conflict",
    "provenance_coverage_mismatch",
]


@dataclass(frozen=True)
class FailedArtifact:
    symbol: str
    # None when the filename itself didn't parse to a trading date
    # (an UnrecognizedCacheEntry) -- there is no date to report.
    trading_date: date | None
    reason: ImportFailureReason
    detail: str


@dataclass(frozen=True)
class ImportReport:
    imported: list[ImportedArtifact] = field(default_factory=list)
    skipped: list[SkippedArtifact] = field(default_factory=list)
    failed: list[FailedArtifact] = field(default_factory=list)


def discover_cache_zips(cache_root: Path) -> tuple[list[CacheZipRef], list[UnrecognizedCacheEntry]]:
    """Find every ``<yyyymmdd>_trade.zip`` under the cache root's minute-bar tree.

    The minute-bar tree location is derived from
    ``app.data_lake.path_policy.minute_bar_market_root`` — the sole path
    authority — rather than hand-built here.

    Returns ``(refs, unrecognized)``. ``refs`` is sorted by
    (symbol, trading_date) for deterministic processing order. A file that
    matches the ``*_trade.zip`` glob but doesn't parse as
    ``<yyyymmdd>_trade.zip`` (wrong shape, or 8 digits that aren't a valid
    calendar date) is reported in ``unrecognized`` instead of silently
    skipped. A cache root with no minute-bar tree yet (e.g. a policy
    directory that has never fetched anything) returns ``([], [])`` rather
    than raising.
    """
    minute_root = cache_root / Path(*minute_bar_market_root("usa").parts)
    if not minute_root.is_dir():
        return [], []
    refs: list[CacheZipRef] = []
    unrecognized: list[UnrecognizedCacheEntry] = []
    for symbol_dir in sorted(p for p in minute_root.iterdir() if p.is_dir()):
        symbol = symbol_dir.name.upper()
        for zip_path in sorted(symbol_dir.glob("*_trade.zip")):
            match = _TRADE_ZIP_RE.match(zip_path.name)
            if match is None:
                unrecognized.append(
                    UnrecognizedCacheEntry(
                        symbol=symbol,
                        path=zip_path,
                        detail=f"filename does not match <yyyymmdd>_trade.zip: {zip_path.name!r}",
                    )
                )
                continue
            date_str = match.group(1)
            try:
                trading_date = datetime.strptime(date_str, "%Y%m%d").date()
            except ValueError as exc:
                unrecognized.append(
                    UnrecognizedCacheEntry(
                        symbol=symbol,
                        path=zip_path,
                        detail=f"{date_str!r} is not a valid calendar date: {exc}",
                    )
                )
                continue
            refs.append(CacheZipRef(symbol=symbol, trading_date=trading_date, zip_path=zip_path))
    return sorted(refs, key=lambda r: (r.symbol, r.trading_date)), unrecognized


def load_symbol_provenance(cache_root: Path, symbol: str) -> dict[str, Any]:
    """Read and validate ``<cache-root>/provenance/<symbol>.json`` end to end.

    Beyond ``policy.adjusted`` (the true adjustment mode), also validates:
    schema shape (a JSON object with the supported ``schema_version``), the
    document's own ``symbol`` field matching the directory it was found
    under (refusing to trust a provenance file for a different symbol), and
    ``policy.source`` being ``'polygon'`` (the only provider this importer
    understands). Raises ``MissingProvenanceError`` on any violation.
    """
    prov_path = cache_root / "provenance" / f"{symbol.lower()}.json"
    if not prov_path.is_file():
        raise MissingProvenanceError(f"no provenance file at {prov_path}")
    try:
        data = json.loads(prov_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MissingProvenanceError(f"cannot parse {prov_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MissingProvenanceError(f"{prov_path} is not a JSON object")
    if data.get("schema_version") != _SUPPORTED_PROVENANCE_SCHEMA_VERSION:
        raise MissingProvenanceError(
            f"{prov_path} has schema_version={data.get('schema_version')!r}; only "
            f"{_SUPPORTED_PROVENANCE_SCHEMA_VERSION!r} is understood"
        )
    if data.get("symbol") != symbol:
        raise MissingProvenanceError(
            f"{prov_path} declares symbol={data.get('symbol')!r}, but was found under the "
            f"{symbol!r} directory -- refusing to trust a provenance file for a different symbol"
        )
    policy = data.get("policy")
    if not isinstance(policy, dict) or not isinstance(policy.get("adjusted"), bool):
        raise MissingProvenanceError(f"{prov_path} is missing a boolean policy.adjusted field")
    if policy.get("source") != _SUPPORTED_PROVENANCE_PROVIDER:
        raise MissingProvenanceError(
            f"{prov_path} declares policy.source={policy.get('source')!r}; only "
            f"{_SUPPORTED_PROVENANCE_PROVIDER!r} is a provider this importer understands"
        )
    if not isinstance(data.get("fetches"), list):
        raise MissingProvenanceError(f"{prov_path} is missing a fetches list")
    return data


def price_adjustment_mode_for(provenance: dict[str, Any]) -> str:
    """Map a provenance file's ``policy.adjusted`` to the catalog's enum value."""
    return polygon_mode_for(bool(provenance["policy"]["adjusted"]))


def provenance_covers_date(provenance: dict[str, Any], trading_date: date) -> bool:
    """True iff at least one minute-resolution fetch range in ``provenance``
    covers ``trading_date`` (inclusive on both ends).

    Pure and defensive: a malformed individual ``fetches`` entry (missing or
    unparsable dates, wrong resolution) is simply not a match, not an error
    -- ``load_symbol_provenance`` already guarantees ``fetches`` is a list,
    but says nothing about each entry's shape.
    """
    for fetch in provenance.get("fetches", []):
        if not isinstance(fetch, dict) or fetch.get("resolution") != "minute":
            continue
        try:
            from_date = date.fromisoformat(fetch["from_date"])
            to_date = date.fromisoformat(fetch["to_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if from_date <= trading_date <= to_date:
            return True
    return False


def _import_minute_trade_dch(adjusted: bool) -> str:
    return _dch(
        provider="polygon",
        provider_params=_IMPORT_MINUTE_TRADE_PARAMS_ADJUSTED if adjusted else _IMPORT_MINUTE_TRADE_PARAMS_RAW,
        price_adjustment_mode=polygon_mode_for(adjusted),
        session_policy="full",
        lean_format_version=1,
    )


def _fetch_range_anchors_ms(fetch: dict[str, Any]) -> tuple[int, int] | None:
    """Anchor one fetch entry's from_date/to_date to int64 ms UTC via the
    canonical calendar's session-open anchor, or None if unparsable.

    Per .claude/rules/temporal-rigor.md's date-anchored-value convention
    ("Trading date -> the session open (09:30 ET) of that date"): a bare
    ISO date is not itself a valid wire/storage format, so it's anchored at
    construction time via app.lean_sidecar.trading_calendar.session_open_ms_utc
    (the sole calendar authority), not a hardcoded UTC-midnight or
    fixed-offset guess.
    """
    try:
        from_date = date.fromisoformat(fetch["from_date"])
        to_date = date.fromisoformat(fetch["to_date"])
    except (KeyError, TypeError, ValueError):
        return None
    return session_open_ms_utc(from_date), session_open_ms_utc(to_date)


def build_provider_params(cache_root: Path, provenance: dict[str, Any]) -> dict[str, Any]:
    """Build the ``ProviderParams`` payload marking this row imported-from-cache.

    Two temporal representations of the same fetch history, deliberately:

    * ``fetch_ranges_ms``: first-class, top-level, int64-ms-UTC anchored
      (via the canonical calendar's session-open anchor) fetch ranges --
      the queryable, wire-legal form per .claude/rules/temporal-rigor.md.
    * ``original_provenance``: the *entire* original document embedded
      verbatim, ISO date strings and all. This is a preserved, opaque audit
      document -- the evidence of the refetch leak (#1830), not garbage to
      discard on import -- not a source of temporal values for anything
      downstream. Never mutated; nothing here re-parses its dates for a
      computation. The R21 provenance plan will move this file-side later.
    """
    fetch_ranges_ms: list[dict[str, int]] = []
    for fetch in provenance.get("fetches", []):
        if not isinstance(fetch, dict):
            continue
        anchors = _fetch_range_anchors_ms(fetch)
        if anchors is None:
            continue
        from_ms, to_ms = anchors
        fetch_ranges_ms.append({"from_date_ms": from_ms, "to_date_ms": to_ms})

    return {
        "import_source": "lean_cache",
        "imported_from_cache": True,
        "cache_root": str(cache_root),
        "imported_at_ms": now_ms_utc(),
        "fetch_ranges_ms": fetch_ranges_ms,
        "original_provenance": provenance,
    }


def verify_and_read_zip(zip_path: Path, symbol: str, trading_date: date) -> VerifiedZip:
    """Strictly verify a cache zip and extract row-count / bar-range metadata.

    Unlike the lenient LEAN readers used elsewhere (``app.engine.data.lean_format``,
    ``app.data_lake.ensure_data._read_minute_trade_bars``) — which skip malformed
    rows when reading data this service itself already wrote — this import
    boundary is ingesting a pre-existing, unaudited cache and must fail fast on
    any row it cannot parse rather than silently importing a truncated day.

    Raises ``CorruptCacheZipError`` for: an unreadable file, a zip that fails to
    open or whose member fails to read (encrypted, or a bad-CRC/corrupted
    member), a missing/unexpected CSV member, a malformed row, a negative
    price field (mirrors ``app.data_lake.lean_writer.to_deci_cent``'s refusal
    of negative prices as upstream corruption), a non-strictly-increasing
    timestamp (finite ingestion is fail-fast per
    .claude/rules/temporal-rigor.md -- a duplicate or out-of-order row is a
    signal of upstream corruption, never silently reordered or
    deduplicated), or zero data rows.
    """
    try:
        raw_bytes = zip_path.read_bytes()
    except OSError as exc:
        raise CorruptCacheZipError(f"cannot read {zip_path}: {exc}") from exc

    expected_name = f"{trading_date.strftime('%Y%m%d')}_{symbol.lower()}_minute_trade.csv"
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise CorruptCacheZipError(f"{zip_path}: not a valid zip file: {exc}") from exc
    with zf:
        names = zf.namelist()
        if names != [expected_name]:
            raise CorruptCacheZipError(
                f"{zip_path}: expected exactly one member named {expected_name!r}, found {names!r}"
            )
        try:
            # A bad-CRC member raises zipfile.BadZipFile at read time (distinct
            # from the open-time BadZipFile above, which only catches a
            # corrupt central directory / local header). An encrypted member
            # raises a bare RuntimeError. Both fold into our typed error --
            # neither should abort the whole import uncaught.
            csv_bytes = zf.read(expected_name)
        except (RuntimeError, zipfile.BadZipFile) as exc:
            raise CorruptCacheZipError(f"{zip_path}: cannot read member {expected_name!r}: {exc}") from exc

    try:
        text = csv_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CorruptCacheZipError(f"{zip_path}: CSV member is not ASCII: {exc}") from exc

    midnight_et = datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=_ET)
    first_ms: int | None = None
    last_ms: int | None = None
    prev_ms_since_midnight: int | None = None
    row_count = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 6:
            raise CorruptCacheZipError(f"{zip_path}: line {line_no} has {len(parts)} fields, expected 6: {line!r}")
        try:
            ms_since_midnight = int(parts[0])
            ohlc = [int(p) for p in parts[1:5]]
            int(parts[5])  # volume; parseability only, matching the canonical writer
        except ValueError as exc:
            raise CorruptCacheZipError(f"{zip_path}: line {line_no} has a non-integer field: {line!r}") from exc
        if any(p < 0 for p in ohlc):
            raise CorruptCacheZipError(
                f"{zip_path}: line {line_no} has a negative price field: {line!r} -- refused as "
                f"upstream corruption, mirroring app.data_lake.lean_writer.to_deci_cent's rule."
            )
        if prev_ms_since_midnight is not None and ms_since_midnight <= prev_ms_since_midnight:
            relation = "duplicates" if ms_since_midnight == prev_ms_since_midnight else "is out of order after"
            raise CorruptCacheZipError(
                f"{zip_path}: line {line_no}'s ms_since_midnight={ms_since_midnight} {relation} the "
                f"previous row's {prev_ms_since_midnight} -- finite ingestion requires strictly "
                f"increasing timestamps (.claude/rules/temporal-rigor.md); refusing rather than "
                f"silently reordering or deduplicating."
            )
        prev_ms_since_midnight = ms_since_midnight
        bar_start_ms = to_ms_utc(midnight_et + timedelta(milliseconds=ms_since_midnight))
        if first_ms is None:
            first_ms = bar_start_ms
        last_ms = bar_start_ms
        row_count += 1

    if row_count == 0 or first_ms is None or last_ms is None:
        raise CorruptCacheZipError(f"{zip_path}: CSV member has zero data rows")

    return VerifiedZip(
        row_count=row_count,
        first_bar_start_ms=first_ms,
        last_bar_start_ms=last_ms,
        raw_bytes=raw_bytes,
    )


def decide_claim_outcome(
    claim_result: int | None,
    existing: ArtifactRecord | None,
    content_hash: str,
) -> ClaimDecision:
    """Pure decision: what should the import do after attempting a claim?

    ``claim_result`` is the return value of ``catalog_client.claim_minute_bar``
    (an artifact_id on a fresh claim, ``None`` when a row already exists for
    the identity tuple). ``existing`` is the current complete row for that
    exact identity (already scoped to the correct adjustment mode by the
    caller), or ``None`` when no *complete* row exists (an in-flight or
    previously-failed claim).

    Never overwrites: a pre-existing row with a different content hash is a
    ``conflict`` regardless of how it got there — the caller must not call
    ``complete_artifact`` or ``atomic_write_and_promote`` for it.
    """
    if claim_result is not None:
        return ClaimDecision(action="proceed")
    if existing is None:
        return ClaimDecision(
            action="in_flight_or_incomplete",
            detail="a catalog row exists for this identity but is not complete",
        )
    if existing.file_sha256 == content_hash:
        return ClaimDecision(action="skip_duplicate")
    return ClaimDecision(
        action="conflict",
        detail=(
            f"existing catalog row has file_sha256={existing.file_sha256!r}; "
            f"cache zip hashes to {content_hash!r}. Refusing to overwrite."
        ),
    )


def decide_destination_outcome(
    existing_dest_hash: str | None,
    content_hash: str,
) -> DestinationDecision:
    """Pure decision: what should happen to the on-disk destination file?

    Mirrors ``decide_claim_outcome``'s shape deliberately: the file-level
    guard (two same-mode writers disagreeing about content -- the one
    collision no path segment can prevent) is a "gather I/O results, then
    decide" seam just like the catalog-claim one, so it gets the same pure,
    database-free, CI-executed
    unit tests instead of living only in the two Postgres-gated
    orchestration tests that exercise ``_import_one_zip`` end to end.

    ``existing_dest_hash`` is the sha256 of the bytes currently at the
    destination path, or ``None`` when nothing is there yet.

    Never overwrites: a destination file with a different content hash is a
    ``conflict`` -- the caller must not call ``atomic_write_and_promote``
    (hence ``os.replace``) for it.
    """
    if existing_dest_hash is None:
        return DestinationDecision(action="write")
    if existing_dest_hash == content_hash:
        return DestinationDecision(action="already_present")
    return DestinationDecision(
        action="conflict",
        detail=(
            f"existing destination file has content hash {existing_dest_hash!r}; "
            f"cache zip hashes to {content_hash!r}. Refusing to overwrite."
        ),
    )


def _inspect_destination(dest_path: Path, content_hash: str) -> DestinationDecision:
    """I/O wrapper around ``decide_destination_outcome``: reads the
    destination file (if any) and hashes it, then defers to the pure
    decision. May raise (e.g. a permissions error) -- callers must run this
    inside the same guarded section that handles a write failure, not let it
    escape uncaught and abort the whole import.
    """
    existing_dest_hash = hashlib.sha256(dest_path.read_bytes()).hexdigest() if dest_path.is_file() else None
    return decide_destination_outcome(existing_dest_hash, content_hash)


async def _import_one_zip(
    ref: CacheZipRef,
    price_adjustment_mode: str,
    dch: str,
    provider_params: dict[str, Any],
    lake_dir: Path,
    staging_dir: Path,
    run_id: UUID,
    data_root_id: UUID,
) -> ImportedArtifact | SkippedArtifact | FailedArtifact:
    try:
        verified = verify_and_read_zip(ref.zip_path, ref.symbol, ref.trading_date)
    except CorruptCacheZipError as exc:
        logger.warning("cache_import: %s", exc)
        return FailedArtifact(symbol=ref.symbol, trading_date=ref.trading_date, reason="corrupt_zip", detail=str(exc))

    content_hash = hashlib.sha256(verified.raw_bytes).hexdigest()
    rel_path = LeanMinuteBarPath(
        market="usa", symbol=ref.symbol, trading_date=ref.trading_date, data_type="trade"
    ).relative_path()
    dest_path = lake_dir / Path(*rel_path.parts)
    file_path = str(rel_path)
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol=ref.symbol,
        trading_date=ref.trading_date,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode=price_adjustment_mode,
        # Explicit, not relying on the field's own default (issue #1876):
        # the *selected* root's identity (issue #1878) -- read from that
        # root's own .data-root.json marker, not necessarily the service's
        # configured active root -- is what a reader should see recorded on
        # the row.
        data_root_id=data_root_id,
    )

    claim_result = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
        provider_params=provider_params,
    )
    existing: ArtifactRecord | None = None
    if claim_result is None:
        existing_rows = await catalog_client.select_coverage_minute_bars(
            market="usa",
            symbol=ref.symbol,
            data_type="trade",
            start_trading_date=ref.trading_date,
            end_trading_date=ref.trading_date,
            price_adjustment_mode=price_adjustment_mode,
            data_root_id=data_root_id,
        )
        existing = existing_rows[0] if existing_rows else None

    decision = decide_claim_outcome(claim_result, existing, content_hash)

    if decision.action == "skip_duplicate":
        # The catalog says this identity is already complete with a matching
        # hash -- but nobody has confirmed the *physical* file still agrees.
        # Reuse the same destination guard the fresh-claim path uses so a
        # missing or corrupted on-disk file can't hide behind a catalog row
        # that merely claims to describe it.
        assert existing is not None  # decide_claim_outcome guarantees this for skip_duplicate
        try:
            dest_decision = _inspect_destination(dest_path, content_hash)
            if dest_decision.action == "conflict":
                detail = (
                    f"{dest_path}: {dest_decision.detail} The catalog row (id={existing.id}) "
                    f"already claims this hash."
                )
                # The row no longer honestly describes what's on disk --
                # mark it 'failed' rather than leaving it 'complete' while
                # this run reports a refusal for it.
                await catalog_client.fail_artifact(artifact_id=existing.id, last_error="io_error", error_message=detail)
                logger.error("cache_import: %s %s: %s", ref.symbol, ref.trading_date, detail)
                return FailedArtifact(
                    symbol=ref.symbol, trading_date=ref.trading_date, reason="destination_file_conflict", detail=detail
                )
            if dest_decision.action == "write":
                # Catalog says complete+matching, but the physical file is
                # missing -- restore it from the cache zip we just
                # re-verified matches the catalog's recorded hash.
                atomic_write_and_promote(
                    content=verified.raw_bytes,
                    lake_root=lake_dir,
                    staging_root=staging_dir,
                    rel_lake_path=rel_path,
                    request_id=run_id,
                    worker_id=_WORKER_ID,
                    attempt=1,
                )
                logger.info(
                    "cache_import: restored missing destination file for %s %s from cache zip",
                    ref.symbol,
                    ref.trading_date,
                )
        except Exception as exc:
            await catalog_client.fail_artifact(artifact_id=existing.id, last_error="io_error", error_message=str(exc))
            logger.exception(
                "cache_import: %s %s: destination inspection/restore failed for existing complete row id=%s",
                ref.symbol,
                ref.trading_date,
                existing.id,
            )
            return FailedArtifact(
                symbol=ref.symbol, trading_date=ref.trading_date, reason="write_failed", detail=str(exc)
            )
        return SkippedArtifact(symbol=ref.symbol, trading_date=ref.trading_date, reason="already_imported_same_hash")

    if decision.action == "conflict" or decision.action == "in_flight_or_incomplete":
        logger.error("cache_import: %s %s: %s", ref.symbol, ref.trading_date, decision.detail)
        # Explicit mapping, not decision.action passed straight through: the
        # ClaimDecision vocabulary ("conflict") and the ImportFailureReason
        # contract ("hash_conflict") are deliberately named differently.
        reason: ImportFailureReason = "hash_conflict" if decision.action == "conflict" else "in_flight_or_incomplete"
        return FailedArtifact(
            symbol=ref.symbol,
            trading_date=ref.trading_date,
            reason=reason,
            detail=decision.detail or "",
        )

    # decision.action == "proceed"
    assert claim_result is not None  # narrows type for the type checker

    try:
        # Destination inspection lives inside this guarded section: an
        # unreadable destination (e.g. a permissions error) must not abort
        # the whole import -- the row is already claimed and needs the same
        # fail_artifact treatment as a write failure below, then the import
        # continues with the remaining files.
        dest_decision = _inspect_destination(dest_path, content_hash)

        if dest_decision.action == "conflict":
            detail = (
                f"{dest_path}: {dest_decision.detail} Both writers agree on the adjustment "
                f"mode -- it is a segment of this path -- so they disagree about content: one "
                f"of the two source caches is not what its provenance says it is."
            )
            await catalog_client.fail_artifact(artifact_id=claim_result, last_error="io_error", error_message=detail)
            logger.error("cache_import: %s %s: %s", ref.symbol, ref.trading_date, detail)
            return FailedArtifact(
                symbol=ref.symbol, trading_date=ref.trading_date, reason="destination_file_conflict", detail=detail
            )
        # Identical bytes already at the destination ("already_present"): nothing
        # to write, but the row we just claimed still needs completing, not left
        # in 'fetching'.
        already_present = dest_decision.action == "already_present"

        file_sha = (
            content_hash
            if already_present
            else await write_lease_gated_artifact(
                content=verified.raw_bytes,
                lake_root=lake_dir,
                staging_root=staging_dir,
                rel_lake_path=rel_path,
                request_id=run_id,
                worker_id=_WORKER_ID,
                attempt=1,
                artifact_id=claim_result,
                # This claim was won moments ago in this same call (no
                # steal_or_retry_minute_bar path in this file) -- the
                # fencing generation issue #1888 added is always the
                # freshly-claimed one.
                lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
            )
        )
        await catalog_client.complete_artifact(
            artifact_id=claim_result,
            row_count=verified.row_count,
            first_bar_start_ms=verified.first_bar_start_ms,
            last_bar_start_ms=verified.last_bar_start_ms,
            file_size_bytes=len(verified.raw_bytes),
            file_sha256=file_sha,
            lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
        )
    except ArtifactLeaseLostError as exc:
        # Unlike the branches below, the lease is no longer ours to fail --
        # fail_artifact() has no owner/generation guard (issue #1888 did not
        # extend the fencing check to it; see the PR's residual-risk note),
        # so calling it here would clobber whoever now legitimately holds
        # this row. Just report the loss; do not touch the catalog row.
        logger.warning(
            "cache_import: %s %s: %s",
            ref.symbol,
            ref.trading_date,
            exc,
        )
        return FailedArtifact(
            symbol=ref.symbol, trading_date=ref.trading_date, reason="in_flight_or_incomplete", detail=str(exc)
        )
    except Exception as exc:
        # Caught broadly and deliberately: whatever goes wrong here (disk
        # full, a dropped DB connection, an unreadable destination, an
        # unexpected atomic-write failure), the row has already been claimed
        # and MUST NOT be left in 'fetching' forever -- that would strand it:
        # every later re-run would find claim_minute_bar returns None (a row
        # exists) and select_coverage_minute_bars returns no *complete* row
        # for it, permanently reporting in_flight_or_incomplete with no way
        # for this tool to recover it (see the module docstring's recovery
        # note). Marking it 'failed' at least makes the row visible to
        # catalog_client.steal_or_retry_minute_bar / the sweep.
        await catalog_client.fail_artifact(
            artifact_id=claim_result,
            last_error="io_error",
            error_message=str(exc),
        )
        logger.exception(
            "cache_import: %s %s: claimed artifact_id=%s but failed to write/complete",
            ref.symbol,
            ref.trading_date,
            claim_result,
        )
        return FailedArtifact(
            symbol=ref.symbol, trading_date=ref.trading_date, reason="write_failed", detail=str(exc)
        )

    logger.info("cache_import: imported %s %s (%s)", ref.symbol, ref.trading_date, price_adjustment_mode)
    return ImportedArtifact(
        symbol=ref.symbol,
        trading_date=ref.trading_date,
        price_adjustment_mode=price_adjustment_mode,
        artifact_id=claim_result,
        file_sha256=file_sha,
        row_count=verified.row_count,
    )


def _unrecognized_to_failures(entries: list[UnrecognizedCacheEntry]) -> list[FailedArtifact]:
    """Pure translation: every unrecognized cache-tree entry becomes a
    FailedArtifact with reason="unrecognized_filename" and no trading_date --
    there's no date to report, since the filename itself didn't parse to
    one. The only producer of ``trading_date=None`` in this module.
    """
    return [
        FailedArtifact(symbol=entry.symbol, trading_date=None, reason="unrecognized_filename", detail=entry.detail)
        for entry in entries
    ]


def _fail_all(
    report: ImportReport,
    refs: list[CacheZipRef],
    reason: ImportFailureReason,
    detail: str,
) -> None:
    """Append a FailedArtifact for every ref, sharing one reason/detail --
    the shape both "refuse this whole symbol" call sites in
    import_cache_root need (missing provenance, a lake-root mode conflict)."""
    for ref in refs:
        report.failed.append(FailedArtifact(symbol=ref.symbol, trading_date=ref.trading_date, reason=reason, detail=detail))


async def import_cache_root(cache_root: Path, lake_root: Path) -> ImportReport:
    """Import every trade zip under ``cache_root`` into the lake catalog.

    ``lake_root`` is any physical root carrying a valid, marked
    ``.data-root.json`` (issue #1878, PR B of #1861) -- it no longer needs to
    equal the process's configured write root. The marker is opened and
    validated via ``root_identity.read_marker`` before anything else runs;
    a missing marker or a malformed one both raise ``LakeRootIdentityError``
    with no catalog row and no lake write. This importer never stamps a root
    itself -- an unmarked root (brand-new or an existing-but-unmarked
    canonical root) must go through ``scripts/manage_data_root.py``
    (``init`` or ``stamp --force``) first, the same explicit administrative
    path every other root-identity consumer goes through.

    The marker's ``data_root_id`` is used for every catalog claim and lookup
    this run performs, and every artifact/staging path is resolved relative
    to ``lake_root`` itself (``path_policy.lake_root_within`` /
    ``staging_root_within``), never the configured
    ``settings.LEAN_DATA_WRITE_ROOT``. Because catalog uniqueness now leads
    with ``DataRootId`` (this PR's index rebuild), importing into a second
    physical root with the same symbol/date/artifact identity as a first no
    longer collides with it and no longer risks "phantom coverage" -- each
    root's rows resolve only against that root's own ``data_root_id``.
    Artifacts land under ``lake_root/lake/<price-adjustment-mode>/...``,
    staged through ``lake_root/staging/...``; both are created if missing.
    Makes zero provider calls — every byte written comes from the cache zips
    already on disk.

    Each symbol's adjustment mode comes from its own provenance document and
    selects its own lake root, so one invocation can import a mixture of raw
    and adjusted caches without them ever meeting on disk (#1839).
    """
    marker = root_identity.read_marker(lake_root)
    if marker is None:
        raise LakeRootIdentityError(
            f"no root-identity marker at {root_identity.marker_path(lake_root)}; refusing to import "
            f"into an unmarked root. Run `python -m scripts.manage_data_root init --root-id <uuid> "
            f"--base-root {lake_root}` for a brand-new root, or `... stamp --root-id <uuid> --force "
            f"--base-root {lake_root}` to claim an existing populated root -- this importer never "
            f"stamps a root itself."
        )
    resolved_root_id = marker.data_root_id

    # Both roots are resolved per symbol, below: each is keyed by adjustment
    # mode, and the mode comes from that symbol's own provenance document.
    # Resolved relative to the selected `lake_root`, not the configured
    # write root, so this import lands wherever the operator pointed it.
    await catalog_client.init_pool()

    refs, unrecognized = discover_cache_zips(cache_root)
    if not refs and not unrecognized:
        logger.warning(
            "cache_import: found zero trade zips under %s -- check --cache-root "
            "points at a populated policy directory (nothing will be imported)",
            cache_root,
        )

    by_symbol: dict[str, list[CacheZipRef]] = {}
    for ref in refs:
        by_symbol.setdefault(ref.symbol, []).append(ref)

    report = ImportReport()
    run_id = uuid4()
    for entry in unrecognized:
        logger.warning("cache_import: %s", entry.detail)
    report.failed.extend(_unrecognized_to_failures(unrecognized))

    for symbol in sorted(by_symbol):
        try:
            provenance = load_symbol_provenance(cache_root, symbol)
        except MissingProvenanceError as exc:
            logger.warning("cache_import: %s", exc)
            _fail_all(report, by_symbol[symbol], "missing_provenance", str(exc))
            continue

        # Per-artifact coverage check: a provenance document can validate its
        # own shape and still simply not attest to a particular day. Filter
        # before claiming anything for this symbol, not after.
        covered_refs: list[CacheZipRef] = []
        for ref in by_symbol[symbol]:
            if provenance_covers_date(provenance, ref.trading_date):
                covered_refs.append(ref)
            else:
                detail = (
                    f"no minute-resolution fetch range in {symbol}'s provenance document covers "
                    f"{ref.trading_date.isoformat()}"
                )
                logger.warning("cache_import: %s", detail)
                report.failed.append(
                    FailedArtifact(
                        symbol=symbol,
                        trading_date=ref.trading_date,
                        reason="provenance_coverage_mismatch",
                        detail=detail,
                    )
                )
        if not covered_refs:
            continue

        adjusted = provenance["policy"]["adjusted"]
        price_adjustment_mode = price_adjustment_mode_for(provenance)

        # This symbol's mode selects its own lake root, resolved relative to
        # the selected `lake_root` (issue #1878) rather than the configured
        # write root. Where this used to sit there was a check-then-commit
        # critical section under a cross-process advisory lock, guarding a
        # marker that committed the whole tree to one mode -- because raw
        # and adjusted bytes for the same (symbol, date) resolved to one
        # path and either could overwrite the other. The mode is now a
        # segment of the root itself, so they cannot collide and there is
        # nothing to serialize: two concurrent imports of different modes
        # touch disjoint trees.
        lake_dir = lake_root_within(lake_root, price_adjustment_mode)
        staging_dir = staging_root_within(lake_root, price_adjustment_mode)
        staging_dir.mkdir(parents=True, exist_ok=True)
        lake_dir.mkdir(parents=True, exist_ok=True)
        # An imported-only lake is still a lake LEAN may be pointed at, so it
        # needs the same corporate-action directories the live pipeline
        # creates. See path_policy.
        ensure_lean_readable_layout(lake_dir)

        dch = _import_minute_trade_dch(adjusted)
        provider_params = build_provider_params(cache_root, provenance)

        for ref in covered_refs:
            outcome = await _import_one_zip(
                ref=ref,
                price_adjustment_mode=price_adjustment_mode,
                dch=dch,
                provider_params=provider_params,
                lake_dir=lake_dir,
                staging_dir=staging_dir,
                run_id=run_id,
                data_root_id=resolved_root_id,
            )
            if isinstance(outcome, ImportedArtifact):
                report.imported.append(outcome)
            elif isinstance(outcome, SkippedArtifact):
                report.skipped.append(outcome)
            else:
                report.failed.append(outcome)

    logger.info(
        "cache_import: done. imported=%d skipped=%d failed=%d",
        len(report.imported),
        len(report.skipped),
        len(report.failed),
    )
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=(
            "One-time, idempotent import of an existing lean-cache policy directory "
            "into the lake catalog. Zero provider calls."
        ),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        required=True,
        help="Root of one cache policy directory, e.g. lean-cache/polygon-adjusted",
    )
    parser.add_argument(
        "--lake-root",
        type=Path,
        required=True,
        help=(
            "Write root: any physical root carrying a valid, marked .data-root.json "
            "(see scripts/manage_data_root.py) -- does not need to equal the configured "
            "write root. An unmarked or malformed root refuses (this importer never stamps "
            "one itself). Artifacts land under <lake-root>/lake/<adjustment-mode>, "
            "staged through <lake-root>/staging. Each symbol's adjustment mode comes from its "
            "own provenance document and selects its own subtree, so one invocation can import "
            "a mixture without the modes ever meeting on disk."
        ),
    )
    args = parser.parse_args()

    try:
        report = asyncio.run(
            import_cache_root(cache_root=args.cache_root, lake_root=args.lake_root)
        )
    finally:
        # Closed in its own asyncio.run: the pool is bound to the event loop
        # import_cache_root ran in, which is already closed by the time this
        # line runs. close_pool() is written to tolerate exactly that (falls
        # back to a non-awaited terminate() when the bound loop is gone). A
        # no-op if init_pool() was never reached (e.g. LakeRootIdentityError).
        asyncio.run(catalog_client.close_pool())

    logger.info(
        "Done. imported=%d skipped=%d failed=%d",
        len(report.imported),
        len(report.skipped),
        len(report.failed),
    )
    for failure in report.failed:
        logger.error("FAILED %s %s: %s (%s)", failure.symbol, failure.trading_date, failure.reason, failure.detail)

    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

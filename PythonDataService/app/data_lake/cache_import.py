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
``{"policy": {"adjusted": bool}, "fetches": [...]}``).

``--lake-root`` is the *write root*: artifacts land under
``<lake-root>/lake/...`` (same relative layout as
``app.data_lake.path_policy.LeanMinuteBarPath``), staged through
``<lake-root>/staging/...`` per ``app.data_lake.atomic``. It is a plain CLI
argument, independent of ``settings.LEAN_DATA_WRITE_ROOT`` — this is a one-shot
operational tool, not a request handled by the live ensure_data pipeline.

Idempotency and no-overwrite are decided by ``decide_claim_outcome`` (pure,
unit-tested in isolation): re-running the import re-derives the same content
hash for every zip and finds the prior complete row already claims that
identity, so nothing is re-written or re-inserted. A catalog row whose
existing hash differs from the cache zip's current hash is a refusal
(``FailedArtifact`` with ``reason="hash_conflict"``) — the row and the on-disk
file are left exactly as they were; overwriting them would silently discard
whichever version is later correct.

Schema note: the v1 ``ck_raw_only_for_canonical_data_root`` CHECK constraint
(``Backend/Migrations/20260521033222_AddDataLakeArtifactsAndRuns.cs``)
originally allowed only ``PriceAdjustmentMode = 'raw'`` for every non-metadata
row. Migration
``Backend/Migrations/20260827120000_AllowImportedNonRawAdjustmentModes.cs``
widens it to also allow ``'polygon_split_adjusted'`` — the constraint's own
comment anticipated exactly this ("Relaxed in v2 by adding data_root_id and
dropping this constraint"); this migration takes the minimal step needed for
an honest adjusted-mode import without attempting the full data_root_id
redesign. That migration must be applied before this script's adjusted-mode
path will insert successfully — a 'raw'-only cache (``policy.adjusted=false``)
already works against the unmigrated schema.

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

from app.data_lake import catalog_client
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.path_policy import LeanMinuteBarPath
from app.data_lake.types import ArtifactIdentity, ArtifactRecord
from app.utils.timestamps import now_ms_utc, to_ms_utc

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_WORKER_ID = os.environ.get("HOSTNAME", "cache-import")
_LEASE_TTL_MS = 300_000
_TRADE_ZIP_RE = re.compile(r"^(\d{8})_trade\.zip$")

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
    """A symbol's ``provenance/<symbol>.json`` is absent or malformed.

    Without it the true adjustment mode (``policy.adjusted``) cannot be
    determined, so the import refuses to guess and fails every zip for that
    symbol rather than defaulting to 'raw'.
    """


class CorruptCacheZipError(RuntimeError):
    """A cache zip cannot be verified: unreadable, missing its expected CSV
    member, or containing a malformed row.

    Refused with no catalog row and no lake write — never silently repaired
    or partially imported.
    """


@dataclass(frozen=True)
class CacheZipRef:
    symbol: str
    trading_date: date
    zip_path: Path


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
    "corrupt_zip",
    "hash_conflict",
    "in_flight_or_incomplete",
]


@dataclass(frozen=True)
class FailedArtifact:
    symbol: str
    trading_date: date
    reason: ImportFailureReason
    detail: str


@dataclass(frozen=True)
class ImportReport:
    imported: list[ImportedArtifact] = field(default_factory=list)
    skipped: list[SkippedArtifact] = field(default_factory=list)
    failed: list[FailedArtifact] = field(default_factory=list)


def discover_cache_zips(cache_root: Path) -> list[CacheZipRef]:
    """Find every ``<yyyymmdd>_trade.zip`` under the cache root's minute-bar tree.

    Returns refs sorted by (symbol, trading_date) for deterministic processing
    order. A cache root with no minute-bar tree yet (e.g. a policy directory
    that has never fetched anything) returns an empty list rather than
    raising.
    """
    minute_root = cache_root / "equity" / "usa" / "minute"
    if not minute_root.is_dir():
        return []
    refs: list[CacheZipRef] = []
    for symbol_dir in sorted(p for p in minute_root.iterdir() if p.is_dir()):
        symbol = symbol_dir.name.upper()
        for zip_path in sorted(symbol_dir.glob("*_trade.zip")):
            match = _TRADE_ZIP_RE.match(zip_path.name)
            if match is None:
                continue
            trading_date = datetime.strptime(match.group(1), "%Y%m%d").date()
            refs.append(CacheZipRef(symbol=symbol, trading_date=trading_date, zip_path=zip_path))
    return sorted(refs, key=lambda r: (r.symbol, r.trading_date))


def load_symbol_provenance(cache_root: Path, symbol: str) -> dict[str, Any]:
    """Read and validate ``<cache-root>/provenance/<symbol>.json``.

    Raises ``MissingProvenanceError`` when the file is absent, unparsable, or
    missing the ``policy.adjusted`` boolean that determines the true
    adjustment mode.
    """
    prov_path = cache_root / "provenance" / f"{symbol.lower()}.json"
    if not prov_path.is_file():
        raise MissingProvenanceError(f"no provenance file at {prov_path}")
    try:
        data = json.loads(prov_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MissingProvenanceError(f"cannot parse {prov_path}: {exc}") from exc
    policy = data.get("policy")
    if not isinstance(policy, dict) or not isinstance(policy.get("adjusted"), bool):
        raise MissingProvenanceError(f"{prov_path} is missing a boolean policy.adjusted field")
    return data


def price_adjustment_mode_for(provenance: dict[str, Any]) -> str:
    """Map a provenance file's ``policy.adjusted`` to the catalog's enum value."""
    return "polygon_split_adjusted" if provenance["policy"]["adjusted"] else "raw"


def _import_minute_trade_dch(adjusted: bool) -> str:
    return _dch(
        provider="polygon",
        provider_params=_IMPORT_MINUTE_TRADE_PARAMS_ADJUSTED if adjusted else _IMPORT_MINUTE_TRADE_PARAMS_RAW,
        price_adjustment_mode="polygon_split_adjusted" if adjusted else "raw",
        session_policy="full",
        lean_format_version=1,
    )


def build_provider_params(cache_root: Path, provenance: dict[str, Any]) -> dict[str, Any]:
    """Build the ``ProviderParams`` payload marking this row imported-from-cache.

    Embeds the *entire* original provenance document (including its
    ``fetches`` history) verbatim — that history is the evidence of the
    refetch leak (#1830), not garbage to discard on import.
    """
    return {
        "import_source": "lean_cache",
        "imported_from_cache": True,
        "cache_root": str(cache_root),
        "imported_at_ms": now_ms_utc(),
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
    open, a missing/unexpected CSV member, a malformed row, or zero data rows.
    """
    try:
        raw_bytes = zip_path.read_bytes()
    except OSError as exc:
        raise CorruptCacheZipError(f"cannot read {zip_path}: {exc}") from exc

    expected_name = f"{trading_date.strftime('%Y%m%d')}_{symbol.lower()}_minute_trade.csv"
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names = zf.namelist()
            if names != [expected_name]:
                raise CorruptCacheZipError(
                    f"{zip_path}: expected exactly one member named {expected_name!r}, found {names!r}"
                )
            csv_bytes = zf.read(expected_name)
    except zipfile.BadZipFile as exc:
        raise CorruptCacheZipError(f"{zip_path}: not a valid zip file: {exc}") from exc

    try:
        text = csv_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CorruptCacheZipError(f"{zip_path}: CSV member is not ASCII: {exc}") from exc

    midnight_et = datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=_ET)
    first_ms: int | None = None
    last_ms: int | None = None
    row_count = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 6:
            raise CorruptCacheZipError(f"{zip_path}: line {line_no} has {len(parts)} fields, expected 6: {line!r}")
        try:
            ms_since_midnight = int(parts[0])
            for field_value in parts[1:]:
                int(field_value)
        except ValueError as exc:
            raise CorruptCacheZipError(f"{zip_path}: line {line_no} has a non-integer field: {line!r}") from exc
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


async def _import_one_zip(
    ref: CacheZipRef,
    price_adjustment_mode: str,
    dch: str,
    provider_params: dict[str, Any],
    lake_dir: Path,
    staging_dir: Path,
    run_id: UUID,
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
        )
        existing = existing_rows[0] if existing_rows else None

    decision = decide_claim_outcome(claim_result, existing, content_hash)

    if decision.action == "skip_duplicate":
        return SkippedArtifact(symbol=ref.symbol, trading_date=ref.trading_date, reason="already_imported_same_hash")
    if decision.action in ("conflict", "in_flight_or_incomplete"):
        logger.error("cache_import: %s %s: %s", ref.symbol, ref.trading_date, decision.detail)
        return FailedArtifact(
            symbol=ref.symbol,
            trading_date=ref.trading_date,
            reason=decision.action,  # type: ignore[arg-type]
            detail=decision.detail or "",
        )

    # decision.action == "proceed"
    assert claim_result is not None  # narrows type for the type checker
    file_sha = atomic_write_and_promote(
        content=verified.raw_bytes,
        lake_root=lake_dir,
        staging_root=staging_dir,
        rel_lake_path=rel_path,
        request_id=run_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    await catalog_client.complete_artifact(
        artifact_id=claim_result,
        row_count=verified.row_count,
        first_bar_start_ms=verified.first_bar_start_ms,
        last_bar_start_ms=verified.last_bar_start_ms,
        file_size_bytes=len(verified.raw_bytes),
        file_sha256=file_sha,
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


async def import_cache_root(cache_root: Path, lake_root: Path) -> ImportReport:
    """Import every trade zip under ``cache_root`` into the lake catalog.

    ``lake_root`` is the write root: artifacts land under
    ``lake_root/lake/...``, staged through ``lake_root/staging/...``. Both are
    created if missing. Makes zero provider calls — every byte written comes
    from the cache zips already on disk.
    """
    lake_dir = lake_root / "lake"
    staging_dir = lake_root / "staging"
    lake_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    await catalog_client.init_pool()

    refs = discover_cache_zips(cache_root)
    by_symbol: dict[str, list[CacheZipRef]] = {}
    for ref in refs:
        by_symbol.setdefault(ref.symbol, []).append(ref)

    report = ImportReport()
    run_id = uuid4()

    for symbol in sorted(by_symbol):
        try:
            provenance = load_symbol_provenance(cache_root, symbol)
        except MissingProvenanceError as exc:
            logger.warning("cache_import: %s", exc)
            for ref in by_symbol[symbol]:
                report.failed.append(
                    FailedArtifact(
                        symbol=symbol,
                        trading_date=ref.trading_date,
                        reason="missing_provenance",
                        detail=str(exc),
                    )
                )
            continue

        adjusted = provenance["policy"]["adjusted"]
        price_adjustment_mode = price_adjustment_mode_for(provenance)
        dch = _import_minute_trade_dch(adjusted)
        provider_params = build_provider_params(cache_root, provenance)

        for ref in by_symbol[symbol]:
            outcome = await _import_one_zip(
                ref, price_adjustment_mode, dch, provider_params, lake_dir, staging_dir, run_id
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
        help="Write root: artifacts land under <lake-root>/lake, staged through <lake-root>/staging",
    )
    args = parser.parse_args()

    report = asyncio.run(import_cache_root(cache_root=args.cache_root, lake_root=args.lake_root))

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

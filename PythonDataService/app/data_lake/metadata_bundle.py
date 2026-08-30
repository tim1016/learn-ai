"""LEAN metadata bundle: one on-disk cryptographic receipt binding the
market-hours database, symbol-properties database, and optional
interest-rate file to the ``lean_image_digest`` they were extracted from.

Issue #1879 (PR C of #1861). Before this module, ``lean_image_digest`` was
recorded only in the catalog's ``DataContractHash`` (see
``app.lean_sidecar.lake_mount``'s module docstring, "Lake metadata bytes are
not verified against the pinned LEAN image digest") -- never beside the
files themselves, so a complete catalog row was not proof the bytes on disk
were trustworthy: a wiped or hand-edited file, a root remounted at the wrong
physical volume, or a crash between "files written" and "catalog completed"
were all invisible to a reader that only checked ``Status = 'complete'``.

The receipt closes that gap. One JSON file per ``(root, adjustment-mode)``
directory, at ``<lake_root>/.lean-metadata-receipt.json`` (``lake_root`` is
already ``<base-root>/lake/<mode>``, so this is exactly the location the
issue names). It is the filesystem commit marker for the whole bundle: files
are published first, the receipt last, and nothing downstream (catalog
activation, a LEAN launch) is allowed to treat the bundle as valid without
reading the receipt back and verifying every byte it names.

Owns, in one place, the responsibilities the issue lists: extraction (one
launcher call for the whole bundle, never per file -- see
:func:`_extract_and_publish_bundle`), atomic file publication (delegated to
``app.data_lake.atomic.atomic_write_and_promote``, the existing canonical
stage-then-rename primitive), file hashing, receipt creation and validation
(:func:`verify_bundle`, the single entry point both the writer's reuse check
and the launcher's pre-mount check call), catalog activation
(:func:`_claim_and_complete_metadata_row`), and repair after an incomplete
publication (implicit: any call that finds no valid receipt falls through to
a fresh extract-and-publish, which atomically overwrites whatever partial
state a prior crash left).

Launcher-safety note
---------------------
``app.lean_sidecar.lake_mount`` imports this module (at module scope, the
same precedent ``app.data_lake.path_policy`` already set for it) to verify a
bundle before mounting the lake into the LEAN container -- a standalone host
process with no Postgres reachability. That is safe for the import itself
(``app.config``'s ``Settings()`` only requires ``POLYGON_API_KEY``, resolved
from whatever ``.env``/environment the launcher process happens to have, the
same way ``path_policy``'s existing top-level ``app.config`` import already
is). What matters is that :func:`verify_bundle`, :func:`read_receipt`, and
the plain Pydantic models never *call* anything that opens a Postgres
connection: only :func:`ensure_lean_metadata_bundle` and the
``_claim_and_complete_metadata_row`` / ``_activate_catalog_from_receipt``
helpers behind it touch ``catalog_client`` at call time, and the launcher
never calls those -- it calls :func:`verify_bundle` alone, via
``app.lean_sidecar.lake_mount.verify_lake_metadata_bundle``.

Known trade-off: cross-host duplicate extraction
--------------------------------------------------
The advisory lock guarding the extract-and-publish sequence
(``app.utils.advisory_lock``) is same-host only. Two data-plane hosts racing
a cold cache for the same digest can both extract and both publish -- wasted
launcher work, not a correctness issue: the bytes for a given digest are
deterministic, so both publishes are byte-identical, and
``catalog_client.claim_metadata_artifact``'s unique index still admits only
one winning row per ``(DataRootId, DataContractHash)``; the loser's claim
becomes a read of the winner's row via ``select_complete_metadata_artifact``
on this same call, not a wasted future one. A cross-host lock
(``pg_advisory_lock``) could remove the redundant extraction if it ever
becomes materially expensive; not added here.

This safety argument holds only when the racing hosts are extracting the
*same* digest. A rolling multi-host deploy where two hosts are pinned to
*different* digests and interleave publishes against the same ``(root,
mode)`` lake root is a genuinely different, more severe scenario the above
argument does not cover -- the same-host advisory lock cannot serialize
across hosts at all, so one host's file-and-receipt publish for digest A
can interleave with another host's for digest B. That is a real,
currently-unmitigated gap, not one the above trade-off already accounts
for; closing it needs the cross-host lock described above, scoped as a
follow-up rather than folded into this fix.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from app.config import active_root_id, settings
from app.data_lake import catalog_client
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.lean_metadata import LeanMetadataExtractionError, extract_lean_metadata
from app.data_lake.path_policy import LeanMetadataPath, MetadataKind
from app.data_lake.types import (
    ArtifactFailureReason,
    ArtifactIdentity,
    ArtifactRecord,
    DataRunSpec,
    PriceAdjustmentMode,
)
from app.lean_sidecar.launcher_client import LauncherUnreachable
from app.utils.advisory_lock import try_advisory_file_lock

logger = logging.getLogger(__name__)

RECEIPT_FILENAME = ".lean-metadata-receipt.json"
RECEIPT_SCHEMA_VERSION = 1

# The file name each metadata kind carries into its data_contract_hash --
# moved here from ensure_data.py's old per-kind ``_bootstrap_metadata_artifact``
# (#1879 retires it) so the DCH recipe and the receipt live beside each other.
_METADATA_FILE_NAMES: dict[MetadataKind, str] = {
    "market_hours": "market-hours-database.json",
    "symbol_properties": "symbol-properties-database.csv",
    "interest_rate": "interest-rate.csv",
}

_WORKER_ID = __name__  # distinct lease-owner label from ensure_data._WORKER_ID
_LEASE_TTL_MS = 300_000
_MAX_CLAIM_RETRIES = 3
_LOCK_POLL_INTERVAL_S = 0.05
_LOCK_TIMEOUT_S = 60.0


class MetadataBundleError(RuntimeError):
    """The on-disk receipt (or the files it names) cannot be trusted.

    Covers a missing receipt, a malformed one, one whose identity (root,
    mode, or digest) disagrees with what the caller expects, and one naming
    a file that is absent or hash-mismatched. Raised instead of returning a
    sentinel so a caller cannot forget to check -- exactly the pattern
    ``app.data_lake.root_identity.LakeRootIdentityError`` already
    establishes for the sibling root marker.
    """


class MetadataBundleExtractionFailed(RuntimeError):
    """The launcher could not produce the bundle at all this attempt.

    Distinct from :class:`MetadataBundleError`: that one means "what's on
    disk is not trustworthy, try extracting"; this one means "the attempt to
    extract just failed". The orchestrator (``ensure_lean_metadata_bundle``)
    records this against each metadata kind's catalog row as ``io_error``
    (#1889) -- an auditable, retryable failure, subject to the normal
    per-row retry ceiling -- rather than the pre-#1889 behaviour of
    touching nothing and silently relying on the next call to try again.
    :class:`MetadataBundleLauncherUnreachable` below is the one case that
    gets a different, uncapped classification.
    """


class MetadataBundleLauncherUnreachable(MetadataBundleExtractionFailed):
    """The launcher process itself is not running/reachable (#1889).

    A subclass of :class:`MetadataBundleExtractionFailed` rather than a
    parallel class: everything that only cares "did the bundle extraction
    fail" (a bare ``except MetadataBundleExtractionFailed``) keeps working
    unchanged, while :func:`ensure_lean_metadata_bundle` catches this
    subclass first to give the condition its own transient classification
    (``launcher_unreachable``, distinct from the generic ``io_error``) and
    a message that names the launcher -- reusing
    ``app.lean_sidecar.launcher_client.LauncherUnreachable``, the existing
    typed diagnostic every other launcher call already surfaces this
    condition with, rather than inventing a new error shape.
    """


class MetadataBundleLockTimeout(MetadataBundleError):
    """Waiting for the per-``(root, mode)`` bundle lock exceeded
    ``_LOCK_TIMEOUT_S``.

    A distinct subclass rather than the plain :class:`MetadataBundleError`
    ``_bundle_lock`` used to raise: that base class also covers "what's on
    disk is not trustworthy" (a condition :func:`ensure_lean_metadata_bundle`
    already handles inline, by re-extracting), which is a completely
    different situation from "another caller is still holding the lock".
    ``_LOCK_TIMEOUT_S`` (60s) is shorter than the launcher's documented HTTP
    budget for a legitimate extraction (``EXTRACT_METADATA_HTTP_TIMEOUT_S``,
    360s), so a concurrent caller polling the lock while another caller is
    mid-extraction can time out well before that extraction was ever
    expected to fail -- this must read as ordinary, retryable contention,
    not an unstructured 500.
    """


class MetadataFileEntry(BaseModel):
    """One file's receipt entry: where it lives (root-relative to the
    mode-specific lake root) and its sha256."""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    sha256: str

    @field_validator("file_path")
    @classmethod
    def _reject_traversal(cls, v: str) -> str:
        candidate = PurePosixPath(v)
        if candidate.is_absolute() or not candidate.parts or any(part in ("..", ".", "") for part in candidate.parts):
            raise ValueError(f"file_path must be a non-empty relative path with no '..' segments, got {v!r}")
        return v


class LeanMetadataFiles(BaseModel):
    """The three metadata files a bundle may carry.

    ``interest_rate`` is declared with no default, so a JSON payload that
    omits the key entirely fails Pydantic validation ("Field required")
    rather than silently reading as absent -- that is what distinguishes an
    incomplete or pre-#1859 receipt (missing key: untrusted, forces repair)
    from a bundle that genuinely has no interest-rate data (explicit
    ``null``: trusted, LEAN falls back to its built-in risk-free rate).
    """

    model_config = ConfigDict(extra="forbid")

    market_hours: MetadataFileEntry
    symbol_properties: MetadataFileEntry
    interest_rate: MetadataFileEntry | None

    @model_validator(mode="after")
    def _bind_entries_to_their_canonical_paths(self) -> LeanMetadataFiles:
        """Refuse a receipt whose ``file_path`` for a kind doesn't equal that
        kind's fixed canonical location.

        ``_reject_traversal`` above only rejects an unsafe path shape; it
        never checks that ``market_hours.file_path`` actually names the
        market-hours file rather than, say, the symbol-properties file's
        path (whose real on-disk content a corrupted/hand-edited receipt
        could legitimately hash-match, since :func:`verify_files_on_disk`
        just opens whatever path each entry names). LEAN itself always
        reads the fixed canonical location for each kind, never whatever
        the receipt says -- so a mismatch here means ``verify_bundle`` could
        pass while the file LEAN actually reads is missing or wrong. This
        is exactly the tampering ``MetadataBundleError``'s docstring says
        the receipt exists to catch.
        """
        expected: dict[MetadataKind, MetadataFileEntry | None] = {
            "market_hours": self.market_hours,
            "symbol_properties": self.symbol_properties,
            "interest_rate": self.interest_rate,
        }
        for kind, entry in expected.items():
            if entry is None:
                continue
            canonical = str(LeanMetadataPath(kind=kind).relative_path())
            if entry.file_path != canonical:
                raise ValueError(
                    f"{kind} file_path {entry.file_path!r} does not match its canonical path {canonical!r} "
                    "-- LEAN reads the fixed canonical location, not whatever the receipt names"
                )
        return self


class LeanMetadataReceipt(BaseModel):
    """The exact, closed shape of ``.lean-metadata-receipt.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    data_root_id: UUID
    price_adjustment_mode: PriceAdjustmentMode
    lean_image_digest: str
    files: LeanMetadataFiles


def receipt_path(lake_root: Path) -> Path:
    """The one location a mode's metadata receipt can live at."""
    return lake_root / RECEIPT_FILENAME


def read_receipt(lake_root: Path) -> LeanMetadataReceipt | None:
    """Parse the receipt at ``lake_root``, or ``None`` if it does not exist.

    Raises :class:`MetadataBundleError` for anything on disk that is not a
    well-formed receipt -- malformed JSON, the wrong schema version, or a
    shape Pydantic rejects (including the missing-``interest_rate``-key
    case). A receipt that exists but cannot be trusted must never read the
    same as no receipt at all.
    """
    path = receipt_path(lake_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataBundleError(f"{path} is malformed: not valid JSON ({exc})") from exc
    try:
        receipt = LeanMetadataReceipt.model_validate(raw)
    except ValidationError as exc:
        raise MetadataBundleError(f"{path} is malformed: {exc}") from exc
    if receipt.schema_version != RECEIPT_SCHEMA_VERSION:
        raise MetadataBundleError(
            f"{path} has schema_version={receipt.schema_version}, expected {RECEIPT_SCHEMA_VERSION}"
        )
    return receipt


def verify_receipt_identity(
    receipt: LeanMetadataReceipt,
    *,
    expected_root_id: UUID,
    expected_mode: PriceAdjustmentMode,
    expected_digest: str,
) -> None:
    """Raise unless the receipt's identity matches exactly what was asked for.

    Three independent checks, so the caller's log line names precisely which
    one failed rather than a generic "receipt invalid": a receipt minted for
    another physical root, another adjustment mode, or an older/different
    pinned LEAN image are three different operator stories.
    """
    if receipt.data_root_id != expected_root_id:
        raise MetadataBundleError(
            f"receipt data_root_id={receipt.data_root_id} does not match expected {expected_root_id} "
            "-- refusing a receipt minted for a different physical root"
        )
    if receipt.price_adjustment_mode != expected_mode:
        raise MetadataBundleError(
            f"receipt price_adjustment_mode={receipt.price_adjustment_mode!r} does not match expected "
            f"{expected_mode!r} -- refusing a receipt minted for a different adjustment mode"
        )
    if receipt.lean_image_digest != expected_digest:
        raise MetadataBundleError(
            f"receipt lean_image_digest={receipt.lean_image_digest!r} does not match requested "
            f"{expected_digest!r} -- the pinned LEAN image changed since this bundle was published"
        )


def _verify_one_file(lake_root: Path, kind_name: str, entry: MetadataFileEntry) -> None:
    path = lake_root / Path(*PurePosixPath(entry.file_path).parts)
    if not path.is_file():
        raise MetadataBundleError(f"receipt names {kind_name} at {path} but the file is absent")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != entry.sha256:
        raise MetadataBundleError(
            f"{kind_name} file at {path} does not match its receipt sha256 "
            f"(expected {entry.sha256}, found {actual}) -- tampering or corruption"
        )


def verify_files_on_disk(lake_root: Path, receipt: LeanMetadataReceipt) -> None:
    """Every file the receipt names must exist and hash exactly as recorded.

    The two required files are always checked. ``interest_rate`` is checked
    only when the receipt says it was produced (a non-``null`` entry) --
    ``null`` means "confirmed not produced", which has no file to check.
    """
    _verify_one_file(lake_root, "market_hours", receipt.files.market_hours)
    _verify_one_file(lake_root, "symbol_properties", receipt.files.symbol_properties)
    if receipt.files.interest_rate is not None:
        _verify_one_file(lake_root, "interest_rate", receipt.files.interest_rate)


def verify_bundle(
    lake_root: Path,
    *,
    expected_root_id: UUID,
    expected_mode: PriceAdjustmentMode,
    expected_digest: str,
) -> LeanMetadataReceipt:
    """Read, then fully verify, the metadata bundle at ``lake_root``.

    The single canonical entry point for "can this bundle be trusted as-is":
    both :func:`ensure_lean_metadata_bundle`'s reuse check (the writer side)
    and ``app.lean_sidecar.lake_mount``'s pre-mount check (the launcher side)
    call this rather than re-deriving the verification sequence. Returns the
    verified receipt on success; raises :class:`MetadataBundleError` naming
    the first failure otherwise -- never a partial pass.
    """
    receipt = read_receipt(lake_root)
    if receipt is None:
        raise MetadataBundleError(f"no LEAN metadata receipt at {receipt_path(lake_root)}")
    verify_receipt_identity(
        receipt, expected_root_id=expected_root_id, expected_mode=expected_mode, expected_digest=expected_digest
    )
    verify_files_on_disk(lake_root, receipt)
    return receipt


def metadata_data_contract_hash(lean_image_digest: str, file_name: str, price_adjustment_mode: PriceAdjustmentMode) -> str:
    """LEAN's session calendar and symbol properties, per lake root.

    Moved verbatim from ``ensure_data._metadata_dch`` (#1879 retires the
    per-kind bootstrap it belonged to) -- the recipe itself is unchanged, so
    catalog rows written by the pre-#1879 code remain addressable by the
    same hash. See the original docstring, preserved: the bytes do not vary
    by adjustment mode, but the physical *root* they are copied into does,
    so folding the mode into the hash is what gives each mode's copy its own
    catalog row under ``uq_data_lake_artifacts_metadata``.
    """
    return _dch(
        provider="lean_image_extract",
        provider_params={
            "lean_image_digest": lean_image_digest,
            "file_name": file_name,
            "lake_root_mode": price_adjustment_mode,
        },
        price_adjustment_mode=None,
        session_policy="full",
        lean_format_version=1,
    )


def _publish_and_verify(
    *,
    content: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_path: PurePosixPath,
    request_id: UUID,
) -> str:
    """Atomically publish one file, then re-read it and confirm the hash.

    ``atomic_write_and_promote`` already hashes the content it is about to
    write; re-hashing the file *after* the rename is the "verify published
    hashes" step the issue calls for -- it catches a corrupt rename or a
    concurrent writer clobbering the destination between the promote and
    this read, neither of which the pre-write hash could ever see.
    """
    sha = atomic_write_and_promote(
        content=content,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    published_path = lake_root / Path(*rel_path.parts)
    actual = hashlib.sha256(published_path.read_bytes()).hexdigest()
    if actual != sha:
        raise MetadataBundleError(
            f"published file hash mismatch immediately after publish: {published_path} "
            f"expected {sha}, read back {actual} (concurrent writer or filesystem corruption)"
        )
    return sha


async def _extract_and_publish_bundle(
    spec: DataRunSpec, lake_root: Path, staging_root: Path, root_id: UUID
) -> LeanMetadataReceipt:
    """One launcher call for the whole bundle, then publish files, then the
    receipt -- in that order, so a crash before the receipt write leaves the
    bundle unusable (no receipt = no trust) rather than half-trusted.

    Raises :class:`MetadataBundleExtractionFailed` if the launcher call
    itself fails; nothing is published in that case.
    """
    from app.lean_sidecar.launcher_auth import read_launcher_token

    try:
        mh_bytes, sp_bytes, ir_bytes = await extract_lean_metadata(
            image_digest=spec.lean_image_digest,
            launcher_url=settings.LEAN_LAUNCHER_URL,
            launcher_token=read_launcher_token() or "",
            run_id=f"metadata-{spec.request_id}",
        )
    except LauncherUnreachable as e:
        # Caught before the broader LeanMetadataExtractionError below --
        # LauncherUnreachable is a sibling type, not a subclass of it (see
        # app.data_lake.lean_metadata.extract_lean_metadata's docstring).
        raise MetadataBundleLauncherUnreachable(str(e)) from e
    except LeanMetadataExtractionError as e:
        raise MetadataBundleExtractionFailed(str(e)) from e

    mh_rel = LeanMetadataPath(kind="market_hours").relative_path()
    sp_rel = LeanMetadataPath(kind="symbol_properties").relative_path()
    ir_rel = LeanMetadataPath(kind="interest_rate").relative_path()

    mh_sha = _publish_and_verify(
        content=mh_bytes, lake_root=lake_root, staging_root=staging_root, rel_path=mh_rel, request_id=spec.request_id
    )
    sp_sha = _publish_and_verify(
        content=sp_bytes, lake_root=lake_root, staging_root=staging_root, rel_path=sp_rel, request_id=spec.request_id
    )
    ir_sha: str | None = None
    if ir_bytes is not None:
        ir_sha = _publish_and_verify(
            content=ir_bytes, lake_root=lake_root, staging_root=staging_root, rel_path=ir_rel, request_id=spec.request_id
        )
    else:
        # This digest has no interest-rate data, but an earlier digest that
        # DID publish one may have left it on disk at the same canonical
        # path (interest_rate's location is digest-independent). LEAN opens
        # that canonical path directly -- it does not consult the receipt to
        # decide whether to look (module docstring) -- so a stale file here
        # would be silently read even though this receipt correctly records
        # interest_rate: null. Delete it as part of this same publish
        # sequence, before the receipt is written: an interrupted delete
        # just means the stale file survives and is retried on the next
        # extraction, never worse than today.
        stale_ir_path = lake_root / Path(*ir_rel.parts)
        if stale_ir_path.exists():
            stale_ir_path.unlink()
            logger.info(
                "data_lake.metadata_bundle: removed stale interest-rate file left by an earlier digest",
                extra={"path": str(stale_ir_path), "lean_image_digest": spec.lean_image_digest},
            )

    receipt = LeanMetadataReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        data_root_id=root_id,
        price_adjustment_mode=spec.price_adjustment_mode,
        lean_image_digest=spec.lean_image_digest,
        files=LeanMetadataFiles(
            market_hours=MetadataFileEntry(file_path=str(mh_rel), sha256=mh_sha),
            symbol_properties=MetadataFileEntry(file_path=str(sp_rel), sha256=sp_sha),
            interest_rate=(MetadataFileEntry(file_path=str(ir_rel), sha256=ir_sha) if ir_sha is not None else None),
        ),
    )
    atomic_write_and_promote(
        content=receipt.model_dump_json().encode("utf-8"),
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=PurePosixPath(RECEIPT_FILENAME),
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    return receipt


class MetadataBootstrap(NamedTuple):
    """Outcome of activating one metadata file's catalog row: the row,
    whether it was a cache hit, and -- when there is no row -- the reason.

    Shape preserved from the pre-#1879 ``ensure_data.MetadataBootstrap`` so
    ``ensure_data.ensure_data``'s existing per-kind failure/reuse accounting
    keeps unpacking the first three fields the same way. ``detail`` is new
    (#1889): the human-readable message behind a failure, when the caller
    has one worth surfacing (e.g. naming the unreachable launcher and its
    URL) -- ``None`` for a cache hit, or for a failure whose reason alone
    is already the whole story. Trailing with a default keeps every
    existing 3-positional-argument construction valid.
    """

    record: ArtifactRecord | None
    is_reused: bool
    failure_reason: ArtifactFailureReason | None
    detail: str | None = None


class MetadataBundleOutcome(NamedTuple):
    """The three per-kind outcomes of one ``ensure_lean_metadata_bundle`` call."""

    market_hours: MetadataBootstrap
    symbol_properties: MetadataBootstrap
    interest_rate: MetadataBootstrap


class _MetadataRowClaim(NamedTuple):
    """Result of :func:`_claim_or_reclaim_metadata_row`: exactly one of the
    three fields is non-``None``. ``artifact_id`` set means the caller now
    holds the lease and must complete or fail it; ``existing`` set means a
    concurrent caller already published a 'complete' row for this exact
    digest, nothing to do; ``failure_reason`` set means neither a claim nor
    a reclaim was possible right now.
    """

    artifact_id: int | None
    existing: ArtifactRecord | None
    failure_reason: ArtifactFailureReason | None


async def _claim_or_reclaim_metadata_row(
    *, dch: str, file_path: str, identity: ArtifactIdentity, root_id: UUID
) -> _MetadataRowClaim:
    """Claim (or reclaim) one metadata kind's catalog row by its identity.

    Shared by :func:`_claim_and_complete_metadata_row` (the success path --
    bytes are already published and verified on disk) and
    :func:`_claim_and_fail_metadata_row` (the extraction-failure path --
    there are no bytes yet, but the row's identity is fully determined
    without them). The race against a concurrent claimant, a settled
    'failed' row, or a lease-expired 'fetching' row is identical either
    way; only what the caller does once it holds the lease differs
    (complete vs. fail).

    Reclaiming an existing row passes ``bypass_retry_ceiling=True`` to
    ``catalog_client.steal_or_retry_minute_bar`` whenever the row's last
    recorded failure was ``launcher_unreachable`` (#1889): that specific
    reason is knowably transient (the launcher being down is never something
    more attempts fix faster, and must stay retryable no matter how long the
    outage lasts), so it is exempt from the normal ``AttemptCount`` ceiling
    other failures are still subject to. No caller of this function chooses
    that -- it is derived here, from the row's own recorded ``LastError``.
    """
    artifact_id = await catalog_client.claim_metadata_artifact(
        identity=identity, worker_id=_WORKER_ID, lease_ttl_ms=_LEASE_TTL_MS, data_contract_hash=dch, file_path=file_path
    )
    if artifact_id is not None:
        return _MetadataRowClaim(artifact_id, None, None)

    existing = await catalog_client.select_complete_metadata_artifact(dch, data_root_id=root_id)
    if existing is not None:
        return _MetadataRowClaim(None, existing, None)

    row_state = await catalog_client.select_metadata_claim_state(dch, data_root_id=root_id)
    if row_state is None:
        return _MetadataRowClaim(None, None, "lease_timeout")

    reclaimed = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=row_state.id,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        max_retries=_MAX_CLAIM_RETRIES,
        bypass_retry_ceiling=(row_state.last_error == "launcher_unreachable"),
    )
    if reclaimed:
        return _MetadataRowClaim(row_state.id, None, None)

    # row_state is a pre-reclaim snapshot; a concurrent winner can flip
    # 'failed' -> 'fetching' between it and this check (same race
    # app.data_lake.ensure_data's old bootstrap guarded against). Re-read
    # before deciding rather than trusting the stale snapshot.
    current = await catalog_client.select_metadata_claim_state(dch, data_root_id=root_id)
    if current is not None and current.status == "failed":
        return _MetadataRowClaim(None, None, "fetch_timeout")
    return _MetadataRowClaim(None, None, "lease_timeout")


def _metadata_row_dch_and_identity(spec: DataRunSpec, kind: MetadataKind, root_id: UUID) -> tuple[str, ArtifactIdentity]:
    """The DataContractHash and ArtifactIdentity for one metadata kind's
    catalog row.

    Fully determined by ``(spec.lean_image_digest, kind,
    spec.price_adjustment_mode, root_id)`` alone, independent of whether
    extraction ever succeeds -- shared by both
    :func:`_claim_and_complete_metadata_row` (the success path) and
    :func:`_claim_and_fail_metadata_row` (the extraction-failure path, #1889)
    so the two don't each re-derive the same identity.
    """
    dch = metadata_data_contract_hash(spec.lean_image_digest, _METADATA_FILE_NAMES[kind], spec.price_adjustment_mode)
    identity = ArtifactIdentity(
        artifact_kind="metadata",
        market=spec.market,
        symbol=None,
        provider="lean_image_extract",
        price_adjustment_mode=spec.price_adjustment_mode,
        data_root_id=root_id,
    )
    return dch, identity


async def _claim_and_complete_metadata_row(
    *, spec: DataRunSpec, kind: MetadataKind, entry: MetadataFileEntry, lake_root: Path, root_id: UUID
) -> MetadataBootstrap:
    """Claim (or adopt an existing) catalog row for one metadata file whose
    bytes are already published and verified on disk -- via a fresh
    extraction moments ago, or via a receipt that was already there.

    Never talks to the launcher: extraction, when needed, already happened
    once for the whole bundle before this is called. Mirrors the reclaim
    dance the pre-#1879 per-kind bootstrap used (a settled 'failed' or
    lease-expired 'fetching' row is not contention and must be reclaimed
    rather than reported as a perpetual lease_timeout), because that race is
    a property of the catalog primitives, not of how many launcher calls
    preceded it.
    """
    dch, identity = _metadata_row_dch_and_identity(spec, kind, root_id)
    file_path = entry.file_path
    published = lake_root / Path(*PurePosixPath(file_path).parts)
    file_size_bytes = published.stat().st_size

    claim = await _claim_or_reclaim_metadata_row(dch=dch, file_path=file_path, identity=identity, root_id=root_id)
    if claim.existing is not None:
        await catalog_client.mark_metadata_artifacts_stale_for_path(
            data_root_id=root_id,
            price_adjustment_mode=spec.price_adjustment_mode,
            file_path=file_path,
            keep_artifact_id=claim.existing.id,
        )
        return MetadataBootstrap(claim.existing, True, None)
    if claim.artifact_id is None:
        return MetadataBootstrap(None, False, claim.failure_reason)
    artifact_id = claim.artifact_id

    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=1,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=file_size_bytes,
        file_sha256=entry.sha256,
    )
    await catalog_client.mark_metadata_artifacts_stale_for_path(
        data_root_id=root_id,
        price_adjustment_mode=spec.price_adjustment_mode,
        file_path=file_path,
        keep_artifact_id=artifact_id,
    )
    return MetadataBootstrap(
        ArtifactRecord(
            id=artifact_id,
            artifact_kind="metadata",
            market=spec.market,
            symbol=None,
            trading_date=None,
            resolution=None,
            data_type=None,
            provider="lean_image_extract",
            price_adjustment_mode=spec.price_adjustment_mode,
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=entry.sha256,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=0,
            file_size_bytes=file_size_bytes,
            data_root_id=root_id,
        ),
        False,
        None,
    )


async def _activate_catalog_from_receipt(
    spec: DataRunSpec, lake_root: Path, receipt: LeanMetadataReceipt, root_id: UUID
) -> MetadataBundleOutcome:
    mh = await _claim_and_complete_metadata_row(
        spec=spec, kind="market_hours", entry=receipt.files.market_hours, lake_root=lake_root, root_id=root_id
    )
    sp = await _claim_and_complete_metadata_row(
        spec=spec, kind="symbol_properties", entry=receipt.files.symbol_properties, lake_root=lake_root, root_id=root_id
    )
    if receipt.files.interest_rate is None:
        # No row is claimed or completed for this digest -- there is no
        # interest-rate DCH to claim it under. But a PRIOR digest that did
        # publish interest-rate data may still have a 'complete' row
        # claiming the same physical (root, mode, canonical path); left
        # alone it would keep reading as a valid catalog row for a file
        # this receipt just deleted (or never had). Stale it directly by
        # path -- there is no "keeper" row to exclude in this branch, so
        # ``keep_artifact_id`` is omitted (mark_metadata_artifacts_stale_
        # for_path treats that as "no keeper, stale everything complete
        # for this path").
        await catalog_client.mark_metadata_artifacts_stale_for_path(
            data_root_id=root_id,
            price_adjustment_mode=spec.price_adjustment_mode,
            file_path=str(LeanMetadataPath(kind="interest_rate").relative_path()),
        )
        ir = MetadataBootstrap(None, False, "provider_no_data")
    else:
        ir = await _claim_and_complete_metadata_row(
            spec=spec, kind="interest_rate", entry=receipt.files.interest_rate, lake_root=lake_root, root_id=root_id
        )
    return MetadataBundleOutcome(mh, sp, ir)


async def _claim_and_fail_metadata_row(
    *,
    spec: DataRunSpec,
    kind: MetadataKind,
    root_id: UUID,
    reason: ArtifactFailureReason,
    detail: str,
    bundle_verifies: bool,
) -> MetadataBootstrap:
    """Record this attempt's extraction failure against one metadata kind's
    catalog row (#1889).

    Called when the whole-bundle extraction call failed before any bytes
    were ever produced. Unlike :func:`_claim_and_complete_metadata_row`
    (the success path), there is no :class:`MetadataFileEntry` yet -- but a
    kind's ``DataContractHash`` and canonical ``FilePath`` are fully
    determined by ``(spec.lean_image_digest, kind,
    spec.price_adjustment_mode)`` alone, independent of whether extraction
    ever succeeds, so the row can be claimed (or reclaimed) here exactly
    like the success path claims it.

    Recording the failure against a real, claimed row -- rather than the
    pre-#1889 behaviour of touching nothing on extraction failure -- is
    what makes a launcher outage an auditable, retryable artifact like
    every other kind: it shows up in coverage/observatory reads instead of
    being invisible, and the very next call's
    ``_claim_and_complete_metadata_row`` reclaims *this exact row* once
    extraction succeeds, instead of nothing ever having tracked the
    failure in the first place.
    """
    dch, identity = _metadata_row_dch_and_identity(spec, kind, root_id)
    file_path = str(LeanMetadataPath(kind=kind).relative_path())
    claim = await _claim_or_reclaim_metadata_row(dch=dch, file_path=file_path, identity=identity, root_id=root_id)
    if claim.existing is not None:
        if not bundle_verifies:
            # The row says 'complete', but this call only got here because the
            # bundle it describes failed verification and the repair
            # extraction then failed too. Adopting the row would report the
            # metadata as available on the strength of a catalog row alone,
            # while the bytes it names are missing or tampered -- and
            # ``ensure_data`` would return success, letting the run proceed to
            # a LEAN mount that cannot verify. Surface the extraction failure
            # instead. The row is left 'complete' rather than invalidated
            # here: it is not this caller's row to transition, and the next
            # successful extraction re-activates it through
            # ``_activate_catalog_from_receipt``'s adopt-and-restale path.
            return MetadataBootstrap(None, False, reason, detail)
        # A concurrent caller already published and completed this exact
        # kind moments ago, and the bundle on disk verifies right now -- this
        # attempt's failure is moot.
        return MetadataBootstrap(claim.existing, True, None)
    if claim.artifact_id is None:
        return MetadataBootstrap(None, False, claim.failure_reason, detail)
    await catalog_client.fail_artifact(claim.artifact_id, reason, detail)
    return MetadataBootstrap(None, False, reason, detail)


def _bundle_still_verifies(spec: DataRunSpec, lake_root: Path, root_id: UUID) -> bool:
    """Does the bundle on disk verify *right now*?

    Asked once on the extraction-failure path, to decide whether a
    pre-existing 'complete' catalog row may be adopted as a usable artifact.
    The answer is normally False there -- a verification failure is what sent
    this call to extraction in the first place -- but a concurrent winner can
    legitimately have republished in between, and that case must still be
    reusable.
    """
    try:
        verify_bundle(
            lake_root,
            expected_root_id=root_id,
            expected_mode=spec.price_adjustment_mode,
            expected_digest=spec.lean_image_digest,
        )
    except MetadataBundleError:
        return False
    return True


async def _fail_required_metadata_rows(
    spec: DataRunSpec, root_id: UUID, *, lake_root: Path, reason: ArtifactFailureReason, detail: str
) -> MetadataBundleOutcome:
    """Record one failed extraction attempt against all three metadata
    kinds (#1889) -- the bundle is one launcher call for all three, so a
    failure to produce it is a failure for all three alike.

    Whether a pre-existing 'complete' row may be adopted despite this failure
    is decided once, from the bytes on disk, and applied to all three kinds
    alike -- ``verify_bundle`` is a whole-bundle check, so a rejection
    implicates every kind it covers."""
    bundle_verifies = _bundle_still_verifies(spec, lake_root, root_id)
    mh = await _claim_and_fail_metadata_row(
        spec=spec, kind="market_hours", root_id=root_id, reason=reason, detail=detail, bundle_verifies=bundle_verifies
    )
    sp = await _claim_and_fail_metadata_row(
        spec=spec,
        kind="symbol_properties",
        root_id=root_id,
        reason=reason,
        detail=detail,
        bundle_verifies=bundle_verifies,
    )
    ir = await _claim_and_fail_metadata_row(
        spec=spec, kind="interest_rate", root_id=root_id, reason=reason, detail=detail, bundle_verifies=bundle_verifies
    )
    return MetadataBundleOutcome(mh, sp, ir)


@asynccontextmanager
async def _bundle_lock(lake_root: Path) -> AsyncIterator[None]:
    """Serialize the whole ensure-bundle sequence per ``(root, mode)``.

    Keyed by the receipt path -- the natural "one lock per mode's lake
    root" key, matching the receipt's own one-per-``(root, mode)`` scope.

    Polls the *non-blocking* advisory lock and yields the event loop
    (``asyncio.sleep``) between attempts, rather than calling the blocking
    variant directly. ``app.utils.advisory_lock.advisory_file_lock`` blocks
    the calling OS thread on ``flock()``; called directly from a coroutine,
    a second concurrent caller *on the same event loop* would freeze that
    loop inside the blocking syscall while the first caller's coroutine sits
    suspended on an ``await`` (e.g. the launcher HTTP call) and can never run
    to release the lock -- a same-thread deadlock, not merely a slow
    acquire. Polling avoids that: every attempt is instantaneous, and a
    suspended holder gets scheduled time to finish and release.
    """
    target = receipt_path(lake_root)
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        with try_advisory_file_lock(target) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() > deadline:
            raise MetadataBundleLockTimeout(
                f"timed out waiting {_LOCK_TIMEOUT_S}s for the metadata-bundle lock at {target}"
            )
        await asyncio.sleep(_LOCK_POLL_INTERVAL_S)


async def ensure_lean_metadata_bundle(spec: DataRunSpec, lake_root: Path, staging_root: Path) -> MetadataBundleOutcome:
    """Phase-0 entry point: verify-or-extract the whole metadata bundle once,
    then activate its catalog rows.

    Replaces the pre-#1879 pattern of three independent
    ``_bootstrap_metadata_artifact`` calls (one launcher round trip per
    file). Under one cross-process lock, per ``(data_root_id,
    price_adjustment_mode)``:

    1. Re-check whether an already-published receipt satisfies the
       requested ``spec.lean_image_digest`` *and* every file it names still
       hashes correctly ("never trust catalog completion alone" -- a
       complete catalog row is not consulted here at all; only the receipt
       and the bytes it names are).
    2. On any verification failure (missing receipt, wrong root/mode/digest,
       missing or tampered file -- including the "receipt exists but a
       crash happened before it was ever published" case, which reads
       identically to "no receipt"), extract the bundle fresh, publish every
       file atomically, then publish the receipt last.
    3. Either way, activate (claim + complete, or adopt-and-restale) the
       catalog row for each of the three kinds from the now-verified
       receipt's own recorded hashes -- no re-hashing, no second read of the
       bytes beyond what verification already did.
    """
    root_id = active_root_id()

    try:
        async with _bundle_lock(lake_root):
            try:
                receipt = verify_bundle(
                    lake_root,
                    expected_root_id=root_id,
                    expected_mode=spec.price_adjustment_mode,
                    expected_digest=spec.lean_image_digest,
                )
            except MetadataBundleError as e:
                logger.info(
                    "data_lake.metadata_bundle: bundle cache miss, (re-)extracting",
                    extra={"lake_root": str(lake_root), "reason": str(e)},
                )
                try:
                    receipt = await _extract_and_publish_bundle(spec, lake_root, staging_root, root_id)
                except MetadataBundleLauncherUnreachable as e2:
                    # Caught before the broader MetadataBundleExtractionFailed
                    # below (it's a subclass of it): the launcher being down
                    # is transient, so this is classified and recorded as
                    # ``launcher_unreachable`` -- infinitely retryable,
                    # distinct from a genuine extraction failure -- rather
                    # than the generic ``io_error`` (#1889).
                    logger.warning(
                        "data_lake.metadata_bundle: LEAN launcher unreachable during metadata extraction",
                        extra={"lake_root": str(lake_root), "error": str(e2)},
                    )
                    return await _fail_required_metadata_rows(
                        spec, root_id, lake_root=lake_root, reason="launcher_unreachable", detail=str(e2)
                    )
                except MetadataBundleExtractionFailed as e2:
                    logger.warning(
                        "data_lake.metadata_bundle: bundle extraction failed",
                        extra={"lake_root": str(lake_root), "error": str(e2)},
                    )
                    return await _fail_required_metadata_rows(
                        spec, root_id, lake_root=lake_root, reason="io_error", detail=str(e2)
                    )

            return await _activate_catalog_from_receipt(spec, lake_root, receipt, root_id)
    except MetadataBundleLockTimeout as e3:
        # Raised by _bundle_lock itself, before its `async with` body ever
        # runs -- structurally distinct from the `except MetadataBundleError`
        # above, which only wraps `verify_bundle`'s call once the lock is
        # already held. A caller polling the lock while another caller is
        # mid-extraction (up to EXTRACT_METADATA_HTTP_TIMEOUT_S = 360s, well
        # past this lock's own 60s budget) is ordinary, retryable
        # contention, not a bundle that failed to verify or extract -- it
        # must surface the same way the module's other "still contended"
        # outcome does, not as an unstructured 500.
        logger.warning(
            "data_lake.metadata_bundle: timed out waiting for the bundle lock",
            extra={"lake_root": str(lake_root), "error": str(e3)},
        )
        failure = MetadataBootstrap(None, False, "lease_timeout")
        return MetadataBundleOutcome(failure, failure, failure)

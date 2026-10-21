"""Evidence-bound retirement of superseded option zips (#3432).

Only a reviewed plan can delete: exact legacy bytes must remain in an admitted
session container and a verified, restore-tested backup. A journal permits
retry after a crash between unlink and catalog commit. Retained proofs and
Clerk/money stores are outside the catalog query and the typed path policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.data_lake import catalog_client
from app.data_lake.admission import read_committed_bytes
from app.data_lake.maintenance import lake_activity
from app.data_lake.path_policy import OptionListingPath, OptionMinuteBarPath
from app.data_lake.root_identity import read_marker
from app.data_lake.types import ArtifactRecord
from app.options.contract import OptionContract
from app.research.runs.hashing import canonical_json, hash_payload
from app.utils.atomic_file import atomic_write_bytes, fsync_parent_dir
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

if TYPE_CHECKING:
    from app.option_archive.backup import BackupManifest

ROLLBACK_INTERVAL_MS = 7 * 24 * 60 * 60 * 1000
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_Ms = Annotated[int, Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)]
_Model = ConfigDict(frozen=True, extra="forbid")


class RetirementRefusedError(RuntimeError):
    """A retirement gate changed or lacks reviewable evidence."""


class RetirementSource(BaseModel):
    model_config = _Model

    artifact_id: Annotated[int, Field(strict=True, ge=1)]
    underlying: str
    symbol: str
    session_ms: _Ms
    file_path: str
    sha256: _Digest
    size_bytes: Annotated[int, Field(strict=True, ge=0)]
    recipe: _Digest
    container_path: str
    container_sha256: _Digest
    listing_path: str
    listing_sha256: _Digest


class RetirementPlan(BaseModel):
    model_config = _Model

    version: Literal[1] = 1
    root_id: UUID
    created_at_ms: _Ms
    rollback_until_ms: _Ms
    generation_id: str
    manifest_sha256: _Digest
    sources: tuple[RetirementSource, ...]

    @property
    def digest(self) -> str:
        return hash_payload(self.model_dump(mode="json"))


class RetirementApproval(BaseModel):
    """The operator records the owner's approval of this exact dry run."""

    model_config = _Model

    plan_sha256: _Digest
    issue_comment_url: Annotated[str, Field(
        pattern=r"^https://github\.com/tim1016/learn-ai/issues/3432#issuecomment-[0-9]+$",
    )]
    approved_at_ms: _Ms
    compatibility_revision: str = Field(min_length=1)
    saved_run_pins_verified: Literal[True]
    retained_study_inputs_verified: Literal[True]
    golden_proofs_verified: Literal[True]
    offline_golden_results_verified: Literal[True]


async def legacy_sources(root_id: UUID) -> tuple[ArtifactRecord, ...]:
    """Only complete raw option-contract bars; no other store is selectable."""
    async with catalog_client.connection() as connection:
        rows = await connection.fetch('''
            SELECT "Id" AS id, "ArtifactKind" AS artifact_kind, "Market" AS market,
                "Symbol" AS symbol, "TradingDate" AS trading_date, "Resolution" AS resolution,
                "DataType" AS data_type, "Provider" AS provider,
                "PriceAdjustmentMode" AS price_adjustment_mode, "DataContractHash" AS data_contract_hash,
                "FilePath" AS file_path, "FileSha256" AS file_sha256, "FileSizeBytes" AS file_size_bytes,
                "RowCount" AS row_count, "FirstBarStartMs" AS first_bar_start_ms,
                "LastBarStartMs" AS last_bar_start_ms, "DataRootId" AS data_root_id
              FROM "DataLakeArtifacts"
             WHERE "DataRootId"=$1 AND "Market"='usa_options' AND "Status"='complete'
               AND "ArtifactKind"='time_series_bars' AND "Resolution"='minute'
               AND "DataType"='trade' AND "PriceAdjustmentMode"='raw' AND "Provider"='polygon'
             ORDER BY "Id"
        ''', root_id)
    return tuple(ArtifactRecord(**dict(row)) for row in rows if "/minute/" in row["file_path"])


def _confined(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or path.is_symlink():
        raise RetirementRefusedError("retirement path is not an ordinary root-relative artifact")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RetirementRefusedError("retirement path escaped the lake root")
    return path


def _root_id(mode_root: Path) -> UUID:
    marker = read_marker(mode_root.parent.parent)
    if mode_root.name != "raw" or mode_root.parent.name != "lake" or marker is None:
        raise RetirementRefusedError("retirement requires a marked canonical raw lake root")
    return marker.data_root_id


def _restore_evidence(generation: Path, receipt_path: Path, *, now_ms: int) -> BackupManifest:
    from app.option_archive.backup import manifest_digest, verify_generation

    manifest = verify_generation(generation)
    receipt = json.loads(receipt_path.read_bytes())
    checked = receipt.get("checked_at_ms")
    if (receipt.get("passed") is not True or receipt.get("generation_id") != manifest.generation_id
            or receipt.get("manifest_sha256") != manifest_digest(generation)
            or type(checked) is not int or not manifest.completed_ms <= checked <= now_ms
            or now_ms - checked > ROLLBACK_INTERVAL_MS):
        raise RetirementRefusedError("the exact backup generation needs a successful restore test within seven days")
    return manifest


def plan_retirement(
    *, mode_root: Path, sources: Sequence[ArtifactRecord], generation: Path,
    restore_receipt: Path, now_ms: int,
) -> RetirementPlan:
    """Dry run: normal readers prove exact compatibility for every selected source.

    Call from a worker thread, as normal committed-byte admission requires.
    A missing or insufficient replacement refuses the whole proposed plan.
    """
    from app.data_lake.option_bars import read_option_day_bars
    from app.data_lake.option_session import read_option_session
    from app.data_lake.path_policy import OptionSessionPath

    root_id = _root_id(mode_root)
    manifest = _restore_evidence(generation, restore_receipt, now_ms=now_ms)
    backed_up = {(item.root_id, item.relative_path): item for item in manifest.files}
    planned = []
    seen: set[int] = set()
    with lake_activity(mode_root):
        for row in sources:
            if (row.id in seen or row.data_root_id != root_id or row.market != "usa_options"
                    or row.artifact_kind != "time_series_bars" or row.provider != "polygon"
                    or row.resolution != "minute" or row.data_type != "trade"
                    or row.price_adjustment_mode != "raw" or row.trading_date is None or row.symbol is None):
                raise RetirementRefusedError("source is duplicate or outside the raw option-contract catalog scope")
            seen.add(row.id)
            contract = OptionContract.from_occ_symbol(row.symbol)
            parts = Path(row.file_path).parts
            if len(parts) != 6:
                raise RetirementRefusedError("source is outside the legacy option minute layout")
            underlying = parts[3].upper()
            canonical = str(OptionMinuteBarPath(underlying, row.symbol, row.trading_date).relative_path())
            if row.file_path != canonical:
                raise RetirementRefusedError("catalog path differs from its typed option-contract identity")
            payload = read_committed_bytes(_confined(mode_root, row.file_path))
            if hashlib.sha256(payload).hexdigest() != row.file_sha256 or len(payload) != row.file_size_bytes:
                raise RetirementRefusedError("legacy source changed after catalog selection")
            session = read_option_session(underlying, row.trading_date, lake_root=mode_root)
            if session is None or session.original_bytes.get(row.symbol) != payload:
                raise RetirementRefusedError("session container does not retain these exact original legacy bytes")
            normal = read_option_day_bars(contract, row.trading_date, lake_root=mode_root, listed_under=underlying)
            if normal is None or normal.sha256 != row.file_sha256:
                raise RetirementRefusedError("the normal reader cannot satisfy the original saved byte pin")
            backup = backed_up.get((str(root_id), "lake/raw/" + row.file_path))
            if (backup is None or backup.sha256 != row.file_sha256 or backup.size_bytes != len(payload)
                    or row.id not in backup.artifact_ids):
                raise RetirementRefusedError("verified backup lacks the original bytes and catalog receipt")
            planned.append(RetirementSource(
                artifact_id=row.id, underlying=underlying, symbol=row.symbol,
                session_ms=session.session_open_ms, file_path=row.file_path,
                sha256=row.file_sha256, size_bytes=len(payload), recipe=row.data_contract_hash,
                container_path=str(OptionSessionPath(underlying, row.trading_date).relative_path()),
                container_sha256=session.sha256,
                listing_path=str(OptionListingPath(underlying, row.trading_date).relative_path()),
                listing_sha256=session.listing_sha256,
            ))
    return RetirementPlan(
        root_id=root_id, created_at_ms=now_ms, rollback_until_ms=now_ms + ROLLBACK_INTERVAL_MS,
        generation_id=manifest.generation_id, manifest_sha256=hashlib.sha256((generation / "manifest.json").read_bytes()).hexdigest(),
        sources=tuple(sorted(planned, key=lambda item: item.artifact_id)),
    )


async def apply_retirement(
    *, plan: RetirementPlan, approval: RetirementApproval, mode_root: Path, generation: Path,
    restore_receipt: Path, journal: Path, now_ms: int,
) -> int:
    """Apply exactly the approved plan, under root exclusion and catalog row locks."""
    from app.option_archive.backup import BackupRefusedError, backup_destination_activity, manifest_digest

    started = monotonic()

    def require_rollback_window() -> None:
        current_ms = now_ms + int((monotonic() - started) * 1000)
        if not plan.created_at_ms <= approval.approved_at_ms <= current_ms < plan.rollback_until_ms:
            raise RetirementRefusedError("the approved bounded rollback interval expired")

    if _root_id(mode_root) != plan.root_id or approval.plan_sha256 != plan.digest:
        raise RetirementRefusedError("approval or root does not match this plan")
    require_rollback_window()
    try:
        # Pruning cannot select/remove this generation between verification,
        # pin publication and the last destructive catalog transaction.
        with backup_destination_activity(generation.parent.parent):
            manifest = await asyncio.to_thread(_restore_evidence, generation, restore_receipt, now_ms=now_ms)
            if manifest.generation_id != plan.generation_id or manifest_digest(generation) != plan.manifest_sha256:
                raise RetirementRefusedError("the approved restore point changed")
            require_rollback_window()
            evidence = {"plan_sha256": plan.digest, "approval": approval.model_dump(mode="json")}
            with lake_activity(mode_root, exclusive=True):
                if journal.exists() and json.loads(journal.read_bytes()) != evidence:
                    raise RetirementRefusedError("retirement journal belongs to another approved plan")
                atomic_write_bytes(journal, canonical_json(evidence).encode())
                pin = generation.parent.parent / "retention-pins" / f"{plan.generation_id}.json"
                atomic_write_bytes(pin, canonical_json({
                    "manifest_sha256": plan.manifest_sha256, "retain_until_ms": plan.rollback_until_ms,
                }).encode())
                for source in plan.sources:
                    await _retire_source(plan, source, mode_root, require_rollback_window)
    except BackupRefusedError as exc:
        raise RetirementRefusedError(str(exc)) from exc
    return len(plan.sources)


async def _retire_source(plan: RetirementPlan, source: RetirementSource, root: Path,
                         require_rollback_window: Callable[[], None]) -> None:
    from app.data_lake.path_policy import OptionSessionPath

    day = et_date_at_ms(source.session_ms)
    if (source.file_path != str(OptionMinuteBarPath(source.underlying, source.symbol, day).relative_path())
            or source.container_path != str(OptionSessionPath(source.underlying, day).relative_path())
            or source.listing_path != str(OptionListingPath(source.underlying, day).relative_path())):
        raise RetirementRefusedError("approved plan includes a path outside the typed option layouts")
    # Root exclusion prevents normal admission or publication while this
    # bounded transaction checks receipts and removes just one original.
    async with catalog_client.connection() as connection, connection.transaction():
        row = await connection.fetchrow('SELECT * FROM "DataLakeArtifacts" WHERE "Id"=$1 FOR UPDATE', source.artifact_id)
        if row is not None and (
            str(row["DataRootId"]) != str(plan.root_id) or row["Status"] != "complete"
            or row["ArtifactKind"] != "time_series_bars" or row["Market"] != "usa_options"
            or row["Symbol"] != source.symbol or row["FilePath"] != source.file_path
            or row["FileSha256"] != source.sha256 or row["FileSizeBytes"] != source.size_bytes
            or row["DataContractHash"] != source.recipe or row["PriceAdjustmentMode"] != "raw"
            or row["TradingDate"] != day or row["Resolution"] != "minute"
            or row["DataType"] != "trade" or row["Provider"] != "polygon"
        ):
            raise RetirementRefusedError("a source was refreshed or reclaimed after the approved dry run")
        for relative, digest in ((source.container_path, source.container_sha256),
                                 (source.listing_path, source.listing_sha256)):
            path = _confined(root, relative)
            payload = await asyncio.to_thread(path.read_bytes)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise RetirementRefusedError("the approved session or listing changed")
            admitted = await connection.fetchval('''SELECT EXISTS(SELECT 1 FROM "DataLakeArtifacts"
                WHERE "DataRootId"=$1 AND "PriceAdjustmentMode"='raw' AND "Status"='complete'
                  AND "FilePath"=$2 AND "FileSha256"=$3 AND "FileSizeBytes"=$4)''',
                plan.root_id, relative, digest, len(payload))
            if not admitted:
                raise RetirementRefusedError("the approved replacement no longer has a committed receipt")
        path = _confined(root, source.file_path)
        if path.exists():
            payload = await asyncio.to_thread(path.read_bytes)
            if hashlib.sha256(payload).hexdigest() != source.sha256 or len(payload) != source.size_bytes:
                raise RetirementRefusedError("legacy source bytes changed after approval")
            require_rollback_window()
            await asyncio.to_thread(path.unlink)
            await asyncio.to_thread(fsync_parent_dir, path)
        if row is not None:
            require_rollback_window()
            await connection.execute('DELETE FROM "DataLakeArtifacts" WHERE "Id"=$1', source.artifact_id)

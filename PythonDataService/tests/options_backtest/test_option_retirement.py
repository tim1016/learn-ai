"""Retirement plans preserve exact originals and refuse incomplete gate evidence."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import respx

import app.data_lake.option_retirement as retirement
from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.maintenance import LakeMaintenanceBusyError, lake_activity
from app.data_lake.option_retirement import (
    ROLLBACK_INTERVAL_MS,
    RetirementApproval,
    RetirementPlan,
    RetirementRefusedError,
    RetirementSource,
    apply_retirement,
    legacy_sources,
    plan_retirement,
)
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionSessionPath
from app.data_lake.root_identity import read_marker
from app.data_lake.types import ArtifactRecord
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.option_archive.backup import (
    CatalogSnapshot,
    RestoreReceipt,
    backup_destination_activity,
    create_generation,
    manifest_digest,
    read_pin,
)
from app.option_archive.settings import STATE_FILE_NAME
from app.option_archive.state import ArchiveState, ArchiveStateDoc, JobReceipt
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import FRIDAY, raw_root, store_listing, store_option_day, vendor_listing_row
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

CALL = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
UNKNOWN = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=501)


def record_restore_test(lake: Path, receipt: RestoreReceipt | None) -> None:
    """What the archive's restore check records for the lake it preserves."""
    def save(state: ArchiveStateDoc) -> None:
        state.jobs["restore"] = JobReceipt(receipt=receipt)
    ArchiveState(lake / STATE_FILE_NAME).update(save)


def complete_records(fake: FakeCatalog) -> list[ArtifactRecord]:
    return [fake._record(row) for row in fake.rows.values() if row["status"] == "complete"]


async def prepare(fake: FakeCatalog, lake: Path, destination: Path) -> tuple[RestoreReceipt, tuple[ArtifactRecord, ...]]:
    destination.mkdir()
    await store_listing([vendor_listing_row(CALL), vendor_listing_row(UNKNOWN)])
    captured = await store_option_day(CALL, [])
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    root_id = str(read_marker(lake).data_root_id)
    records = complete_records(fake)
    inventory = CatalogSnapshot(schema_hash="test", row_count=len(records), migration_ids=[], files=[{
        "Id": row.id, "DataRootId": str(row.data_root_id), "PriceAdjustmentMode": row.price_adjustment_mode,
        "FilePath": row.file_path, "FileSha256": row.file_sha256, "FileSizeBytes": row.file_size_bytes,
        "ArtifactKind": row.artifact_kind, "Symbol": row.symbol,
        "TradingDate": None if row.trading_date is None else session_open_ms_utc(row.trading_date),
        "Resolution": row.resolution, "DataType": row.data_type, "Market": row.market,
    } for row in records])

    async def snapshot(path: Path) -> CatalogSnapshot:
        path.write_bytes(b"FakeCatalog receipt snapshot")
        return inventory

    manifest = await create_generation(destination=destination, roots={root_id: lake}, clock=lambda: 1000,
                                       should_pause=lambda: False, snapshot=snapshot)
    receipt = RestoreReceipt(generation_id=manifest.generation_id,
        manifest_sha256=manifest_digest(destination / "generations" / manifest.generation_id),
        checked_at_ms=1100, passed=True, decoded_counts={"legacy": 1, "session": 1})
    record_restore_test(lake, receipt)
    return receipt, (captured.record,)


def approval(digest: str) -> RetirementApproval:
    return RetirementApproval(plan_sha256=digest,
        issue_comment_url="https://github.com/tim1016/learn-ai/issues/3432#issuecomment-1234", approved_at_ms=1250,
        compatibility_revision="verified-test-revision", saved_run_pins_verified=True, retained_study_inputs_verified=True,
        golden_proofs_verified=True, offline_golden_results_verified=True)


async def a_plan(lake: Path, destination: Path, rows: tuple[ArtifactRecord, ...]) -> RetirementPlan:
    return await asyncio.to_thread(plan_retirement, mode_root=raw_root(lake), sources=rows,
                                   destination=destination, now_ms=1200)


def without_catalog(monkeypatch: pytest.MonkeyPatch, *, unlink_then_crash: bool = False) -> list[RetirementSource]:
    """Stand in for the per-source catalog transaction, which needs Postgres; record what it was asked to retire."""
    retired: list[RetirementSource] = []

    async def retire(plan: RetirementPlan, source: RetirementSource, root: Path, require_window: object) -> None:
        retired.append(source)
        if unlink_then_crash:
            (root / source.file_path).unlink()
            raise RuntimeError("simulated crash between unlink and catalog commit")

    monkeypatch.setattr(retirement, "_retire_source", retire)
    return retired


@respx.mock
async def test_dry_run_binds_the_archive_restore_receipt_and_preserves_original_bytes(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    before = dict(fake_catalog.rows)
    original = raw_root(tmp_lake) / rows[0].file_path
    payload = original.read_bytes()
    plan = await a_plan(tmp_lake, tmp_path / "backup", rows)
    assert plan.restore == receipt
    assert plan.sources[0].sha256 == rows[0].file_sha256
    assert plan.sources[0].artifact_id == rows[0].id
    assert plan.sources[0].container_sha256 != rows[0].file_sha256
    assert original.read_bytes() == payload
    assert fake_catalog.rows == before


@pytest.mark.parametrize(("damage", "refusal", "message"), [
    ("no_restore_test", RetirementRefusedError, "no restore test"),
    ("restore_failed", RetirementRefusedError, "successful restore test within seven days"),
    ("wrong_manifest", RetirementRefusedError, "successful restore test within seven days"),
    ("stale_restore", RetirementRefusedError, "successful restore test within seven days"),
    ("missing_generation", RetirementRefusedError, "verification failed"),
    ("source_changed", LakeAdmissionError, "no committed catalog receipt"),
    ("wrong_market", RetirementRefusedError, "not a legacy option-contract file"),
    ("session_container", RetirementRefusedError, "not a legacy option-contract file"),
])
@respx.mock
async def test_dry_run_refuses_unproven_recovery(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, damage: str, refusal: type[Exception], message: str,
) -> None:
    receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    now = 1200
    if damage == "no_restore_test":
        record_restore_test(tmp_lake, None)
    elif damage == "restore_failed":
        record_restore_test(tmp_lake, receipt.model_copy(update={"passed": False}))
    elif damage == "wrong_manifest":
        record_restore_test(tmp_lake, receipt.model_copy(update={"manifest_sha256": "f" * 64}))
    elif damage == "stale_restore":
        now += 8 * 24 * 60 * 60 * 1000
    elif damage == "missing_generation":
        record_restore_test(tmp_lake, receipt.model_copy(update={"generation_id": "another-generation"}))
    elif damage == "source_changed":
        (raw_root(tmp_lake) / rows[0].file_path).write_bytes(b"changed")
    elif damage == "wrong_market":
        rows = (rows[0].model_copy(update={"market": "usa"}),)
    elif damage == "session_container":
        container = str(OptionSessionPath("SPY", FRIDAY).relative_path())
        rows = tuple(row for row in complete_records(fake_catalog) if row.file_path == container)
        assert len(rows) == 1
    with pytest.raises(refusal, match=message):
        await asyncio.to_thread(plan_retirement, mode_root=raw_root(tmp_lake), sources=rows,
                                destination=tmp_path / "backup", now_ms=now)


@respx.mock
async def test_only_legacy_contract_files_are_selectable_whatever_the_catalog_query_returns(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await store_listing([vendor_listing_row(CALL), vendor_listing_row(UNKNOWN)])
    legacy = (await store_option_day(CALL, [])).record
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    # Every complete row of the root, the session container and listing among them: the filter alone decides.
    every_row = [record.model_dump() for record in complete_records(fake_catalog)]
    assert len(every_row) == 3

    class Everything:
        async def fetch(self, query: str, *args: object) -> list[dict]:
            return every_row

    @asynccontextmanager
    async def connection():
        yield Everything()

    monkeypatch.setattr(catalog_client, "connection", connection)
    assert [record.id for record in await legacy_sources(raw_root(tmp_lake))] == [legacy.id]


@pytest.mark.parametrize("activity", ["lake_reader", "running_backup"])
@respx.mock
async def test_apply_requires_exact_approval_and_excludes_active_readers_and_backups(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, activity: str,
) -> None:
    _, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await a_plan(tmp_lake, tmp_path / "backup", rows)
    args = dict(plan=plan, mode_root=raw_root(tmp_lake), destination=tmp_path / "backup",
                journal=tmp_path / "journal.json", now_ms=1300)
    with pytest.raises(RetirementRefusedError, match="approval"):
        await apply_retirement(approval=approval("0" * 64), **args)
    if activity == "lake_reader":
        with lake_activity(raw_root(tmp_lake)), pytest.raises(LakeMaintenanceBusyError):
            await apply_retirement(approval=approval(plan.digest), **args)
    else:
        # A backup holds the destination shared while it copies; retirement needs it alone.
        with backup_destination_activity(tmp_path / "backup", shared=True), \
                pytest.raises(RetirementRefusedError, match="retry later"):
            await apply_retirement(approval=approval(plan.digest), **args)
    assert (raw_root(tmp_lake) / rows[0].file_path).exists()
    assert not args["journal"].exists()


@respx.mock
async def test_apply_rechecks_the_apply_window_after_backup_verification(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await a_plan(tmp_lake, tmp_path / "backup", rows)
    plan = plan.model_copy(update={"apply_until_ms": 1301})
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(retirement, "monotonic", lambda: next(ticks))
    with pytest.raises(RetirementRefusedError, match="expired"):
        await apply_retirement(plan=plan, approval=approval(plan.digest), mode_root=raw_root(tmp_lake),
            destination=tmp_path / "backup", journal=tmp_path / "journal.json", now_ms=1300)
    assert (raw_root(tmp_lake) / rows[0].file_path).exists()
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("pin_data", [[], {"manifest_sha256": "wrong", "retain_until_ms": 9999},
                                     {"manifest_sha256": None, "retain_until_ms": True}])
@respx.mock
async def test_apply_refuses_existing_pin_with_unverifiable_identity_or_deadline(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, pin_data: object,
) -> None:
    _, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await a_plan(tmp_lake, tmp_path / "backup", rows)
    pin = tmp_path / "backup" / "retention-pins" / f"{plan.restore.generation_id}.json"
    pin.parent.mkdir()
    if isinstance(pin_data, dict) and pin_data["manifest_sha256"] is None:
        pin_data = {**pin_data, "manifest_sha256": plan.restore.manifest_sha256}
    pin.write_text(json.dumps(pin_data))
    with pytest.raises(RetirementRefusedError, match="rollback pin"):
        await apply_retirement(plan=plan, approval=approval(plan.digest), mode_root=raw_root(tmp_lake),
            destination=tmp_path / "backup", journal=tmp_path / "journal.json", now_ms=1300)
    assert (raw_root(tmp_lake) / rows[0].file_path).exists()
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("existing", ["none", "longer"])
@respx.mock
async def test_apply_pins_the_restore_point_a_rollback_interval_past_the_last_possible_deletion(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str,
) -> None:
    from app.option_archive.backup import extend_pin

    _, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await a_plan(tmp_lake, tmp_path / "backup", rows)
    longer = plan.apply_until_ms + ROLLBACK_INTERVAL_MS + 10_000
    if existing == "longer":
        # Another approved plan resting on the same restore point already pinned it longer.
        extend_pin(tmp_path / "backup", plan.restore.generation_id, longer,
                   manifest_sha256=plan.restore.manifest_sha256)
    retired = without_catalog(monkeypatch)
    assert await apply_retirement(plan=plan, approval=approval(plan.digest), mode_root=raw_root(tmp_lake),
        destination=tmp_path / "backup", journal=tmp_path / "journal.json", now_ms=1300) == 1
    assert retired == list(plan.sources)
    pin = read_pin(tmp_path / "backup", plan.restore.generation_id)
    assert pin.retain_until_ms == (longer if existing == "longer" else plan.apply_until_ms + ROLLBACK_INTERVAL_MS)


@respx.mock
async def test_a_missing_source_is_accepted_only_on_a_retry_with_the_plans_own_journal(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await a_plan(tmp_lake, tmp_path / "backup", rows)
    args = dict(plan=plan, approval=approval(plan.digest), mode_root=raw_root(tmp_lake),
                destination=tmp_path / "backup", now_ms=1300)
    without_catalog(monkeypatch, unlink_then_crash=True)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await apply_retirement(journal=tmp_path / "journal.json", **args)
    assert not (raw_root(tmp_lake) / rows[0].file_path).exists()
    retired = without_catalog(monkeypatch)
    # A first apply, which is what a fresh journal is, refuses a source that is already gone.
    with pytest.raises(RetirementRefusedError, match="missing"):
        await apply_retirement(journal=tmp_path / "another-journal.json", **args)
    assert retired == [] and not (tmp_path / "another-journal.json").exists()
    assert await apply_retirement(journal=tmp_path / "journal.json", **args) == 1
    assert retired == list(plan.sources)


"""Retirement plans preserve exact originals and refuse incomplete gate evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import respx

from app.data_lake.maintenance import LakeMaintenanceBusyError, lake_activity
from app.data_lake.option_retirement import (
    RetirementApproval,
    RetirementRefusedError,
    apply_retirement,
    plan_retirement,
)
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.root_identity import read_marker
from app.data_lake.types import ArtifactRecord
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.option_archive.backup import CatalogSnapshot, RestoreReceipt, create_generation, manifest_digest
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import FRIDAY, raw_root, store_listing, store_option_day, vendor_listing_row
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

CALL = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
UNKNOWN = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=501)


async def prepare(fake: FakeCatalog, lake: Path, output: Path) -> tuple[Path, Path, tuple[ArtifactRecord, ...]]:
    output.mkdir()
    await store_listing([vendor_listing_row(CALL), vendor_listing_row(UNKNOWN)])
    captured = await store_option_day(CALL, [])
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    root_id = str(read_marker(lake).data_root_id)
    records = [fake._record(row) for row in fake.rows.values() if row["status"] == "complete"]
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

    manifest = await create_generation(destination=output, roots={root_id: lake}, clock=lambda: 1000,
                                       should_pause=lambda: False, snapshot=snapshot)
    generation = output / "generations" / manifest.generation_id
    receipt = output / "restore-receipt.json"
    receipt.write_text(RestoreReceipt(generation_id=manifest.generation_id, manifest_sha256=manifest_digest(generation),
        checked_at_ms=1100, passed=True, decoded_counts={"legacy": 1, "session": 1}).model_dump_json())
    return generation, receipt, (captured.record,)


def approval(digest: str) -> RetirementApproval:
    return RetirementApproval(plan_sha256=digest,
        issue_comment_url="https://github.com/tim1016/learn-ai/issues/3432#issuecomment-1234", approved_at_ms=1250,
        compatibility_revision="verified-test-revision", saved_run_pins_verified=True, retained_study_inputs_verified=True,
        golden_proofs_verified=True, offline_golden_results_verified=True)


@respx.mock
async def test_dry_run_preserves_partial_coverage_and_original_bytes(fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path) -> None:
    generation, receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    before = dict(fake_catalog.rows)
    original = raw_root(tmp_lake) / rows[0].file_path
    payload = original.read_bytes()
    plan = await asyncio.to_thread(plan_retirement, mode_root=raw_root(tmp_lake), sources=rows,
                                  generation=generation, restore_receipt=receipt, now_ms=1200)
    assert plan.sources[0].sha256 == rows[0].file_sha256
    assert plan.sources[0].artifact_id == rows[0].id
    assert plan.sources[0].container_sha256 != rows[0].file_sha256
    assert original.read_bytes() == payload
    assert fake_catalog.rows == before


@pytest.mark.parametrize("damage", ["restore_failed", "wrong_generation", "stale_restore", "source_changed", "wrong_market"])
@respx.mock
async def test_dry_run_refuses_unproven_recovery(fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, damage: str) -> None:
    generation, receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    evidence = json.loads(receipt.read_bytes())
    now = 1200
    if damage == "restore_failed":
        evidence["passed"] = False
    elif damage == "wrong_generation":
        evidence["generation_id"] = "another-generation"
    elif damage == "stale_restore":
        now += 8 * 24 * 60 * 60 * 1000
    elif damage == "source_changed":
        (raw_root(tmp_lake) / rows[0].file_path).write_bytes(b"changed")
    elif damage == "wrong_market":
        rows = (rows[0].model_copy(update={"market": "usa"}),)
    receipt.write_text(json.dumps(evidence))
    with pytest.raises((RetirementRefusedError, RuntimeError)):
        await asyncio.to_thread(plan_retirement, mode_root=raw_root(tmp_lake), sources=rows,
                                generation=generation, restore_receipt=receipt, now_ms=now)


@respx.mock
async def test_apply_requires_exact_approval_and_excludes_active_readers(fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path) -> None:
    generation, receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await asyncio.to_thread(plan_retirement, mode_root=raw_root(tmp_lake), sources=rows,
                                  generation=generation, restore_receipt=receipt, now_ms=1200)
    args = dict(plan=plan, mode_root=raw_root(tmp_lake), generation=generation, restore_receipt=receipt,
                journal=tmp_path / "journal.json", now_ms=1300)
    with pytest.raises(RetirementRefusedError, match="approval"):
        await apply_retirement(approval=approval("0" * 64), **args)
    with lake_activity(raw_root(tmp_lake)), pytest.raises(LakeMaintenanceBusyError):
        await apply_retirement(approval=approval(plan.digest), **args)
    assert (raw_root(tmp_lake) / rows[0].file_path).exists()
    assert not args["journal"].exists()


@respx.mock
async def test_apply_rechecks_rollback_deadline_after_backup_verification(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.data_lake.option_retirement as retirement

    generation, receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await asyncio.to_thread(plan_retirement, mode_root=raw_root(tmp_lake), sources=rows,
                                  generation=generation, restore_receipt=receipt, now_ms=1200)
    plan = plan.model_copy(update={"rollback_until_ms": 1301})
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(retirement, "monotonic", lambda: next(ticks))
    with pytest.raises(RetirementRefusedError, match="expired"):
        await apply_retirement(plan=plan, approval=approval(plan.digest), mode_root=raw_root(tmp_lake),
            generation=generation, restore_receipt=receipt, journal=tmp_path / "journal.json", now_ms=1300)
    assert (raw_root(tmp_lake) / rows[0].file_path).exists()
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("pin_data", [[], {"manifest_sha256": "wrong", "retain_until_ms": 9999},
                                     {"manifest_sha256": None, "retain_until_ms": True}])
@respx.mock
async def test_apply_refuses_existing_pin_with_unverifiable_identity_or_deadline(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, pin_data: object,
) -> None:
    generation, receipt, rows = await prepare(fake_catalog, tmp_lake, tmp_path / "backup")
    plan = await asyncio.to_thread(plan_retirement, mode_root=raw_root(tmp_lake), sources=rows,
                                  generation=generation, restore_receipt=receipt, now_ms=1200)
    pin = generation.parent.parent / "retention-pins" / f"{plan.generation_id}.json"
    pin.parent.mkdir()
    if isinstance(pin_data, dict) and pin_data["manifest_sha256"] is None:
        pin_data = {**pin_data, "manifest_sha256": plan.manifest_sha256}
    pin.write_text(json.dumps(pin_data))
    with pytest.raises(RetirementRefusedError, match="rollback pin"):
        await apply_retirement(plan=plan, approval=approval(plan.digest), mode_root=raw_root(tmp_lake),
            generation=generation, restore_receipt=receipt, journal=tmp_path / "journal.json", now_ms=1300)
    assert (raw_root(tmp_lake) / rows[0].file_path).exists()
    assert not (tmp_path / "journal.json").exists()

"""Offline evidence verification uses normal readers and exact replay on recorded bytes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.data_lake.option_session_repack import repack_option_session
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import run_sessions
from app.options_backtest.run_wire import result_to_wire
from app.options_bots.golden_proof import build_proof, proof_sha256
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search.dispatch import pins_to_wire
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.metrics import evidence_to_json
from app.research.options_golden_search.planning import freeze_protocol
from app.research.options_golden_search.projections import exact_data
from app.research.options_golden_search.protocol import Knob, Window
from app.research.options_golden_search.snapshots import SnapshotRunData, capture_snapshot
from app.research.options_golden_search.stages import StudyEvaluator, metric_summary
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import et_midnight_ms
from scripts.verify_retained_option_evidence import verify_export
from tests.options_backtest import test_golden_spy_2025_06_10 as recording
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


async def retained_bundle(tmp_path: Path, *, container: bool) -> tuple[dict[str, Any], BlobStore]:
    blobs = BlobStore(tmp_path / "blobs")
    with recorded_vendor(recording.RECORDING, recording.RECORDED_FILES, recording.STOCK_PATH):
        await recording.capture_the_recording()
    if container:
        assert (await repack_option_session("SPY", recording.TRADING_DATE)).failure is None
    setup = recording.a_setup()
    source = await asyncio.to_thread(run_sessions, setup, [recording.TRADING_DATE], LakeRunData("SPY"),
                                    slippage_ladder=(), cash_ladder=())
    proof = await asyncio.to_thread(build_proof, source, blobs=blobs)
    protocol = freeze_protocol(setup, (Knob(path="legs.leg.stop_loss", values=(None, {"kind": "percent", "value": "10"})),),
        start_ms=et_midnight_ms(date(2025, 1, 1)), end_ms=et_midnight_ms(date(2025, 7, 1)),
        train_months=1, test_months=1, final_months=1)
    snapshot = await asyncio.to_thread(capture_snapshot, protocol, blobs)
    assert snapshot.version == (2 if container else 1)
    assert any("/session/" in path for path in snapshot.manifest) is container
    protocol = protocol.model_copy(update={"snapshot_id": snapshot.snapshot_id})
    row = {"id": "retained-study", "protocol": protocol.model_dump(mode="json"),
           "protocol_hash": protocol.protocol_hash, "snapshot": snapshot.model_dump(mode="json")}
    window = Window(key="final", start_ms=source.sessions[0].session_open_ms,
        end_ms=source.sessions[0].session_close_ms + 1, session_ms=(source.sessions[0].session_open_ms,),
        annualization_sessions=protocol.final.annualization_sessions, activity_floor=1)
    evaluator = StudyEvaluator(row, lambda: None, lambda *_: None, blobs=blobs)
    result = await asyncio.to_thread(evaluate_setup, setup, window, SnapshotRunData("SPY", snapshot, blobs))
    payload = {"sessions": [evidence_to_json(session) for session in result.sessions],
               "pins": pins_to_wire(result.pins), "input_failures": result.input_failures}
    key, identity = evaluator.key(setup, window, "base")
    evaluation = {"key": key, "identity": identity, "result": {
        "evidence_blob": blobs.put(json.dumps(payload).encode()),
        "metrics_sha256": hash_payload(exact_data(result.metrics)), "metrics": metric_summary(result.metrics)}}
    return {"version": 1, "studies": [{"document": row, "evaluations": [evaluation]}],
            "proofs": [{"id": "approved-proof", "source_run_id": 1, "proof": proof, "proof_sha256": proof_sha256(proof)}],
            "sources": {"1": result_to_wire(source)}}, blobs


@pytest.mark.parametrize("container", [False, True])
async def test_actual_replay_checks_exported_study_and_proof_without_mutating_sources(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, container: bool,
) -> None:
    bundle, blobs = await retained_bundle(tmp_path, container=container)
    original = copy.deepcopy(bundle)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in blobs.root.rglob("*") if path.is_file()}
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        report = await asyncio.to_thread(verify_export, bundle, blobs)
    assert report["passed"]
    assert report["studies"][0]["evaluations_replayed"] == 1
    assert report["studies"][0]["selection_reconstruction"] == "not_performed"
    assert report["proofs"][0]["sessions_replayed"] == 1
    assert bundle == original
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in blobs.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("damage", ["container", "trace", "unfinished", "source"])
async def test_changed_or_incomplete_evidence_fails_the_receipt_with_a_reason(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, damage: str,
) -> None:
    bundle, blobs = await retained_bundle(tmp_path, container=True)
    if damage == "container":
        manifest = bundle["proofs"][0]["proof"]["manifest"]
        digest = next(value for path, value in manifest.items() if "/session/" in path)
        (blobs.root / digest[:2] / digest).write_bytes(b"changed container")
    elif damage == "trace":
        bundle["studies"][0]["evaluations"][0]["result"]["metrics_sha256"] = "0" * 64
    elif damage == "unfinished":
        bundle["studies"][0]["evaluations"][0]["result"] = None
    else:
        bundle["sources"]["1"]["setup"]["starting_cash_usd"] = "20000"
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        report = await asyncio.to_thread(verify_export, bundle, blobs)
    assert not report["passed"]
    failed = [item for kind in ("studies", "proofs") for item in report[kind] if item["result"] == "failed"]
    assert failed and all(item["reason"] for item in failed)

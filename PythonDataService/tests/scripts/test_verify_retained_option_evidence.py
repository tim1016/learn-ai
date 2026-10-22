"""Offline evidence verification uses normal readers and exact replay on recorded bytes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
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
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.planning import freeze_protocol
from app.research.options_golden_search.protocol import Knob, Window
from app.research.options_golden_search.snapshots import SnapshotRunData, capture_snapshot
from app.research.options_golden_search.stages import StudyEvaluator, evaluation_record, evidence_payload
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import et_midnight_ms
from scripts.verify_retained_option_evidence import main, verify_export
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
    payload = evidence_payload(result)
    key, identity = evaluator.key(setup, window, "base")
    evaluation = {"key": key, "identity": identity,
                  "result": evaluation_record(result, blobs.put(payload), len(payload), [])}
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


@pytest.mark.parametrize(("damage", "failing", "reason"), [
    ("container", ("studies", "proofs"), "is corrupt"),
    ("trace", ("studies",), "no longer reproduces its recorded qualification metrics"),
    ("unfinished", ("studies",), "unfinished evaluation cannot be verified"),
    ("incomplete_flag", ("studies",), "does not reproduce its exact retained evidence"),
    ("foreign_study", ("studies",), "not bound to the exported study"),
    ("unknown_identity_field", ("studies",), "not bound to the exported study"),
    ("source", ("proofs",), "no longer matches the approved proof"),
])
async def test_changed_or_incomplete_evidence_fails_only_the_damaged_record_with_a_reason(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, damage: str, failing: tuple[str, ...], reason: str,
) -> None:
    bundle, blobs = await retained_bundle(tmp_path, container=True)
    evaluation = bundle["studies"][0]["evaluations"][0]
    if damage == "container":
        manifest = bundle["proofs"][0]["proof"]["manifest"]
        digest = next(value for path, value in manifest.items() if "/session/" in path)
        (blobs.root / digest[:2] / digest).write_bytes(b"changed container")
    elif damage == "trace":
        evaluation["result"]["metrics_sha256"] = "0" * 64
    elif damage == "unfinished":
        evaluation["result"] = None
    elif damage == "incomplete_flag":
        evaluation["result"]["complete"] = False
    elif damage in ("foreign_study", "unknown_identity_field"):
        # The key is re-derived over the altered identity: only the identity's shape can refuse it.
        evaluation["identity"].update({"protocol": "f" * 64} if damage == "foreign_study" else {"unreviewed": 1})
        evaluation["key"] = hash_payload(evaluation["identity"])
    else:
        bundle["sources"]["1"]["setup"]["starting_cash_usd"] = "20000"
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        report = await asyncio.to_thread(verify_export, bundle, blobs)
    assert not report["passed"]
    for kind in ("studies", "proofs"):
        (item,) = report[kind]
        assert item["result"] == ("failed" if kind in failing else "passed")
        assert kind not in failing or reason in item["reason"]


async def test_main_refuses_unsafe_report_paths_and_writes_the_receipt(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = await retained_bundle(tmp_path, container=False)
    bundle_path, taken = tmp_path / "bundle.json", tmp_path / "taken.json"
    payload = json.dumps(bundle).encode()
    bundle_path.write_bytes(payload)
    taken.write_text("{}")

    def run(report: Path) -> int:
        monkeypatch.setattr(sys, "argv", ["verify", "--bundle", str(bundle_path), "--blob-root", str(blobs.root),
                                          "--report", str(report)])
        try:
            main()
        except SystemExit as stop:
            return int(stop.code)
        return 0

    inside_blobs = blobs.root / "receipt.json"
    for unsafe in (bundle_path, inside_blobs, taken):
        assert await asyncio.to_thread(run, unsafe) == 2
    assert not inside_blobs.exists() and taken.read_text() == "{}" and bundle_path.read_bytes() == payload

    receipt = tmp_path / "receipt.json"
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        assert await asyncio.to_thread(run, receipt) == 0
    written = json.loads(receipt.read_text())
    assert written["passed"] and written["bundle_sha256"] == hashlib.sha256(payload).hexdigest()

    bundle["studies"][0]["evaluations"][0]["result"] = None
    bundle_path.write_text(json.dumps(bundle))
    failing = tmp_path / "failing.json"
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        assert await asyncio.to_thread(run, failing) == 1
    refused = json.loads(failing.read_text())
    assert not refused["passed"] and refused["studies"][0]["result"] == "failed"

"""Replay an exported Options evidence inventory without a database or provider (#3422).

Run with ``--bundle EXPORT.json --blob-root /retained/blobs --report RECEIPT.json``.
The version-1 export contains ``studies`` (each original ``document`` plus its
``evaluations``), ``proofs`` (id, source_run_id, proof and proof_sha256), and
``sources`` keyed by source run id, encoded by ``run_wire.result_to_wire``.
Obtain those records together in a read-only database snapshot. This tool
has no database export, migration, provider, repair or publication path.

Every study evaluation is replayed from SnapshotRunData and compared exactly
with its retained trace, pins and recomputed metrics. Every Golden Options
proof uses the normal reprove function. Study selection/gate reconstruction
is a separate check; replaying its recorded evaluations does not perform it.
Only the new receipt is written. Original documents, proofs and blobs stay
unchanged. Run with provider HTTP blocked when collecting acceptance evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from time import monotonic
from typing import Any

from app.options_backtest.run_wire import result_from_wire
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden_proof import proof_sha256, reprove
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.protocol import CostScenario, Window, apply_scenario
from app.research.options_golden_search.snapshots import SnapshotRunData
from app.research.options_golden_search.stages import StudyEvaluator
from app.research.runs.hashing import hash_payload
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


def verify_study(exported: Mapping[str, Any], blobs: BlobStore) -> dict[str, Any]:
    """Replay every exported evaluation; no retained absence can fall through to the lake."""
    row = exported["document"]
    evaluator = StudyEvaluator(row, lambda: None, lambda *_: None, blobs=blobs)
    protocol = evaluator.protocol
    if protocol.protocol_hash != row["protocol_hash"]:
        raise ValueError("The exported protocol differs from its recorded identity.")
    total_bytes = sum(len(blobs.get(digest)) for digest in evaluator.snapshot.manifest.values())
    if total_bytes != evaluator.snapshot.retained_bytes:
        raise ValueError("The snapshot's retained byte count differs from its manifest.")
    records = exported["evaluations"]
    keys = [record["key"] for record in records]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("A study needs distinct completed evaluation records.")
    for record in records:
        identity = record["identity"]
        if (hash_payload(identity) != record["key"] or identity["protocol"] != row["protocol_hash"]
                or identity["snapshot"] != protocol.snapshot_id or identity["identities"] != protocol.identities):
            raise ValueError("An evaluation is not bound to the exported study.")
        window = Window.model_validate(identity["window"])
        setup = OptionsBacktestSetup.model_validate(identity["setup"])
        scenario = next((item for item in (CostScenario(key="base"), *protocol.stress)
                         if item.key == identity["scenario"]), None)
        if scenario is None:
            raise ValueError("An evaluation names an unfrozen cost scenario.")
        if record["result"] is None:
            raise ValueError("An unfinished evaluation cannot be verified.")
        expected = evaluator.load(record["result"], window)
        replayed = evaluate_setup(apply_scenario(setup, scenario), window,
            SnapshotRunData(setup.underlying, evaluator.snapshot, blobs),
            risk_free_daily_return=protocol.policy.risk_free_daily_return,
            fee_multiplier=scenario.effective_fee_multiplier)
        if replayed != expected:
            raise ValueError(f"Evaluation {record['key']} does not reproduce its exact retained evidence.")
    return {"snapshot_id": protocol.snapshot_id, "manifest_files": len(evaluator.snapshot.manifest),
            "retained_bytes": total_bytes, "evaluations_replayed": len(records),
            "selection_reconstruction": "not_performed"}


def verify_proof(exported: Mapping[str, Any], sources: Mapping[str, Any], blobs: BlobStore) -> dict[str, Any]:
    """Use the production proof replay without appending a qualification event."""
    proof = exported["proof"]
    if proof_sha256(proof) != exported["proof_sha256"]:
        raise ValueError("The exported proof differs from its approved identity.")
    source = result_from_wire(sources[str(exported["source_run_id"])])
    verified = reprove(source, proof, blobs=blobs)
    return {"sessions_replayed": len(source.sessions), "manifest_files": len(proof["manifest"]),
            "recorded_proof_sha256": exported["proof_sha256"], "current_rules_digest": verified["rules_digest"]}


def verify_export(bundle: Mapping[str, Any], blobs: BlobStore, *,
                  on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Return explicit per-record outcomes; any failed record makes the receipt fail."""
    if bundle["version"] != 1 or not (bundle["studies"] or bundle["proofs"]):
        raise ValueError("The export must be a nonempty version-1 Options evidence inventory.")
    report: dict[str, Any] = {"version": 1, "started_ms": now_ms_utc(), "studies": [], "proofs": []}
    for kind, records in (("studies", bundle["studies"]), ("proofs", bundle["proofs"])):
        ids = [record["document"]["id"] if kind == "studies" else record["id"] for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError("The export contains duplicate evidence identities.")
        for identity, record in zip(ids, records, strict=True):
            started = monotonic()
            item = {"id": identity, "result": "passed"}
            try:
                item.update(verify_study(record, blobs) if kind == "studies" else
                            verify_proof(record, bundle["sources"], blobs))
            except (KeyError, TypeError, ValueError, OSError, ArithmeticError, ProofMismatchError) as exc:
                item.update(result="failed", reason=f"{type(exc).__name__}: {exc}")
            item["duration_ms"] = int((monotonic() - started) * 1000)
            report[kind].append(item)
            if on_progress is not None:
                on_progress({"kind": kind, **item})
    report["ended_ms"] = now_ms_utc()
    report["passed"] = all(item["result"] == "passed" for kind in ("studies", "proofs") for item in report[kind])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--blob-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    bundle_path, blob_root, report_path = (path.resolve() for path in (args.bundle, args.blob_root, args.report))
    if not blob_root.is_dir():
        parser.error("the retained blob root must be an existing directory")
    if report_path == bundle_path or report_path.is_relative_to(blob_root) or report_path.exists():
        parser.error("the report must be a new file outside the input bundle and blob store")
    payload = bundle_path.read_bytes()
    report = verify_export(json.loads(payload), BlobStore(blob_root),
        on_progress=lambda item: logger.info("Retained Options evidence checked", extra={"action": "retained_options_checked", **item}))
    report["bundle_sha256"] = hashlib.sha256(payload).hexdigest()
    atomic_write_bytes(report_path, json.dumps(report, indent=2, allow_nan=False).encode())
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

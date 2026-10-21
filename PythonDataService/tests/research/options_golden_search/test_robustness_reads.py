"""Retained descriptive values and bounded measured/untested sensitivity (#3401)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.research import options_candidate_evidence as retained
from app.research import options_robustness as robustness
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import stages
from app.research.options_golden_search.protocol import Knob, materialize
from app.routers.options_robustness import router

FIXTURES = Path(__file__).parents[2] / "fixtures" / "options_evidence"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any], BlobStore]:
    monkeypatch.setattr(retained, "_result_cache", retained.OrderedDict())
    data = fixture("development-evidence")
    blobs = BlobStore(tmp_path)
    assert blobs.put(json.dumps(data["payload"], separators=(",", ":")).encode()) == data["record"]["result"]["evidence_blob"]
    return fixture("study-record"), data["record"], blobs


def test_actual_robustness_is_the_verified_canonical_projection_with_final_still_unopened(evidence: tuple) -> None:
    row, record, blobs = evidence
    digest = retained.setup_sha256(retained.retained_protocol(row).setup)
    source = retained.load_record(row, record, digest, "development", blobs=blobs)
    result = robustness.project(source, row)
    assert (result.observed_sessions, result.scheduled_sessions, result.complete) == (4, 373, False)
    assert result.metrics.net_profit_usd == "-339.60"
    assert result.metrics.no_trade_sessions == 2
    assert result.metrics.expectancy_usd == str(source.result.metrics.expectancy_usd)
    assert result.metrics.profit_without_best_session_usd == str(source.result.metrics.profit_without_best_session_usd)
    assert result.metrics.top_five_profit_contribution is None
    assert result.capital.cash_needed_usd == str(source.result.metrics.capital_evidence.cash_needed_usd)
    assert result.exposed_final_outcome is None
    assert len(result.model_dump_json()) < 10_000
    row["finalists"] = [digest]
    row["final_results"] = [{**fixture("candidate-development")["candidate"], "verdict": "fails_rules",
        "gate_outcomes": [{"gate_id": "stress:costs:profit", "status": "fails_rules", "actual": "-1", "required": ">0", "overridable": True, "reason": "Retained stress failed."}]}]
    exposed = robustness.project(source, row)
    assert exposed.scope == "development"
    assert exposed.exposed_final_outcome.verdict == "fails_rules"
    assert exposed.exposed_final_outcome.gate_outcomes[0].reason == "Retained stress failed."


def test_cache_reuses_only_verified_full_identity_and_never_hides_missing_or_changed_evidence(evidence: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    row, record, blobs = evidence
    digest = retained.setup_sha256(retained.retained_protocol(row).setup)
    canonical = stages.summarize_evidence
    calls = 0
    def summarize(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return canonical(*args, **kwargs)
    monkeypatch.setattr(stages, "summarize_evidence", summarize)
    for _ in range(2):
        retained.load_record(row, record, digest, "development", blobs=blobs)
    assert calls == 1
    changed = json.loads(json.dumps(record))
    changed["result"]["metrics"]["net_profit_usd"] = "999"
    with pytest.raises(ValueError, match="qualification metrics"):
        retained.load_record(row, changed, digest, "development", blobs=blobs)
    assert calls == 2
    artifact = blobs._path(record["result"]["evidence_blob"])
    artifact.write_bytes(b"corrupt after the cache was warmed")
    with pytest.raises(ProofMismatchError) as corrupt:
        retained.load_record(row, record, digest, "development", blobs=blobs)
    assert corrupt.value.code == "BLOB_CORRUPT"
    artifact.unlink()
    with pytest.raises(ProofMismatchError) as missing:
        retained.load_record(row, record, digest, "development", blobs=blobs)
    assert missing.value.code == "BLOB_MISSING"


async def test_neighbors_page_declared_values_using_only_bounded_scalar_reads(evidence: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    row, record, _ = evidence
    protocol = retained.retained_protocol(row)
    protocol = protocol.model_copy(update={"knobs": (protocol.knobs[0].model_copy(update={"values": tuple(str(v) for v in range(10, 71))}),)})
    row["protocol"], row["protocol_hash"] = protocol.model_dump(mode="json"), protocol.protocol_hash
    digest = retained.setup_sha256(protocol.setup)
    points = [{protocol.knobs[0].path: str(v)} for v in range(10, 71)]
    row["research"]["selection"]["points"] = {retained.setup_sha256(materialize(protocol, point)): point for point in points}
    baseline_key = retained.resolve(row, digest, "development")[3]
    reads = []
    async def read(fn: Any, *args: Any) -> Any:
        reads.append(fn)
        if fn is repo.get:
            return row
        assert fn is repo.evaluation_summaries
        assert len(args[1]) <= 50
        return {baseline_key: {"metrics": record["result"]["metrics"], "complete": False}} if baseline_key in args[1] else {}
    monkeypatch.setattr(robustness, "with_connection", read)
    first = await robustness.neighbors(row["id"], digest, "development")
    second = await robustness.neighbors(row["id"], digest, "development", offset=50)
    assert len(first.items) == 50 and len(second.items) == 11 and first.total == 61
    rows = [*first.items, *second.items]
    assert len({item.ordinal for item in rows}) == 61
    measured = [item for item in rows if item.state == "retained"]
    assert len(measured) == 1 and measured[0].is_selected
    assert measured[0].net_profit_usd == "-339.60" and measured[0].verdict == "could_not_evaluate"
    assert all(item.net_profit_usd is None and item.verdict is None for item in rows if item.state == "untested")
    assert repo.get_evaluation not in reads
    with pytest.raises(LookupError, match="not exposed"):
        await robustness.neighbors(row["id"], digest, "final")


async def test_actual_three_neighbors_match_retained_scalar_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    row = fixture("study-record")
    summaries = fixture("neighbor-summaries")
    async def read(fn: Any, *_args: Any) -> Any:
        return row if fn is repo.get else summaries
    monkeypatch.setattr(robustness, "with_connection", read)
    digest = retained.setup_sha256(retained.retained_protocol(row).setup)
    page = await robustness.neighbors(row["id"], digest, "development")
    assert page.model_dump(mode="json") == fixture("neighbors-development")
    assert [item.net_profit_usd for item in page.items] == ["-346.90", "-339.60", "-361.60"]
    assert all(item.verdict == "could_not_evaluate" for item in page.items)


async def test_invalid_declared_dependency_is_not_a_measured_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    row = fixture("study-record")
    protocol = retained.retained_protocol(row)
    leg_id = protocol.setup.legs[0].leg_id
    knobs = (Knob(path=f"legs.{leg_id}.momentum", values=(None, {"kind": "percent_up", "value": "25"})),
        Knob(path=f"legs.{leg_id}.reentry_on_stop", values=(None, {"kind": "on_momentum", "count": 1})))
    protocol = protocol.model_copy(update={"knobs": knobs})
    row["protocol"], row["protocol_hash"] = protocol.model_dump(mode="json"), protocol.protocol_hash
    selected = {f"legs.{leg_id}.momentum": None}
    digest = retained.setup_sha256(materialize(protocol, selected))
    row["research"]["selection"]["points"] = {digest: selected}
    async def read(fn: Any, *_args: Any) -> Any:
        return row if fn is repo.get else {}
    monkeypatch.setattr(robustness, "with_connection", read)
    result = await robustness.neighbors(row["id"], digest, "development")
    invalid = [item for item in result.items if item.state == "invalid"]
    assert len(invalid) == 1
    assert invalid[0].candidate_hash is invalid[0].net_profit_usd is invalid[0].verdict is None


async def test_http_scope_shape_and_page_bounds(evidence: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    row, record, blobs = evidence
    digest = retained.setup_sha256(retained.retained_protocol(row).setup)
    async def read(fn: Any, *_args: Any) -> Any:
        if fn is repo.get:
            return row
        assert fn is repo.get_evaluation
        return record
    monkeypatch.setattr(robustness, "with_connection", read)
    monkeypatch.setattr(stages, "default_blob_store", lambda: blobs)
    app = FastAPI()
    app.include_router(router)
    base = f"/studies/{row['id']}/candidate-evidence/{digest}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(base + "/robustness")).status_code == 200
        assert (await client.get(base + "/robustness?window=final")).status_code == 404
        assert (await client.get(base + "/neighbors?limit=51")).status_code == 422
        del record["result"]["metrics_sha256"]
        broken = await client.get(base + "/robustness")
        assert broken.status_code == 503
        assert broken.json()["detail"]["code"] == "RETAINED_ROBUSTNESS_UNAVAILABLE"

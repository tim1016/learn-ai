"""One rejected candidate from its retained bytes; no replay, capture or final exposure (#3397)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.research import options_candidate_evidence as reader
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import stages
from app.research.options_golden_search.protocol import OptionsSearchProtocol, materialize
from app.routers.options_golden_search import router
from app.schemas.options_golden_search import StudyDetail

FIXTURES = Path(__file__).parents[2] / "fixtures" / "options_evidence"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def retained(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], BlobStore]:
    data = fixture("development-evidence")
    blobs = BlobStore(tmp_path)
    digest = blobs.put(json.dumps(data["payload"], separators=(",", ":"), allow_nan=False).encode())
    assert digest == data["record"]["result"]["evidence_blob"]
    return fixture("study-record"), data["record"], blobs


def test_rejected_candidate_restores_null_gaps_and_partial_scope_from_retained_bytes(retained: tuple) -> None:
    row, record, blobs = retained
    digest = reader.setup_sha256(OptionsSearchProtocol.model_validate(row["protocol"]).setup)
    result = reader.project(reader.load_record(row, record, digest, "development", blobs=blobs), session_offset=2, session_limit=2)
    assert result.candidate.verdict == "could_not_evaluate"
    assert result.candidate.net_profit_usd == "-339.60"
    assert result.coverage.scheduled_sessions == 373
    assert result.coverage.replayed_sessions == 4
    assert result.coverage.required_held_marks == 599
    assert result.coverage.missing_held_marks == 2
    assert result.unobserved_sessions == 369
    assert [p.timestamp_ms for p in result.candidate.equity if p.equity_usd is None] == [1730925480000, 1730925600000]
    assert len(result.candidate.equity) <= 2000
    assert len(result.candidate.sessions) == 2
    assert all(not s.attempts for s in result.candidate.sessions)
    assert result.candidate.sessions[0].status == "no_trade"
    assert result.candidate.sessions[0].net_profit_usd == "0"
    assert any("entry is read" in line for line in result.settings_description)
    assert any("exit is read" in line for line in result.settings_description)


def test_unopened_final_undeclared_point_and_changed_evaluation_identity_are_refused(retained: tuple) -> None:
    row, record, blobs = retained
    digest = reader.setup_sha256(OptionsSearchProtocol.model_validate(row["protocol"]).setup)
    with pytest.raises(LookupError, match="not exposed"):
        reader.load_record(row, record, digest, "final", blobs=blobs)
    with pytest.raises(LookupError, match="not admitted"):
        reader.load_record(row, record, "f" * 64, "development", blobs=blobs)
    record["identity"]["scenario"] = "changed"
    with pytest.raises(LookupError, match="exact retained"):
        reader.load_record(row, record, digest, "development", blobs=blobs)


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_artifact_never_falls_back_to_lake(retained: tuple, corrupt: bool) -> None:
    row, record, blobs = retained
    digest = reader.setup_sha256(OptionsSearchProtocol.model_validate(row["protocol"]).setup)
    artifact = blobs._path(record["result"]["evidence_blob"])
    if corrupt:
        artifact.write_bytes(b"not the retained trace")
    else:
        artifact.unlink()
    with pytest.raises(ProofMismatchError) as error:
        reader.load_record(row, record, digest, "development", blobs=blobs)
    assert error.value.code == ("BLOB_CORRUPT" if corrupt else "BLOB_MISSING")


async def test_candidate_pages_are_stable_beyond_old_top50_without_loading_traces(monkeypatch: pytest.MonkeyPatch) -> None:
    row = fixture("study-record")
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    protocol = protocol.model_copy(update={"knobs": (protocol.knobs[0].model_copy(update={"values": tuple(str(v) for v in range(10, 71))}),)})
    row["protocol"] = protocol.model_dump(mode="json")
    row["protocol_hash"] = protocol.protocol_hash
    path = protocol.knobs[0].path
    points = {}
    for value in range(10, 71):
        point = {path: str(value)}
        points[reader.setup_sha256(materialize(protocol, point))] = point
    row["research"]["selection"]["points"] = dict(reversed(list(points.items())))
    calls = []
    async def read(fn: Any, *args: Any) -> Any:
        calls.append(fn)
        if fn is repo.get:
            return row
        assert fn is repo.evaluation_summaries
        assert len(args[1]) <= 50
        return {}
    monkeypatch.setattr(reader, "with_connection", read)
    first = await reader.page(row["id"])
    second = await reader.page(row["id"], offset=50)
    assert first.total == 61
    assert len(first.items) == 50 and len(second.items) == 11
    assert len({i.candidate_hash for i in [*first.items, *second.items]}) == 61
    assert all(i.state == "uncomputed" for i in first.items)
    assert repo.get_evaluation not in calls


async def test_http_rejected_evidence_is_read_only_and_scope_fenced(retained: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    row, record, blobs = retained
    digest = reader.setup_sha256(OptionsSearchProtocol.model_validate(row["protocol"]).setup)
    calls = []
    async def read(fn: Any, *_args: Any) -> Any:
        calls.append(fn)
        if fn is repo.get:
            return row
        assert fn is repo.get_evaluation
        return record
    def no_write(_payload: bytes) -> str:
        raise AssertionError("Reads must not publish evidence")
    monkeypatch.setattr(reader, "with_connection", read)
    monkeypatch.setattr(stages, "default_blob_store", lambda: blobs)
    monkeypatch.setattr(blobs, "put", no_write)
    app = FastAPI()
    app.include_router(router)
    base = f"/studies/{row['id']}/candidate-evidence/{digest}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(base)
        final = await client.get(base + "?window=final")
        invalid = await client.get(base + "?session_limit=51")
    assert response.status_code == 200, response.text
    assert response.json()["candidate"]["net_profit_usd"] == "-339.60"
    assert any(p["equity_usd"] is None for p in response.json()["candidate"]["equity"])
    assert final.status_code == 404
    assert invalid.status_code == 422
    assert calls.count(repo.get_evaluation) == 1
    overview = StudyDetail.from_record(row)
    assert all(not c.equity and not c.sessions and not c.monthly for c in overview.candidates)

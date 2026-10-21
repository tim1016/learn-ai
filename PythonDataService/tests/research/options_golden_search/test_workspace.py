"""Actual retained identities and truthful, independent verdict dimensions (#3395)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.research import options_evidence as workspace
from app.routers import options_golden_search
from app.schemas.golden_option_setups import GoldenOptionSetupView
from app.schemas.options_backtest_runs import OptionsBacktestRunDetailResponse

FIXTURES = Path(__file__).parents[2] / "fixtures" / "options_evidence"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def run() -> OptionsBacktestRunDetailResponse:
    return OptionsBacktestRunDetailResponse.model_validate(fixture("run76"))


def golden() -> GoldenOptionSetupView:
    return GoldenOptionSetupView.model_validate(fixture("golden76"))


def test_actual_positive_replay_has_manual_approval_without_search_ancestry() -> None:
    view = workspace.run_workspace(run(), [golden()])
    assert view.headline == "Profitable in replay. Research qualification is missing."
    assert view.setup_sha256 == "d1b43858a5665309c247592c4c93bbc51cbd3db2b75a8fbd5632d9aab1108b8a"
    assert view.source_run_id == 76
    assert view.period.observed_sessions == view.period.scheduled_sessions == 499
    assert view.research.status == "not_recorded"
    assert view.approvals[0].source == "backtest"
    assert view.approvals[0].study_id is None
    assert view.approvals[0].identity_matches
    assert "17109.10 USD" in view.replay.explanation
    assert [str(leg.momentum.value) for leg in view.setup.legs] == ["35", "50"]


@pytest.mark.parametrize("proof_status", ["ready", "stale", "revoked"])
def test_proof_changes_do_not_manufacture_or_remove_historical_research(proof_status: str) -> None:
    version = golden()
    version.status = proof_status
    version.evidence.options_search_study_id = "os_" + "a" * 40
    version.evidence.study_manifest_sha256 = "b" * 64
    version.evidence.verdict = "owner_exception"
    version.evidence.waived_gate_ids = ["final:exposure"]
    view = workspace.run_workspace(run(), [version], golden_id=version.id)
    assert view.research.status == "recorded"
    assert view.approvals[0].proof_status == proof_status
    assert view.approvals[0].research_verdict == "owner_exception"
    assert view.approvals[0].waived_gate_ids == ["final:exposure"]


def test_legacy_partial_link_and_same_name_mismatched_approval_never_qualify_source() -> None:
    version = golden()
    version.evidence.options_search_study_id = "os_" + "a" * 40
    assert workspace.run_workspace(run(), [version]).research.status == "not_recorded"
    version.setup_sha256 = "c" * 64
    ordinary = workspace.run_workspace(run(), [version])
    assert ordinary.approval.status == "identity_mismatch"
    pinned = workspace.run_workspace(run(), [version], golden_id=version.id)
    assert pinned.source_run_id is None
    assert pinned.replay.status == "unavailable"
    assert "identity mismatch" in pinned.headline


def test_actual_blocked_search_never_inherits_combined_replay() -> None:
    view = workspace.study_workspace(fixture("study-record"))
    assert view.headline == "This search did not establish a result."
    assert view.period.observed_sessions == 4
    assert view.period.scheduled_sessions == 373
    assert view.final.status == "not_opened"
    assert view.research.status == "inconclusive"
    assert len(view.setup.legs) == 1
    assert view.setup.legs[0].right == "put"
    assert view.source_run_id is None
    assert view.frozen_search_settings[0].values == ["20", "25", "30"]


def test_pending_and_successful_selection_do_not_inherit_a_baseline_failure() -> None:
    row = fixture("study-record")
    row["research"] = None
    row["candidates"] = []
    assert workspace.study_workspace(row).research.status == "pending"
    row = fixture("study-record")
    row["research"]["selection"]["winner"] = {"point_hash": "a" * 64}
    assert workspace.study_workspace(row).research.status == "procedure"
    row["research"]["selection"]["winner"] = None
    for candidate in row["candidates"]:
        candidate["gate_outcomes"][0]["reason"] = "capital invalid"
    assert "missing valuation" not in workspace.study_workspace(row).research.explanation


async def test_comparator_and_inspected_search_are_explicit_non_lineage_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    async def read_run(run_id: int) -> OptionsBacktestRunDetailResponse:
        return OptionsBacktestRunDetailResponse.model_validate(fixture(f"run{run_id}"))

    async def get(_fn, _id):
        return fixture("study-record")

    class Golden:
        async def list(self, **_kwargs):
            return [golden()]

    monkeypatch.setattr(workspace, "read_run", read_run)
    monkeypatch.setattr(workspace, "with_connection", get)
    monkeypatch.setattr(workspace, "GoldenOptionsSetups", Golden)
    view = await workspace.read("run", "76", inspected_study_id=fixture("study-record")["id"], comparator_run_id=69)
    assert view.research.status == "not_recorded"
    assert [r.kind for r in view.relations] == ["inspected_search", "historical_comparator"]
    assert all(not r.settings_match for r in view.relations)
    assert "Settings mismatch" in view.relations[0].explanation
    assert view.setup_sha256 != view.relations[0].setup_sha256


async def test_workspace_endpoint_serializes_exact_money_ids_and_read_only_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def read(kind, subject_id, **_kwargs):
        assert (kind, subject_id) == ("run", "76")
        return workspace.run_workspace(run(), [golden()])

    monkeypatch.setattr(workspace, "read", read)
    app = FastAPI()
    app.include_router(options_golden_search.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/workspace/run/76")
        invalid = await client.get("/workspace/run/-1")
    assert response.status_code == 200
    assert response.json()["setup"]["starting_cash_usd"] == "10000"
    assert response.json()["approvals"][0]["golden_id"] == golden().id
    assert invalid.status_code == 422

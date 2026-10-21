"""The frozen procedure stays separate from setup performance and final outcomes (#3400)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.research import options_research_lineage as lineage
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.protocol import OptionsSearchProtocol
from app.routers.options_research_lineage import router

FIXTURES = Path(__file__).parents[2] / "fixtures" / "options_evidence"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_actual_put_procedure_explains_no_winners_and_distinct_receipt_counts() -> None:
    view = lineage.project(fixture("study-record"), fixture("lineage-receipts"))
    assert view.counts.distinct_development_candidates == 3
    assert view.counts.eligible_development_candidates == 0
    assert view.counts.rejected_development_candidates == 3
    assert view.counts.evaluation_keys == view.counts.completed_evaluations == view.counts.physical_starts == 27
    assert view.counts.trial_events == 28
    assert view.counts.evaluated_trial_events == 21
    assert view.counts.cache_hit_trial_events == 7
    assert view.settings[0].values == ["20", "25", "30"]
    assert len(view.folds) == 6
    assert all(f.state == "no_eligible_point" and f.winner_hash is None and f.forward_net_profit_usd is None for f in view.folds)
    assert view.final_state == "not_opened" and view.finalists == []
    assert view.exposure_ledger[0].scope == "development"
    assert view.exposure.classification == "history_unknown"
    assert view.parent_study_id == fixture("study-record")["parent_study_id"]


def test_distinct_fold_winners_and_final_stress_remain_exact_recorded_facts() -> None:
    row = fixture("study-record")
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    points = row["research"]["selection"]["points"]
    hashes = list(points)
    for index, observed in enumerate(row["research"]["folds"][:2]):
        digest = hashes[index]
        observed.update(status="evaluated", winner_hash=digest,
            forward={"point_hash": digest, "net_profit_usd": "10.01234567890123456789", "eligible": False})
    frozen = copy.deepcopy(row["candidates"][0])
    frozen["gate_outcomes"].append({"gate_id": "final:fees:technical_validity", "status": "could_not_evaluate", "actual": None, "required": "Complete marks", "reason": "Missing stress mark"})
    row.update(finalists=[frozen["candidate_hash"]], final_results=[frozen])
    view = lineage.project(row, fixture("lineage-receipts"))
    assert view.folds[0].winner_hash != view.folds[1].winner_hash
    assert view.folds[0].forward_net_profit_usd == "10.01234567890123456789"
    assert view.folds[0].forward_eligible is False
    assert view.final_state == "opened"
    assert view.finalists[0].gate_outcomes[-1].gate_id == "final:fees:technical_validity"
    assert view.finalists[0].verdict == frozen["verdict"]
    assert view.stress[0].mandatory == protocol.policy.stress_gate
    assert view.stress[0].key == protocol.stress[0].key


def test_legacy_absent_results_are_not_zero_or_inferred_final_success() -> None:
    row = fixture("study-record")
    retained = fixture("lineage-receipts")
    row.pop("research")
    row.update(finalists=[], final_results=[], final_opened_at_ms=1792340655994)
    retained["counts"].update(evaluation_keys=0, completed_evaluations=0, physical_starts=0)
    view = lineage.project(row, retained)
    assert view.counts.receipt_state == "not_recorded"
    assert view.counts.evaluation_keys is None
    assert view.counts.distinct_development_candidates is None
    assert all(f.state == "not_recorded" for f in view.folds)
    assert view.final_state == "opened" and not view.finalists


async def test_http_is_bounded_scope_fenced_and_reads_no_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    row = fixture("study-record")
    retained = fixture("lineage-receipts")
    calls = []
    async def read(fn: Any, *args: Any) -> Any:
        calls.append(fn)
        if fn is repo.get:
            return row
        if fn is lineage.receipts:
            assert "final" not in args[1]
            return retained
        assert fn is lineage.trial_rows
        assert "final" not in args[1]
        return [item for item in retained["trials"] if item["id"] > args[2]][:args[3] + 1]
    monkeypatch.setattr(lineage, "with_connection", read)
    app = FastAPI()
    app.include_router(router)
    base = f"/studies/{row['id']}/lineage"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        overview = await client.get(base)
        first = await client.get(base + "/trials?limit=2")
        second = await client.get(base + f"/trials?limit=2&after={first.json()['next_cursor']}")
        too_many = await client.get(base + "/trials?limit=51")
        too_large = await client.get(base + "/trials?after=9223372036854775808")
    assert overview.status_code == 200, overview.text
    assert [item["receipt_id"] for item in first.json()["items"]] == ["69", "70"]
    assert [item["receipt_id"] for item in second.json()["items"]] == ["71", "72"]
    assert second.json()["items"][0]["status"] == "cache_hit"
    assert too_many.status_code == too_large.status_code == 422
    assert repo.get_evaluation not in calls

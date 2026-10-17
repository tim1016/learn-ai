"""Options workbench transport preserves exact evidence and refuses unopened final reads."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.research.options_golden_search.repository import Conflict
from app.routers.options_golden_search import get_service, router

STUDY = "os_" + "a" * 40
CANDIDATE = "b" * 64
SESSION = 1712151000000


@pytest.fixture
def api() -> tuple[FastAPI, SimpleNamespace]:
    state = SimpleNamespace(opened=False,calls=[])
    async def read_session(study_id: str,digest: str,window: str,session_open_ms: int) -> dict:
        state.calls.append((study_id,digest,window,session_open_ms))
        if not state.opened:
            raise LookupError("This candidate has no exposed evidence in the requested window.")
        return {"session_open_ms":session_open_ms,"net_profit_usd":"1.234567890123456789","status":"traded",
            "detail":"Exact retained session.","attempts":[{"leg_id":"call","attempt":0,"contract":"SPY240403C00500000",
                "side":"buy","contracts":1,"entry_ms":session_open_ms+60000,"exit_ms":session_open_ms+120000,
                "entry_price_usd":"1.01","exit_price_usd":"1.03","entry_fee_usd":"0.1234567890123456789",
                "exit_fee_usd":"0.6419753208641975321","net_profit_usd":"1.234567890123456789","exit_reason":"exit_time"}]}
    async def command(study_id: str,body: object) -> None:
        raise Conflict("This study changed. Refresh before sending another command.")
    app = FastAPI()
    app.include_router(router,prefix="/options-search")
    app.dependency_overrides[get_service] = lambda: SimpleNamespace(read_session=read_session,command=command)
    return app,state


async def test_lazy_session_http_contract_retains_exact_money_and_integer_instants(api: tuple[FastAPI,SimpleNamespace]) -> None:
    app,state = api
    state.opened = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response = await client.get(f"/options-search/studies/{STUDY}/candidates/{CANDIDATE}/sessions/{SESSION}?window=final")
    assert response.status_code==200
    assert state.calls==[(STUDY,CANDIDATE,"final",SESSION)]
    result = response.json()
    assert result["net_profit_usd"]=="1.234567890123456789"
    assert result["attempts"][0]["entry_fee_usd"]=="0.1234567890123456789"
    assert type(result["attempts"][0]["entry_ms"]) is int


async def test_unopened_final_evidence_refuses_without_disclosing_outcomes(api: tuple[FastAPI,SimpleNamespace]) -> None:
    app,_state = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response = await client.get(f"/options-search/studies/{STUDY}/candidates/{CANDIDATE}/sessions/{SESSION}?window=final")
    assert response.status_code==404
    assert "net_profit_usd" not in response.text


async def test_stale_command_is_a_conflict_and_unsupported_wire_instants_are_rejected(api: tuple[FastAPI,SimpleNamespace]) -> None:
    app,state = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        refused = await client.post(f"/options-search/studies/{STUDY}/commands",json={"command":"run_research","expected_revision":0,"idempotency_key":"run"})
        invalid = await client.get(f"/options-search/studies/{STUDY}/candidates/{CANDIDATE}/sessions/2024-04-03?window=final")
    assert refused.status_code==409
    assert invalid.status_code==422
    assert not state.calls


async def test_exception_http_requires_exact_complete_scoped_acknowledgements_and_blocks_technical() -> None:
    from app.research.options_golden_search.approval import validate_approval
    from app.schemas.options_golden_search import Command, StudyDetail

    def gate(identifier: str) -> dict:
        return {"gate_id": identifier, "status": "not_enough_evidence", "actual": "0.4", "required": "1.0", "reason": "Frozen rule did not qualify."}

    def candidate(scope: str) -> dict:
        return {"candidate_hash": CANDIDATE, "label": "Exact candidate", "is_original": False,
            "verdict": "not_enough_evidence", "net_profit_usd": "10", "drawdown_usd": "1",
            "drawdown_fraction": "0.01", "drawdown_percent": "1", "sharpe": 0.4,
            "completed_sessions": 2, "recovery": "Recovered", "gate_outcomes": [gate(scope)],
            "evidence": [], "monthly": [], "equity": [], "sessions": []}

    row = {"id": STUDY, "revision": 1, "phase": "approval", "status": "completed", "protocol_hash": "locked",
        "setup_name": "Exact saved setup", "underlying": "SPY", "progress": {"stage": "approval"},
        "candidates": [candidate("development:activity")], "final_results": [candidate("final:sharpe")],
        "finalists": [CANDIDATE], "gate_outcomes": [gate("forward_0:sharpe")],
        "exposure": {"classification": "history_unknown", "explanation": "Prior use is unknown."},
        "allowed_commands": ["approve_exception"], "dispatch": None, "failure_reason": None,
        "qualification_id": None, "qualification_status": None}
    accepted = []

    async def read_detail(study_id: str) -> StudyDetail:
        return StudyDetail.from_record(row)

    async def command(study_id: str, body: Command) -> StudyDetail:
        validate_approval(row, body)
        accepted.append(body.waived_gate_ids)
        return StudyDetail.from_record(row)

    app = FastAPI()
    app.include_router(router, prefix="/options-search")
    app.dependency_overrides[get_service] = lambda: SimpleNamespace(detail=read_detail, command=command)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        detail = (await client.get(f"/options-search/studies/{STUDY}")).json()
        review = detail["approval_reviews"][CANDIDATE]
        identifiers = [failure["gate_id"] for failure in review["failures"]]
        assert set(identifiers) == {"forward_0:sharpe", "development:activity", "final:sharpe", "final:exposure"}
        assert review["blocked_reason"] is None
        body = {"command": "approve_exception", "expected_revision": 1, "idempotency_key": "acknowledge", "candidate_hash": CANDIDATE, "comment": "Reviewed every listed failed rule."}
        for waived in ([], identifiers[:-1], [*identifiers, "unrelated:rule"], [*identifiers, identifiers[0]]):
            rejected = await client.post(f"/options-search/studies/{STUDY}/commands", json={**body, "waived_gate_ids": waived})
            assert rejected.status_code == 400
        assert not accepted
        approved = await client.post(f"/options-search/studies/{STUDY}/commands", json={**body, "waived_gate_ids": identifiers})
        assert approved.status_code == 200
        assert accepted == [tuple(identifiers)]
        row["gate_outcomes"][0]["status"] = "could_not_evaluate"
        rejected = await client.post(f"/options-search/studies/{STUDY}/commands", json={**body, "waived_gate_ids": identifiers})
        assert rejected.status_code == 400
        assert len(accepted) == 1
        blocked = (await client.get(f"/options-search/studies/{STUDY}")).json()["approval_reviews"][CANDIDATE]
        assert "Technical invalidity" in blocked["blocked_reason"]

"""Publication failure boundaries use real scored/source replays and an atomic in-memory writer."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.options_backtest.run import run_sessions
from app.options_bots.golden_proof import setup_sha256
from app.research.options_golden_search import approval, identity
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.planning import freeze_protocol
from app.research.options_golden_search.protocol import Knob, Window
from app.research.persistence.fence import StaleAttemptError
from app.utils.session_anchors import et_midnight_ms
from tests.options_backtest._run_data import WED, open_ms
from tests.research.options_golden_search._evidence import scene


@pytest.fixture
def publication(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    written, data = scene((WED,), exits=("2",))
    ordinary = run_sessions(written, (WED,), data, slippage_ladder=(), cash_ladder=())
    observed_window = Window(key="final", start_ms=open_ms(WED), end_ms=ordinary.sessions[-1].session_close_ms + 1,
                             session_ms=(open_ms(WED),), annualization_sessions=251, activity_floor=1)
    scored = evaluate_setup(written, observed_window, data)
    protocol = freeze_protocol(written, (Knob(path="entry.offset_ms", values=(300000, 360000)),),
        start_ms=et_midnight_ms(date(2024, 1, 1)), end_ms=et_midnight_ms(date(2025, 7, 1)),
        identities={"selection": "test"})
    digest = setup_sha256(written)
    row = {
        "id": "approval-study", "revision": 1, "attempt": 1, "phase": "approval", "status": "running",
        "setup_name": "Exact setup", "protocol": protocol.model_dump(mode="json"), "snapshot": {},
        "approval_request": {"command": "approve", "expected_revision": 0, "idempotency_key": "approval",
                             "candidate_hash": digest, "comment": "reviewed"},
        "research": {"selection": {"points": {}}}, "finalists": [digest], "gate_outcomes": [],
        "candidates": [{"candidate_hash": digest, "gate_outcomes": []}],
        "final_results": [{"candidate_hash": digest, "gate_outcomes": [], "verdict": "meets_rules"}],
        "exposure": {"classification": "not_opened"},
    }
    entries = [{"key": "selected-evidence", "identity": {"window": protocol.final.model_dump(mode="json")}, "result": {"retained": True}}]
    state = SimpleNamespace(row=copy.deepcopy(row), records=[], manifests=[], source=ordinary, scored=scored,
                            fail_manifest=False, proof_calls=0, before_proof=None)

    class Connection:
        @asynccontextmanager
        async def transaction(self) -> AsyncIterator[None]:
            previous = copy.deepcopy((state.row, state.records, state.manifests))
            try:
                yield
            except BaseException:
                state.row, state.records, state.manifests = previous
                raise

        async def fetchrow(self, query: str, *args: object) -> dict[str, Any]:
            assert "research_options_backtest_runs" in query
            return {"id": 42, "status": "complete"}

        async def fetchval(self, query: str, *args: object) -> int:
            return len(entries) if "count(*)" in query else 1

        async def fetch(self, query: str, *args: object) -> list[Any]:
            assert "research_options_search_trials" in query
            return []

        async def execute(self, query: str, *args: object) -> None:
            assert "research_options_search_approvals" in query
            if state.fail_manifest:
                raise RuntimeError("study manifest storage failed")
            state.manifests.append(args)

    connection = Connection()

    async def with_connection(fn: Callable[..., Any], *args: object, **kwargs: object) -> Any:
        return await fn(connection, *args, **kwargs)

    async def evaluations(*args: object) -> list[dict[str, Any]]:
        return entries

    async def counters(*args: object) -> dict[str, int]:
        return {"reserved": len(entries), "completed": len(entries), "execution_attempts": 1, "evidence_bytes": 0}

    async def source(*args: object) -> Any:
        return state.source

    async def fence(*args: object) -> dict[str, Any]:
        if state.row["status"] != "running":
            raise StaleAttemptError("study no longer running")
        return copy.deepcopy(state.row)

    async def save(_conn: object, value: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        state.row = copy.deepcopy(value)
        return copy.deepcopy(value)

    async def insert_record(_conn: object, value: object) -> None:
        state.records.append(value)

    def proof(*args: object, **kwargs: object) -> dict[str, str]:
        assert kwargs["source_data"] is data
        assert callable(kwargs["should_stop"]) and callable(kwargs["on_session"])
        state.proof_calls += 1
        if state.before_proof is not None:
            state.before_proof()
        return {"rules_digest": "c" * 64}

    monkeypatch.setattr(approval, "run_sync", asyncio.run)
    monkeypatch.setattr(approval, "with_connection", with_connection)
    monkeypatch.setattr(approval.repo, "evaluations", evaluations)
    monkeypatch.setattr(approval.repo, "counters", counters)
    monkeypatch.setattr(approval.repo, "fence", fence)
    monkeypatch.setattr(approval.repo, "save", save)
    monkeypatch.setattr(approval.saved_runs, "read_result", source)
    monkeypatch.setattr(approval.golden_repository, "insert_record", insert_record)
    monkeypatch.setattr(approval, "build_proof", proof)
    # This fixture isolates source equivalence and atomic publication; retained study
    # reconstruction has replay-backed coverage in test_stages_replay.py.
    monkeypatch.setattr(approval, "verify_recorded_study", lambda *args: None)
    monkeypatch.setattr(approval, "SnapshotRunData", lambda *args: data)
    monkeypatch.setattr(identity, "execution_identity", lambda: protocol.identities)
    evaluator = SimpleNamespace(
        stage_limits=SimpleNamespace(proof_and_materialization=40), snapshot=None, blobs=None,
        check=lambda: False, key=lambda *args: ("selected-evidence", {}), load=lambda *args: state.scored,
        work_progress=lambda count: None,
        cache_hits=0,
    )
    state.run = lambda: approval.approve(copy.deepcopy(row), evaluator)
    return state


def test_approval_publishes_exact_version_and_study_manifest_together(publication: SimpleNamespace) -> None:
    result = publication.run()
    assert result["phase"] == "approved" and result["qualification_status"] == "ready"
    assert len(publication.records) == len(publication.manifests) == 1
    assert publication.records[0].source_run_id == 42
    assert publication.records[0].evidence["selection_count"] == 1
    assert publication.proof_calls == 1


@pytest.mark.parametrize("mismatch", ["coverage", "cash", "pins", "setup"])
def test_source_must_equal_the_scored_final_evidence_before_repeatability_proof(
    publication: SimpleNamespace, mismatch: str,
) -> None:
    source = publication.source
    if mismatch == "coverage":
        publication.source = replace(source, sessions=())
    elif mismatch == "cash":
        session = source.sessions[0]
        publication.source = replace(source, sessions=(replace(session, cash_before_usd=session.cash_before_usd + 1),))
    elif mismatch == "pins":
        session = source.sessions[0]
        publication.source = replace(source, sessions=(replace(session, pins=replace(session.pins, underlying_sha256="d" * 64)),))
    else:
        changed = source.setup.model_copy(update={"starting_cash_usd": source.setup.starting_cash_usd + 1})
        publication.source = replace(source, setup=changed)
    with pytest.raises(ValueError, match="ordinary source"):
        publication.run()
    assert publication.records == publication.manifests == []
    assert publication.proof_calls == 0


def test_manifest_write_failure_rolls_back_the_exact_version_insert(publication: SimpleNamespace) -> None:
    publication.fail_manifest = True
    with pytest.raises(RuntimeError, match="manifest storage failed"):
        publication.run()
    assert publication.records == publication.manifests == []
    assert publication.row["phase"] == "approval"


def test_repeatability_failure_publishes_nothing(publication: SimpleNamespace) -> None:
    def fail() -> None:
        raise ValueError("independent replay failed")

    publication.before_proof = fail
    with pytest.raises(ValueError, match="independent replay failed"):
        publication.run()
    assert publication.records == publication.manifests == []


def test_cancel_or_revision_change_during_proof_cannot_publish(publication: SimpleNamespace) -> None:
    publication.before_proof = lambda: setattr(publication, "row", {**publication.row, "status": "cancelled"})
    with pytest.raises(StaleAttemptError):
        publication.run()
    assert publication.records == publication.manifests == []

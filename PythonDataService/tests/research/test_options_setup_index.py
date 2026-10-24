"""The Options setups index keeps drafts and evidence apart (#3512, PRD #3490 R20, T14).

A saved name addresses a mutable draft. Every run, study and approved version carries the hash of the content it
froze, so a reader can join them by equal content and never by name. These tests work T14 on the rows the index is
built from: two names with identical content share one hash; editing one, deleting one and reusing its name move no
evidence and no approval; and a record that can no longer be read stays listed, with its reason, without a hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.options_backtest.saved_runs import RunRecord
from app.options_backtest.saved_setups import SavedSetup
from app.options_bots.golden_proof import setup_sha256
from app.options_bots.golden_repository import GoldenEvent, GoldenRecord
from app.research import options_setup_index as index
from app.routers import options_backtest as options_backtest_router
from app.schemas.options_setup_index import OptionsSetupIndexResponse
from tests.options_backtest.setup_fixtures import leg, setup

FIXTURES = Path(__file__).parent.parent / "fixtures" / "options_evidence"
PREFIX = "/api/research/options-backtest"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def saved(name: str, **changes: Any) -> SavedSetup:
    return SavedSetup(name=name, example=False, saved_at_ms=1_790_000_000_000, setup=setup(**changes), cannot_open_reason=None)


def the_hash(**changes: Any) -> str:
    return setup_sha256(setup(**changes))


def a_run(run_id: int, *, name: str | None, status: str = "complete") -> RunRecord:
    return RunRecord(
        id=run_id,
        created_at_ms=1_791_800_000_000 + run_id,
        finished_at_ms=1_791_800_000_005 + run_id,
        job_id=f"job-{run_id}",
        status=status,  # type: ignore[arg-type]
        setup_name=name,
        underlying="SPY",
        first_session_open_ms=1_791_379_800_000,
        last_session_open_ms=1_791_379_800_000,
        sessions_requested=1,
        fee_policy_text="No fees are charged.",
        calendar_identity="NYSE",
        calendar_package_version="1",
        failure=None,
        headline=None,
    )


def a_version(**evidence: Any) -> GoldenRecord:
    version = fixture("golden76")
    return GoldenRecord(
        id=version["id"],
        name=version["name"],
        source_run_id=version["source_run_id"],
        setup=version["setup"],
        setup_sha256=version["setup_sha256"],
        rules_digest=version["rules_digest"],
        proof=version["proof"],
        proof_sha256=version["proof_sha256"],
        evidence={**version["evidence"], **evidence},
        note=version["note"],
        approved_by="owner",
        created_at_ms=version["created_at_ms"],
        command_id="command-1",
        command_sha256="c" * 64,
    )


def a_study_row(**changes: Any) -> dict[str, Any]:
    row = fixture("study-record")
    return {
        "id": row["id"],
        "setup_name": row["setup_name"],
        "underlying": row["underlying"],
        "phase": row["phase"],
        "status": row["status"],
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
        "protocol": json.dumps(row["protocol"]),
        "protocol_hash": row["protocol_hash"],
        **changes,
    }


def test_two_names_with_identical_content_share_one_hash_and_neither_owns_the_evidence() -> None:
    listed = {entry.name: entry for entry in index.indexed_setups([saved("Alpha"), saved("Beta")])}

    assert listed["Alpha"].setup_sha256 == listed["Beta"].setup_sha256 == the_hash()
    # A run recorded under Alpha froze the same content: the join is the hash, and the run's label does not move it.
    [run] = index.indexed_runs([a_run(5, name="Alpha")], {5: saved("run 5")})
    assert run.setup_sha256 == listed["Beta"].setup_sha256
    assert run.recorded_name == "Alpha"


def test_editing_deleting_and_reusing_a_name_moves_no_evidence_and_no_approval() -> None:
    frozen = {5: saved("run 5"), 6: saved("run 6", underlying="QQQ")}
    runs_before = index.indexed_runs([a_run(5, name="Alpha"), a_run(6, name=None)], frozen)

    # Alpha is edited, Beta is deleted, and the name Beta is reused for different content.
    after = {entry.name: entry for entry in index.indexed_setups([saved("Alpha", starting_cash_usd="25000"), saved("Beta", underlying="IWM")])}
    runs_after = index.indexed_runs([a_run(5, name="Alpha"), a_run(6, name=None)], frozen)

    assert runs_after == runs_before
    assert all(entry.setup_sha256 not in {run.setup_sha256 for run in runs_after} for entry in after.values())
    # The approval kept the hash it froze, whichever name now holds other content.
    [version] = index.indexed_approvals([a_version()], [], running_digest=None)
    assert version.setup_sha256 == fixture("golden76")["setup_sha256"]
    assert all(entry.setup_sha256 != version.setup_sha256 for entry in after.values())


def test_a_run_the_name_never_labelled_and_an_edited_draft_are_still_listed_with_their_own_content() -> None:
    [unnamed] = index.indexed_runs([a_run(9, name=None)], {9: saved("run 9", legs=[leg(), leg(leg_id="put", right="put")])})

    assert unnamed.recorded_name is None
    assert unnamed.setup_sha256 == the_hash(legs=[leg(), leg(leg_id="put", right="put")]) != the_hash()


def test_a_run_whose_stored_setup_cannot_be_read_is_listed_without_a_hash_and_says_why() -> None:
    broken = SavedSetup(name="run 7", example=False, saved_at_ms=1, setup=None, cannot_open_reason="legs: Field required")

    [run] = index.indexed_runs([a_run(7, name="Gone")], {7: broken})

    assert (run.setup_sha256, run.unreadable_reason, run.recorded_name) == (None, "legs: Field required", "Gone")


def test_a_saved_setup_that_cannot_be_opened_keeps_its_reason_and_no_hash() -> None:
    legacy = SavedSetup(name="Old", example=False, saved_at_ms=3, setup=None, cannot_open_reason="entry: Field required")

    [entry] = index.indexed_setups([legacy])

    assert (entry.setup_sha256, entry.cannot_open_reason, entry.underlying) == (None, "entry: Field required", None)


def test_the_shipped_examples_are_listed_as_examples() -> None:
    from app.options_backtest.saved_setups import EXAMPLE_SETUPS

    listed = index.indexed_setups(EXAMPLE_SETUPS)

    assert [entry.example for entry in listed] == [True] * len(EXAMPLE_SETUPS)
    assert all(entry.setup_sha256 is not None and entry.saved_at_ms is None for entry in listed)


@pytest.mark.parametrize(
    ("evidence", "origin"),
    [
        ({}, "backtest"),
        ({"options_search_study_id": "os_" + "a" * 40}, "not_recorded"),
        (
            {"options_search_study_id": "os_" + "a" * 40, "study_manifest_sha256": "b" * 64, "verdict": "meets_rules"},
            "search",
        ),
    ],
)
def test_an_approval_says_how_it_was_made_as_the_evidence_workspace_does(evidence: dict[str, Any], origin: str) -> None:
    [version] = index.indexed_approvals([a_version(**evidence)], [], running_digest=None)

    assert version.origin == origin
    assert version.study_id == evidence.get("options_search_study_id")
    assert version.source_run_id == 76


def test_a_revoked_approval_stays_listed_as_revoked() -> None:
    record = a_version()
    revoked = GoldenEvent(1, record.id, "revoked", None, None, "no longer wanted", "owner", 1_792_400_000_000, "command-2", "d" * 64)

    [version] = index.indexed_approvals([record], [revoked], running_digest=record.rules_digest)

    assert version.status == "revoked"


def test_a_study_is_identified_by_its_frozen_baseline_setup() -> None:
    [study] = index.indexed_studies([a_study_row()])

    assert study.setup_sha256 is not None and study.unreadable_reason is None
    assert study.recorded_name == fixture("study-record")["setup_name"]


def test_a_study_whose_frozen_protocol_was_changed_stays_listed_and_claims_no_content() -> None:
    row = fixture("study-record")
    row["protocol"]["policy"]["min_sharpe"] = -999

    [study] = index.indexed_studies([a_study_row(protocol=json.dumps(row["protocol"]))])

    assert study.setup_sha256 is None
    assert study.unreadable_reason is not None and "cannot be read" in study.unreadable_reason
    assert study.id == row["id"]


async def test_the_route_serves_the_index_and_says_when_the_store_cannot_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    served = OptionsSetupIndexResponse(
        read_at_ms=1,
        setups=index.indexed_setups([saved("Alpha")]),
        runs=[],
        runs_total=0,
        studies=[],
        studies_total=0,
        approvals=[],
        approvals_total=0,
    )

    async def read() -> OptionsSetupIndexResponse:
        return served

    monkeypatch.setattr(options_backtest_router, "read_index", read)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(f"{PREFIX}/setup-index")

        async def down() -> OptionsSetupIndexResponse:
            raise ConnectionError("no route to the research store")

        monkeypatch.setattr(options_backtest_router, "read_index", down)
        unavailable = await client.get(f"{PREFIX}/setup-index")

    assert ok.status_code == 200
    assert ok.json()["setups"][0]["setup_sha256"] == the_hash()
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "OPTIONS_SETUP_INDEX_UNAVAILABLE"

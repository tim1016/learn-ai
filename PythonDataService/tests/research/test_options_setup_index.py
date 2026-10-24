"""The Options setups index keeps drafts and evidence apart (#3512, PRD #3490 R20, T14).

A saved name addresses a mutable draft. Every run, study and approved version carries the hash of the content it
froze, so a reader can join them by equal content and never by name. These tests work T14 on the rows the index is
built from: two names with identical content share one hash; editing one, deleting one and reusing its name move no
evidence and no approval; and a record that can no longer be read stays listed, with its reason, without a hash.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.options_backtest import saved_runs
from app.options_backtest.saved_runs import RunRecord
from app.options_backtest.saved_setups import SavedSetup
from app.options_bots.golden_proof import setup_sha256
from app.options_bots.golden_repository import GoldenEvent, GoldenRecord
from app.research import options_setup_index as index
from app.routers import options_backtest as options_backtest_router
from app.routers.options_backtest import listed_setup
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
    listed = {entry.name: entry for entry in map(listed_setup, [saved("Alpha"), saved("Beta")])}

    assert listed["Alpha"].setup_sha256 == listed["Beta"].setup_sha256 == the_hash()
    # A run recorded under Alpha froze the same content: the join is the hash, and the run's label does not move it.
    [run] = index.indexed_runs([a_run(5, name="Alpha")], {5: saved("run 5")})
    assert run.setup_sha256 == listed["Beta"].setup_sha256
    assert run.recorded_name == "Alpha"


def test_editing_deleting_and_reusing_a_name_moves_no_evidence_and_no_approval() -> None:
    frozen = {5: saved("run 5"), 6: saved("run 6", underlying="QQQ")}
    runs_before = index.indexed_runs([a_run(5, name="Alpha"), a_run(6, name=None)], frozen)

    # Alpha is edited, Beta is deleted, and the name Beta is reused for different content.
    after = {entry.name: entry for entry in map(listed_setup, [saved("Alpha", starting_cash_usd="25000"), saved("Beta", underlying="IWM")])}
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

    entry = listed_setup(legacy)

    assert (entry.setup_sha256, entry.cannot_open_reason, entry.setup) == (None, "entry: Field required", None)


def test_the_shipped_examples_are_listed_as_examples() -> None:
    from app.options_backtest.saved_setups import EXAMPLE_SETUPS

    listed = [listed_setup(example) for example in EXAMPLE_SETUPS]

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
    # The saved setups are not listed twice: GET /setups lists them, each with its hash.
    assert "setups" not in ok.json()
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "OPTIONS_SETUP_INDEX_UNAVAILABLE"


class _OneApproval:
    """A store that holds one approved version and no events, as the index reads it."""

    async def list(self, *, limit: int) -> tuple[GoldenRecord, ...]:
        return (a_version(),)

    async def count(self) -> int:
        return 1

    async def events_for(self, ids: list[str]) -> tuple[GoldenEvent, ...]:
        return ()


def _spied_on(monkeypatch: pytest.MonkeyPatch, name: str, ran_on: list[int]) -> None:
    """Wraps the pure builder `name` so that the thread it runs on is recorded."""
    real: Callable[..., Any] = getattr(index, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        ran_on.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(index, name, spy)


async def test_the_runs_are_validated_and_hashed_on_a_worker_thread_after_the_connection_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thousand stored setups are validated and hashed per read; that must not hold the data plane's event loop (A-M3)."""
    ran_on: list[int] = []
    _spied_on(monkeypatch, "indexed_runs", ran_on)
    connected = False

    async def with_connection(read: Callable[..., Any], *args: Any) -> Any:
        nonlocal connected
        connected = True
        try:
            return (), {7: (setup().model_dump_json(), 1)}, 1
        finally:
            connected = False

    real_parse = saved_runs.run_setups_of
    held_during_parse: list[bool] = []

    def parse(stored: Any) -> Any:
        held_during_parse.append(connected)
        return real_parse(stored)

    monkeypatch.setattr(index, "with_connection", with_connection)
    monkeypatch.setattr(saved_runs, "run_setups_of", parse)

    runs, total = await index._read_runs()

    assert (runs, total) == ([], 1)
    assert ran_on and ran_on[0] != threading.get_ident()
    assert held_during_parse == [False]


async def test_the_studies_are_parsed_and_hashed_on_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    ran_on: list[int] = []
    _spied_on(monkeypatch, "indexed_studies", ran_on)

    async def with_connection(read: Callable[..., Any], *args: Any) -> Any:
        return [a_study_row()], 1

    monkeypatch.setattr(index, "with_connection", with_connection)

    [study], total = await index._read_studies()

    assert total == 1 and study.setup_sha256 is not None
    assert ran_on and ran_on[0] != threading.get_ident()


async def test_the_approvals_are_judged_on_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    ran_on: list[int] = []
    _spied_on(monkeypatch, "indexed_approvals", ran_on)
    monkeypatch.setattr(index, "PostgresGoldenStore", _OneApproval)

    [version], total = await index._read_approvals()

    assert total == 1 and version.id == a_version().id
    assert ran_on and ran_on[0] != threading.get_ident()


async def test_the_saved_setups_carry_their_content_hash_and_pick_no_approved_version_for_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-M1: GET /setups lists what is saved by exact content (R20) and no longer picks one approval for each name."""
    legacy = SavedSetup(name="Old", example=False, saved_at_ms=3, setup=None, cannot_open_reason="entry: Field required")

    async def with_connection(read: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return [saved("Alpha"), saved("Beta"), saved("Gamma", underlying="QQQ"), legacy]

    monkeypatch.setattr(options_backtest_router, "with_connection", with_connection)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/setups")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()}
    assert rows["Alpha"]["setup_sha256"] == rows["Beta"]["setup_sha256"] == the_hash()
    assert rows["Gamma"]["setup_sha256"] == the_hash(underlying="QQQ") != the_hash()
    assert (rows["Old"]["setup_sha256"], rows["Old"]["cannot_open_reason"], rows["Old"]["setup"]) == (None, "entry: Field required", None)
    assert not any(key.startswith("golden_") for row in rows.values() for key in row)

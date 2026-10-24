"""One recorded study run over REST, and that opening it recomputes nothing (#3520)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

import app.routers.indicator_reliability as reliability_router
import app.routers.jobs as jobs
import app.services.feature_study_service as feature_study_service
from app.main import app
from app.research.study_records.models import STUDY_TYPES, StudyType
from app.research.study_records.store import StudyRecordStore
from app.routers.research_study_records import get_study_records_root
from tests.research.history.test_study_sources import INPUTS, RESULTS, Clock, record_run

BASE = "/api/research/study-records"
T0 = 1_700_000_000_000


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    app.dependency_overrides[get_study_records_root] = lambda: tmp_path
    yield tmp_path
    app.dependency_overrides.pop(get_study_records_root, None)


@pytest.fixture
async def client(root: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def the_ids(root: Path, study: StudyType) -> list[str]:
    return [record_id for record_id, _ in StudyRecordStore(study, root=root).config_paths()]


@pytest.mark.parametrize("study", STUDY_TYPES)
async def test_a_recorded_run_is_served_with_its_inputs_result_and_what_was_derived_from_them(
    client: httpx.AsyncClient, root: Path, clock: Clock, study: StudyType
) -> None:
    record_run(root, study, instant=T0, clock=clock)
    (record_id,) = the_ids(root, study)

    response = await client.get(f"{BASE}/{study}/{record_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["study"] == study and body["record_id"] == record_id and body["schema_version"] == 1
    assert body["status"] == "completed" and body["status_detail"] is None
    assert body["inputs"] == INPUTS[study]
    assert body["result"] == json.loads(json.dumps(RESULTS[study]).replace("NaN", "null"))
    assert body["question"] and body["symbols"]
    assert body["created_at_ms"] == body["completed_at_ms"] == T0
    assert body["rerun_of"] is None and body["data_source"] is None and body["job_id"] is None


async def test_a_failed_and_a_cancelled_run_are_served_with_their_reason_and_no_result(
    client: httpx.AsyncClient, root: Path, clock: Clock
) -> None:
    record_run(root, "signal-study", instant=T0, clock=clock, end="failed")
    record_run(root, "signal-study", instant=T0 + 1, clock=clock, end="cancelled")

    bodies = [(await client.get(f"{BASE}/signal-study/{record_id}")).json() for record_id in the_ids(root, "signal-study")]

    assert sorted((b["status"], b["status_detail"], b["result"]) for b in bodies) == [
        ("cancelled", "Cancelled by the user.", None),
        ("failed", "The provider returned no bars.", None),
    ]


async def test_a_rerun_is_served_with_the_record_it_was_prefilled_from(client: httpx.AsyncClient, root: Path, clock: Clock) -> None:
    record_run(root, "feature-study", instant=T0, clock=clock)
    (first,) = the_ids(root, "feature-study")
    record_run(root, "feature-study", instant=T0 + 1, clock=clock, rerun_of=first)
    (second,) = (i for i in the_ids(root, "feature-study") if i != first)

    body = (await client.get(f"{BASE}/feature-study/{second}")).json()

    assert body["rerun_of"] == {"type": "feature-study", "native_id": first}


async def test_opening_a_record_again_and_again_recomputes_nothing_and_writes_nothing(
    client: httpx.AsyncClient, root: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every entry point of every study's computation fails if called; reading must not reach one."""

    def never(*_: Any, **__: Any) -> None:
        raise AssertionError("opening a stored run computed something")

    for module, name in (
        (jobs, "run_feature_study"),
        (feature_study_service, "run_feature_research"),
        (jobs, "run_signal_study"),
        (jobs, "run_cross_sectional_job"),
        (jobs, "run_in_thread"),
        (reliability_router, "admit_reliability_inputs"),
        (reliability_router, "compute_indicator_reliability_with_oos"),
        (jobs, "run_realized_vs_iv_study"),
    ):
        monkeypatch.setattr(module, name, never)
    monkeypatch.setattr(jobs.polygon_client, "fetch_aggregates", never)
    for study in STUDY_TYPES:
        record_run(root, study, instant=T0, clock=clock)

    def stamp() -> dict[str, tuple[int, int]]:
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*")) if p.is_file()}

    before = stamp()
    first = {study: (await client.get(f"{BASE}/{study}/{the_ids(root, study)[0]}")).json() for study in STUDY_TYPES}
    again = {study: (await client.get(f"{BASE}/{study}/{the_ids(root, study)[0]}")).json() for study in STUDY_TYPES}

    assert first == again and all(body["result"] is not None for body in first.values())
    assert stamp() == before


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"{BASE}/feature-study/{'a' * 32}", id="well-formed id nobody holds"),
        pytest.param(f"{BASE}/feature-study/not-an-id", id="malformed id"),
        pytest.param(f"{BASE}/feature-study/{'A' * 32}", id="upper-case id"),
    ],
)
async def test_a_record_that_does_not_exist_is_a_404_that_names_it(client: httpx.AsyncClient, url: str) -> None:
    response = await client.get(url)

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "STUDY_RECORD_NOT_FOUND"


async def test_an_id_that_climbs_out_of_the_study_directory_reaches_nothing(client: httpx.AsyncClient) -> None:
    for climb in ("..%2F..%2Fsecrets", "%2e%2e/%2e%2e/secrets"):
        assert (await client.get(f"{BASE}/feature-study/{climb}")).status_code == 404


async def test_a_record_is_found_only_under_the_study_that_holds_it(client: httpx.AsyncClient, root: Path, clock: Clock) -> None:
    record_run(root, "feature-study", instant=T0, clock=clock)
    (record_id,) = the_ids(root, "feature-study")

    assert (await client.get(f"{BASE}/signal-study/{record_id}")).status_code == 404
    assert (await client.get(f"{BASE}/feature-study/{record_id}")).status_code == 200


async def test_an_unknown_study_is_refused(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{BASE}/spec-run/{'a' * 32}")).status_code == 422


async def test_a_record_of_a_version_this_build_does_not_know_is_a_500_that_says_so_never_a_partial_record(
    client: httpx.AsyncClient, root: Path, clock: Clock
) -> None:
    record_run(root, "feature-study", instant=T0, clock=clock)
    (record_id,) = the_ids(root, "feature-study")
    config = root / "feature-study" / record_id / "config.json"
    config.write_text(json.dumps({**json.loads(config.read_text(encoding="utf-8")), "schema_version": 2}), encoding="utf-8")

    response = await client.get(f"{BASE}/feature-study/{record_id}")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "STUDY_RECORD_UNREADABLE"
    assert "newer build" in response.json()["detail"]["message"]


async def test_a_record_whose_inputs_this_build_cannot_read_is_a_500_that_says_so(
    client: httpx.AsyncClient, root: Path, clock: Clock
) -> None:
    record_run(root, "feature-study", instant=T0, clock=clock, inputs={"symbol": "SPY"})
    (record_id,) = the_ids(root, "feature-study")

    response = await client.get(f"{BASE}/feature-study/{record_id}")

    assert response.status_code == 500
    assert "inputs are not ones this build understands" in response.json()["detail"]["message"]

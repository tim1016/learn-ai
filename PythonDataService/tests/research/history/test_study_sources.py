"""The five studies' own records, read through the History endpoint (T17, #3520).

Records are made the way a run makes them, through the recorder, under a temporary artifact root, then listed as the
History lists them. The four older file stores are written beside them with their own ``save_*`` functions, so paging,
status and symbol filters are proven across old and new together, and the migration is proven additive: the new
directories change nothing the older stores list, and a build without them lists the same root as before.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.history.artifact_sources import Baselines, MonteCarlos, SpecRuns, WalkForwards
from app.research.history.registry import default_history_sources
from app.research.history.study_sources import study_record_sources
from app.research.study_records.models import STUDY_TYPES, StudyType
from app.research.study_records.recorder import RunCapture, RunRecorder
from app.research.study_records.specs import STUDY_SPECS
from app.research.study_records.store import StudyRecordStore
from app.routers.research_history import get_history_sources
from app.schemas.research_history import HISTORY_TYPES
from tests._helpers.run_ledgers import run_ids
from tests._helpers.study_runs import a_cross_sectional_result, a_feature_result
from tests.research.history.test_artifact_sources import (
    SPY_END,
    SPY_START,
    EmptyLegacyStore,
    a_run,
    save_a_run,
)

URL = "/api/research/history"
T0 = 1_700_000_000_000

DATED = {"symbol": "SPY", "from_date": "2024-01-02", "to_date": "2024-03-28"}
INPUTS: dict[StudyType, dict[str, Any]] = {
    "feature-study": {**DATED, "feature_name": "rsi_14"},
    "signal-study": {**DATED, "feature_name": "rsi_14", "flip_sign": True},
    "cross-sectional-study": {
        "symbols": ["SPY", "QQQ", "IWM"],
        "from_date": "2025-09-02",
        "to_date": "2025-12-01",
        "feature_name": "iv_rank_60",
        "target_type": "directional",
    },
    "indicator-reliability": {**DATED, "indicator_name": "rsi", "indicator_params": {"length": 14}},
    "realized-vs-iv": {
        "symbol": "SPY",
        "bar_size": "1d",
        "tenor_days": 30,
        "window_start_ms": SPY_START,
        "window_end_ms": SPY_END,
        "iv_series_supplied": False,
    },
}
RESULTS: dict[StudyType, dict[str, Any]] = {
    "feature-study": a_feature_result(),
    "signal-study": {"success": True, "bars_used": 9_000, "best_threshold": 1.0, "graduation": {"overall_grade": "B", "status_label": "Research"}},
    "cross-sectional-study": a_cross_sectional_result(),
    "indicator-reliability": {"success": True, "best_horizon": 5, "results": [{"horizon": 5, "is_mean_ic": 0.02, "oos_mean_ic": 0.01}]},
    "realized-vs-iv": {"coverage": {"n_bars": 60, "forward_nan_bars": 21}, "iv_source": "absent"},
}


class Clock:
    """Makes ``RunRecorder`` stamp the instants a test chooses, so records can share an instant."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.research.study_records.recorder as recorder

        self._now = T0
        monkeypatch.setattr(recorder, "now_ms_utc", lambda: self._now)

    def at(self, instant: int) -> None:
        self._now = instant


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def record_run(
    root: Path,
    study: StudyType,
    *,
    instant: int,
    clock: Clock,
    end: str = "completed",
    inputs: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    rerun_of: str | None = None,
) -> None:
    """One run of ``study`` that ends as ``end`` ("completed", "failed" or "cancelled"), recorded as a run records itself."""
    clock.at(instant)
    run = RunRecorder(study, None, lambda _: RunCapture(inputs=inputs or INPUTS[study], rerun_of=rerun_of), root=root)
    if end == "failed":
        problem = run.fail("The provider returned no bars.")
    elif end == "cancelled":
        problem = run.cancel("Cancelled by the user.")
    else:
        problem = run.deliver(result or RESULTS[study])
    assert problem is None, problem


def file_stores(root: Path) -> list[Any]:
    """Every file-backed store, new and old, with the two database stores stood in for by empty ones."""
    return [
        SpecRuns(root),
        WalkForwards(root),
        MonteCarlos(root),
        Baselines(root),
        *study_record_sources(root),
        EmptyLegacyStore("signal-report"),
        EmptyLegacyStore("feature-experiment"),
    ]


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[None]:
    app.dependency_overrides[get_history_sources] = lambda: file_stores(tmp_path)
    yield
    app.dependency_overrides.pop(get_history_sources, None)


@pytest.fixture
async def client(stores: None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def every_page(client: httpx.AsyncClient, page_size: int, **params: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first = (await client.get(URL, params={**params, "page": 1, "page_size": page_size})).json()
    entries, page = list(first["entries"]), 1
    while len(entries) < first["total"]:
        page += 1
        body = (await client.get(URL, params={**params, "page": page, "page_size": page_size})).json()
        assert body["entries"], f"page {page} is empty before the count of {first['total']} is reached"
        entries.extend(body["entries"])
    return entries, first


# ── After a run ─────────────────────────────────────────────────────────────────────────────────


async def test_every_study_appears_in_the_history_after_a_run_including_failed_and_cancelled_ones(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    for n, study in enumerate(STUDY_TYPES):
        for k, end in enumerate(("completed", "failed", "cancelled")):
            record_run(tmp_path, study, instant=T0 + 10 * n + k, clock=clock, end=end)

    entries, first = await every_page(client, 100)

    assert first["total"] == 15 and first["partial"] is False
    assert {(e["type"], e["status"]) for e in entries} == {(s, end) for s in STUDY_TYPES for end in ("completed", "failed", "cancelled")}
    by_type = {s["type"]: s for s in first["sources"]}
    assert all(by_type[study]["state"] == "read" and by_type[study]["total"] == 3 for study in STUDY_TYPES)
    failed = next(e for e in entries if e["type"] == "feature-study" and e["status"] == "failed")
    assert failed["status_detail"] == "The provider returned no bars." and failed["headline"] is None
    cancelled = next(e for e in entries if e["type"] == "feature-study" and e["status"] == "cancelled")
    assert cancelled["status_detail"] == "Cancelled by the user."


async def test_a_study_run_is_listed_with_the_question_instruments_window_and_headline_its_inputs_and_result_give(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    record_run(tmp_path, "cross-sectional-study", instant=T0, clock=clock)
    record_run(tmp_path, "realized-vs-iv", instant=T0 + 1, clock=clock)

    body = (await client.get(URL, params={"page_size": 100})).json()

    universe = next(e for e in body["entries"] if e["type"] == "cross-sectional-study")
    assert universe["question"] == "Does iv_rank_60 predict the next day's direction across 3 instruments?"
    assert universe["symbols"] == ["SPY", "QQQ", "IWM"]
    assert universe["headline"] == "Aggregate IC 0.0310; 1 of 3 evaluated members passed (3 asked); stage 1 (Weak)"
    assert universe["created_at_ms"] == T0 and universe["completed_at_ms"] == T0
    assert universe["provenance"] is None, "a run whose result carried no provenance block reads as not recorded"
    bars = next(e for e in body["entries"] if e["type"] == "realized-vs-iv")
    assert bars["window"] == {"start_ms": SPY_START, "end_ms": SPY_END}
    assert bars["headline"] == "60 bars; IV from none, so realized volatility alone; 21 without a forward window"


async def test_a_run_that_named_no_window_shows_the_window_its_data_source_says_it_read(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    named = {key: value for key, value in INPUTS["realized-vs-iv"].items() if key not in ("window_start_ms", "window_end_ms")}
    read = {"effective_window": {"start_ms": SPY_START + 86_400_000, "end_ms": SPY_END - 86_400_000}}
    record_run(tmp_path, "realized-vs-iv", instant=T0, clock=clock, inputs=named, result={**RESULTS["realized-vs-iv"], "data_source": read})
    record_run(tmp_path, "realized-vs-iv", instant=T0 + 1, clock=clock, inputs=named)

    body = (await client.get(URL, params={"type": "realized-vs-iv"})).json()

    windows = {e["created_at_ms"]: e["window"] for e in body["entries"]}
    assert windows[T0] == {"start_ms": SPY_START + 86_400_000, "end_ms": SPY_END - 86_400_000}
    assert windows[T0 + 1] is None, "a run with neither a named window nor a source that read one has no window to show"


async def test_a_listing_reads_only_the_small_config_never_a_result(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    record_run(tmp_path, "feature-study", instant=T0, clock=clock)
    for result_file in (tmp_path / "feature-study").glob("*/result.json"):
        result_file.write_text("{ not json", encoding="utf-8")

    body = (await client.get(URL)).json()

    assert [(e["type"], e["status"]) for e in body["entries"]] == [("feature-study", "completed")]
    assert body["partial"] is False


# ── Paging and status across the old stores and the new ─────────────────────────────────────────


@pytest.mark.parametrize("page_size", [1, 4, 7, 100])
async def test_pages_across_the_old_stores_and_the_new_hold_every_record_once_in_the_shared_order(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock, page_size: int
) -> None:
    instants = [T0 + 1_000 * (i // 3) for i in range(18)]  # six instants, three records each
    for i, instant in enumerate(instants):
        study = STUDY_TYPES[i % len(STUDY_TYPES)]
        record_run(tmp_path, study, instant=instant, clock=clock, end=("completed", "failed", "cancelled")[i % 3])
    for i in range(6):
        save_a_run(tmp_path, a_run(100 + i, "SPY", T0 + 1_000 * i))

    entries, first = await every_page(client, page_size)

    keys = [(e["type"], e["native_id"]) for e in entries]
    assert len(entries) == first["total"] == 24
    assert len(set(keys)) == 24, "a record appeared on two pages"
    assert keys == [(e["type"], e["native_id"]) for e in sorted(entries, key=lambda e: (-e["created_at_ms"], e["type"], e["native_id"]))]


async def test_the_status_filter_finds_partial_failed_and_cancelled_study_runs_and_agrees_with_its_count(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    record_run(tmp_path, "cross-sectional-study", instant=T0, clock=clock, result=a_cross_sectional_result(asked=3, evaluated=2))
    record_run(tmp_path, "cross-sectional-study", instant=T0 + 1, clock=clock)
    record_run(tmp_path, "signal-study", instant=T0 + 2, clock=clock, end="failed")
    record_run(tmp_path, "feature-study", instant=T0 + 3, clock=clock, end="cancelled")
    record_run(tmp_path, "feature-study", instant=T0 + 4, clock=clock, end="cancelled")

    partial = (await client.get(URL, params={"status": "partial"})).json()
    cancelled, _ = await every_page(client, 1, status="cancelled")
    completed = (await client.get(URL, params={"status": "completed", "type": "cross-sectional-study"})).json()

    assert [(e["type"], e["status_detail"]) for e in partial["entries"]] == [
        ("cross-sectional-study", "2 of 3 members evaluated: 1 errored")
    ]
    assert partial["total"] == 1
    assert len(cancelled) == 2 and {e["type"] for e in cancelled} == {"feature-study"}
    assert completed["total"] == 1


async def test_the_symbol_filter_reaches_a_universe_member_and_offers_every_symbol_the_new_stores_hold(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    record_run(tmp_path, "cross-sectional-study", instant=T0, clock=clock)
    record_run(tmp_path, "feature-study", instant=T0 + 1, clock=clock, inputs={**INPUTS["feature-study"], "symbol": "aapl"})

    iwm = (await client.get(URL, params={"symbol": "iwm"})).json()
    aapl = (await client.get(URL, params={"symbol": "AAPL"})).json()

    assert [e["type"] for e in iwm["entries"]] == ["cross-sectional-study"]
    assert [e["type"] for e in aapl["entries"]] == ["feature-study"]
    assert aapl["symbols"] == ["AAPL", "IWM", "QQQ", "SPY"]


async def test_a_rerun_lists_the_record_it_was_prefilled_from_as_its_parent_and_the_original_stays(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    record_run(tmp_path, "feature-study", instant=T0, clock=clock)
    original = next(iter(StudyRecordStore("feature-study", root=tmp_path).config_paths()))[0]
    record_run(tmp_path, "feature-study", instant=T0 + 1, clock=clock, rerun_of=original)

    body = (await client.get(URL, params={"page_size": 100})).json()

    rerun, first = body["entries"]
    assert rerun["parent"] == {"type": "feature-study", "native_id": original}
    assert first["native_id"] == original and first["parent"] is None


# ── The migration is additive ───────────────────────────────────────────────────────────────────


async def test_adding_the_study_records_changes_nothing_the_older_stores_list(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    save_a_run(tmp_path, a_run(1, "SPY", T0))
    save_a_run(tmp_path, a_run(2, "QQQ", T0 + 1, status="failed"))
    older = (await client.get(URL, params={"page_size": 100})).json()
    runs_before = run_ids(tmp_path)

    for study in STUDY_TYPES:
        record_run(tmp_path, study, instant=T0 + 10, clock=clock)
    with caplog.at_level(logging.WARNING):
        later = (await client.get(URL, params={"page_size": 100})).json()
        runs_after = run_ids(tmp_path)

    assert [e for e in later["entries"] if e["type"] in {"spec-run"}] == older["entries"]
    assert runs_after == runs_before
    assert not [r for r in caplog.records if "skipping" in r.getMessage()], "the runs store did not mistake a study directory for debris"


async def test_a_build_without_the_study_sources_lists_the_same_root_exactly_as_before(tmp_path: Path, clock: Clock) -> None:
    """Rolling back removes the sources; the directories stay, and nothing of the older stores notices them."""
    save_a_run(tmp_path, a_run(1, "SPY", T0))
    for study in STUDY_TYPES:
        record_run(tmp_path, study, instant=T0 + 10, clock=clock)
    rolled_back = [*default_history_sources(tmp_path)[:4], EmptyLegacyStore("signal-report"), EmptyLegacyStore("feature-experiment")]
    app.dependency_overrides[get_history_sources] = lambda: rolled_back
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            body = (await http.get(URL, params={"page_size": 100})).json()
    finally:
        app.dependency_overrides.pop(get_history_sources, None)

    assert body["total"] == 1 and body["partial"] is False
    assert {s["unreadable"] for s in body["sources"]} == {0}
    assert [e["type"] for e in body["entries"]] == ["spec-run"]


async def test_a_record_a_newer_build_wrote_is_counted_as_unreadable_not_dropped_and_an_added_field_stays_readable(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock
) -> None:
    for n in range(3):
        record_run(tmp_path, "feature-study", instant=T0 + n, clock=clock)
    configs = sorted((tmp_path / "feature-study").glob("*/config.json"))
    newer, with_extra = configs[0], configs[1]
    document = json.loads(newer.read_text(encoding="utf-8"))
    newer.write_text(json.dumps({**document, "schema_version": 2}), encoding="utf-8")
    document = json.loads(with_extra.read_text(encoding="utf-8"))
    with_extra.write_text(json.dumps({**document, "a_field_a_later_build_added": {"x": 1}}), encoding="utf-8")

    body = (await client.get(URL, params={"type": "feature-study"})).json()

    assert body["total"] == 2, "the record of a version this build does not know is in neither the count nor the entries"
    assert body["partial"] is True
    assert {s["type"]: s["unreadable"] for s in body["sources"]}["feature-study"] == 1


# ── Registry ────────────────────────────────────────────────────────────────────────────────────


def test_every_study_type_has_a_spec_and_every_study_type_is_a_history_type() -> None:
    assert set(STUDY_SPECS) == set(STUDY_TYPES)
    assert set(STUDY_TYPES) <= set(HISTORY_TYPES)


def test_the_registry_registers_each_study_store_once_after_the_four_file_stores() -> None:
    registered = [source.type for source in default_history_sources()]

    assert registered[:4] == ["spec-run", "walk-forward", "monte-carlo", "baseline"]
    assert registered[4:9] == list(STUDY_TYPES)

"""The Realized vs IV job over admitted lake inputs: refusals, cache identity and saved results (#3525; T05, T07, T09).

The job runs through the real runner, emitter and result cache over the job-store fake; the lake under it is the real
pipeline (vendor HTTP, ``ensure_data``, the lake writer, admission) as in ``tests/research/test_study_inputs.py``. The
live IV30 marker is a separate browser call and is not part of the job: nothing here mocks it, and a call to it
would fail the vendor mock.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, get_args

import pytest
import respx
from pydantic import ValidationError

from app.data_lake import run_materialization
from app.data_lake.adjustment_versions import companion_path
from app.engine.edge.realized_vs_iv_series import ESTIMATOR_FNS
from app.jobs import cache as result_cache
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.study_records.captures import capture_realized_vs_iv
from app.research.study_records.models import StudyRecordConfig, StudyRecordResult
from app.research.study_records.recorder import keeps_job_record
from app.research.study_records.store import StudyRecordStore
from app.routers.jobs import RealizedVsIvJobRequest, start_realized_vs_iv_job
from app.services import realized_vs_iv_study_service
from app.services.iv_recorder import InMemoryIvSnapshotStore, RecordedIvSnapshot, get_iv_store, set_iv_store
from app.services.realized_vs_iv_study_service import (
    CACHE_NAMESPACE,
    RealizedVsIvParams,
    resolve_window,
    run_realized_vs_iv_study,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day, lake_file
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests.jobs.conftest import _FakeRedis

WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 5, 3))
NOW_MS = 1_735_689_600_000  # 2025-01-01


class _Harness:
    def __init__(self, fake: _FakeRedis, catalog: FakeCatalog, lake: Path, tape: dict[date, list[VendorBar]]) -> None:
        self.fake, self.catalog, self.lake, self.tape = fake, catalog, lake, tape
        self.computed = 0

    def run(self, params: RealizedVsIvParams, *, job_id: str, force: bool = False) -> dict[str, str]:
        """One job through the real runner: its state hash after the worker thread has finished."""

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_realized_vs_iv_study(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=NOW_MS)

        run_in_thread(job_id, work, thread_name=f"test-{job_id}").join(timeout=120)
        return self.fake.hashes[f"job:{job_id}:state"]

    def run_recorded(
        self, params: RealizedVsIvParams, *, job_id: str, root: Path, force: bool = False, rerun_of: str | None = None
    ) -> dict[str, str]:
        """One job as the route starts it: the study's work wrapped to keep a History record under ``root``."""
        window = {} if params.window is None else {
            "window_start_ms": et_midnight_ms(params.window[0]), "window_end_ms": et_midnight_ms(params.window[1])
        }
        request = RealizedVsIvJobRequest(
            job_id=job_id, symbol=params.symbol, bar_size=params.bar_size, tenor_days=params.tenor_days,
            hf_session=params.hf_session, estimators=list(params.estimators), windows=list(params.windows),
            iv_series=params.iv_series, force=force, rerun_of=rerun_of, **window,
        )

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_realized_vs_iv_study(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=NOW_MS)

        recorded_work = keeps_job_record("realized-vs-iv", request, work, root=root, capture=capture_realized_vs_iv)
        run_in_thread(job_id, recorded_work, thread_name=f"test-{job_id}").join(timeout=120)
        return self.fake.hashes[f"job:{job_id}:state"]

    def result(self, job_id: str) -> dict:
        return json.loads(self.fake.strings[f"job:{job_id}:result"])

    def cache_keys(self) -> list[str]:
        return [key for key in self.fake.strings if key.startswith("result:")]


def params(**overrides: Any) -> RealizedVsIvParams:
    base: dict[str, Any] = {"symbol": "SPY", "bar_size": "1d", "window": (WINDOW[0], WINDOW[-1])}
    return RealizedVsIvParams(**{**base, **overrides})


@pytest.fixture
def recorder_store():
    new = InMemoryIvSnapshotStore()
    original = get_iv_store()
    set_iv_store(new)
    try:
        yield new
    finally:
        set_iv_store(original)


@pytest.fixture
def harness(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder_store):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    tape = vendor_history(WINDOW, seed=11, start_price=150.0, regular_keep=0.995, extended_keep=0.92)
    harness_ = _Harness(fake, catalog, lake, tape)
    real = realized_vs_iv_study_service.compute_realized_vs_iv_series

    def counting(*args: Any, **kwargs: Any):
        harness_.computed += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(realized_vs_iv_study_service, "compute_realized_vs_iv_series", counting)
    with respx.mock:
        mock_launcher()
        mock_vendor("SPY", tape, adjusted=tape)
        yield harness_


def repair_day(harness: _Harness) -> None:
    """The lake repairs one session in place: the same request now reads different, committed bytes."""
    commit_bars(
        harness.catalog, harness.lake, WINDOW[3], corrected_day(WINDOW[3], harness.tape[WINDOW[3]]),
        mode="polygon_split_adjusted",
    )


def recorded(day: date, iv: float = 0.21) -> RecordedIvSnapshot:
    return RecordedIvSnapshot(
        ticker="SPY", snapshot_ts_ms=et_midnight_ms(day) + 9 * 3_600_000 + 35 * 60_000, slot="09:35", spot=100.0, rate=0.04,
        dividend_yield=0.01, rate_source="t", dividend_source="t", iv30_vix_style=iv, iv30_parametric=None,
        iv_provenance={}, raw_chain=[],
    )


# ---------------------------------------------------------------------------
# A completed study reports where its data came from
# ---------------------------------------------------------------------------


def test_a_completed_study_carries_a_lake_data_source_with_its_coverage_counts(harness):
    state = harness.run(params(), job_id="done")

    result = harness.result("done")
    source = result["data_source"]
    assert state["status"] == "completed" and result["success"] is True
    assert source["kind"] == "lake"
    assert source["session_policy"] == "regular", "a day bar is folded from the regular session, as the vendor's day bar is"
    assert (source["bar_minutes"], source["bar_size"], source["warmup_sessions"]) == (1440, "1d", 0)
    assert source["price_adjustment_mode"] == "polygon_split_adjusted"
    assert source["coverage"]["window"] == {
        "expected_sessions": len(WINDOW), "reused": 0, "added": len(WINDOW), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "captured_before_final": 0, "unavailable_from_provider": 0,
    }
    assert len(source["snapshot_digest"]) == 64 and len(source["identity_digest"]) == 64
    assert source["bars_used"] == len(result["ts"]) == len(WINDOW)
    assert source["cache"] == {"hit": False, "cached_at_ms": None}
    assert source["window_start_ms"] == et_midnight_ms(WINDOW[0]) and source["window_end_ms"] == et_midnight_ms(WINDOW[-1])
    assert (source["iv_source"], source["iv_snapshots"], source["iv_alignment_rule"]) == ("absent", 0, "snapshot_in_bar_latest")
    assert (result["bar_size"], result["tenor_days"], result["hf_session"]) == ("1d", 30, None)


def test_the_default_window_holds_the_273_daily_bars_a_z_score_needs_and_is_read_through_the_lake(harness, monkeypatch):
    first, last = resolve_window("1d", "regular", NOW_MS)
    year = expected_sessions(first, last)
    harness.tape = vendor_history(year, seed=5, start_price=100.0, regular_keep=0.99, extended_keep=0.2)
    respx.mock.reset()
    mock_launcher()
    mock_vendor("SPY", harness.tape, adjusted=harness.tape)

    state = harness.run(params(window=None), job_id="year")

    source = harness.result("year")["data_source"]
    assert state["status"] == "completed"
    # 2025-01-01 00:00 UTC is 19:00 ET on 2024-12-31: that session's extended tape is final from 20:20 ET, so the last
    # session the study can read or capture is the day before, and 273 sessions end there (252 with a forward value
    # after the 21 that have none).
    assert (first, last) == (date(2023, 11, 29), date(2024, 12, 30))
    assert len(year) == 273
    assert source["window_start_ms"] == et_midnight_ms(first) and source["window_end_ms"] == et_midnight_ms(last)
    assert source["effective_window"] == {"start_ms": et_midnight_ms(year[0]), "end_ms": et_midnight_ms(year[-1])}
    assert source["coverage"]["window"]["expected_sessions"] == len(year)


# ---------------------------------------------------------------------------
# T05: held, one gap, cold, corrupt adjustment and the daily rollup; a failed admission refuses
# ---------------------------------------------------------------------------


def test_held_sessions_are_reused_and_a_gap_is_the_only_thing_fetched(harness):
    harness.run(params(window=(WINDOW[0], WINDOW[9])), job_id="first-half")
    harness.run(params(window=(WINDOW[11], WINDOW[-1])), job_id="second-half")

    state = harness.run(params(), job_id="all")

    window = harness.result("all")["data_source"]["coverage"]["window"]
    assert state["status"] == "completed"
    assert (window["reused"], window["added"], window["missing"], window["failed"]) == (len(WINDOW) - 1, 1, 0, 0)


def test_a_window_the_lake_cannot_admit_fails_the_job_with_a_typed_refusal_and_the_counts(harness):
    missing_day = WINDOW[4]
    del harness.tape[missing_day]

    state = harness.run(params(), job_id="refused")

    refusal = harness.result("refused")
    assert state["status"] == "failed"
    assert state["error_code"] == "data_unavailable_from_provider"
    assert missing_day.isoformat() in state["error_message"]
    assert refusal["success"] is False and refusal["status"] == "refused" and refusal["symbol"] == "SPY"
    assert refusal["refusal"]["code"] == "data_unavailable_from_provider"
    assert [(s["outcome"], s["group"]) for s in refusal["refusal"]["sessions"]] == [("unavailable_from_provider", "window")]
    window = refusal["data_source"]["coverage"]["window"]
    assert (window["expected_sessions"], window["added"], window["failed"]) == (len(WINDOW), len(WINDOW) - 1, 1)
    assert refusal["data_source"]["snapshot_digest"] is None, "nothing was admitted, so nothing is fingerprinted"
    assert harness.computed == 0, "no number is computed on a shorter series"
    assert harness.cache_keys() == [], "a refusal is never cached"


def test_a_session_still_forming_refuses_with_its_own_code_and_computes_nothing(harness):
    still_forming = params(window=(WINDOW[0], date(2025, 1, 2)))

    state = harness.run(still_forming, job_id="forming")

    assert state["status"] == "failed" and state["error_code"] == "window_not_complete"
    assert "2025-01-02" in state["error_message"]
    assert harness.computed == 0 and harness.cache_keys() == []


@pytest.mark.parametrize("bar_size", ["5m", "1h", "30m", "1D"])
def test_a_bar_size_the_study_was_never_defined_for_is_refused_before_the_lake_is_touched(harness, bar_size):
    """Before #3525 the page offered 5m and 1h and the series route silently ran them as daily bars."""
    state = harness.run(params(bar_size=bar_size), job_id="bad-size")

    refusal = harness.result("bad-size")
    assert state["status"] == "failed" and state["error_code"] == "resolution_unsupported"
    assert "15m and 1d" in state["error_message"] and bar_size in state["error_message"]
    assert refusal["refusal"]["coverage"] is None and refusal["data_source"] is None
    assert harness.catalog.rows == {}, "a request that can never run costs the lake nothing"
    assert harness.computed == 0


def test_a_bad_adjustment_record_is_rebuilt_and_counted_as_added(harness):
    harness.run(params(), job_id="first")
    record = companion_path(lake_file(harness.lake, "polygon_split_adjusted", WINDOW[5]))
    record.write_text(record.read_text().replace('"corporate_action_version": "', '"corporate_action_version": "0'))

    state = harness.run(params(), job_id="rebuilt", force=True)

    window = harness.result("rebuilt")["data_source"]["coverage"]["window"]
    assert state["status"] == "completed"
    assert (window["reused"], window["added"]) == (len(WINDOW) - 1, 1), "only the session whose record failed is fetched again"


def test_a_daily_study_reads_minute_artifacts_and_neither_waits_on_nor_builds_the_daily_rollup(harness):
    narrow = run_materialization._build_engine_run_spec(symbol="SPY", start=WINDOW[0], end=WINDOW[1], requester="test")
    asyncio.run(run_materialization._materialize_run_data(narrow, resolution="minute"))
    rollups = lambda: [r for r in harness.catalog.rows.values() if r["resolution"] == "daily"]  # noqa: E731
    rollup_before = [dict(r) for r in rollups()]

    state = harness.run(params(), job_id="rollup")

    window = harness.result("rollup")["data_source"]["coverage"]["window"]
    assert state["status"] == "completed" and window["failed"] == 0
    assert [dict(r) for r in rollups()] == rollup_before, "the daily bars are folded from minutes; the rollup was left as it was"


# ---------------------------------------------------------------------------
# The History record: a lake run is kept with the source it read, a refused run with its refusal
# ---------------------------------------------------------------------------


def saved_records(root: Path) -> list[tuple[StudyRecordConfig, StudyRecordResult]]:
    store = StudyRecordStore("realized-vs-iv", root=root)
    return [store.load(record_id) for record_id, _ in store.config_paths()]


def test_a_completed_run_is_recorded_with_the_data_source_block_it_returned_and_the_series_its_page_draws(harness, tmp_path):
    state = harness.run_recorded(params(), job_id="kept", root=tmp_path / "records")

    result = harness.result("kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert state["status"] == "completed" and config.status == "completed"
    assert config.data_source == result["data_source"], "the lake block is the record's provenance, field for field"
    assert config.data_source["kind"] == "lake" and len(config.data_source["snapshot_digest"]) == 64
    assert config.headline is not None and config.headline.startswith(f"{len(WINDOW)} bars; IV from none, so realized")
    assert stored.result["ts"] == result["ts"] and stored.result["bars"] == result["bars"], "the chart's bars are stored with the series"
    assert config.inputs["symbol"] == "SPY" and config.inputs["bar_size"] == "1d" and config.inputs["tenor_days"] == 30
    assert config.inputs["window_start_ms"] == et_midnight_ms(WINDOW[0]) and config.inputs["window_end_ms"] == et_midnight_ms(WINDOW[-1])
    assert config.job_id == "kept" and config.data_identity is None


def test_a_caller_supplied_iv_series_is_recorded_as_supplied_and_never_copied_into_the_inputs(harness, tmp_path):
    points = [{"ts": et_midnight_ms(WINDOW[10]) + 9 * 3_600_000, "iv30": 0.25}]

    harness.run_recorded(params(iv_series=points), job_id="with-iv", root=tmp_path / "records")
    harness.run_recorded(params(), job_id="without-iv", root=tmp_path / "records")

    flags = {config.job_id: config.inputs for config, _ in saved_records(tmp_path / "records")}
    assert flags["with-iv"]["iv_series_supplied"] is True and flags["without-iv"]["iv_series_supplied"] is False
    assert all("iv_series" not in inputs for inputs in flags.values()), "the record holds the count and digest in its data_source"
    assert harness.result("with-iv")["data_source"]["iv_snapshots"] == 1


def test_a_window_the_lake_refused_is_recorded_as_a_failed_run_with_its_refusal_and_counts(harness, tmp_path):
    del harness.tape[WINDOW[4]]

    state = harness.run_recorded(params(), job_id="refused-kept", root=tmp_path / "records")

    parked = harness.result("refused-kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert state["status"] == "failed" and state["error_code"] == "data_unavailable_from_provider"
    assert config.status == "failed" and config.status_detail == parked["error"]
    assert config.headline is None
    assert stored.result == parked, "the record holds the refusal the page reads, as the job parked it"
    assert stored.result["refusal"]["code"] == "data_unavailable_from_provider"
    assert config.data_source == parked["data_source"] and config.data_source["coverage"]["window"]["failed"] == 1


def test_a_bar_size_the_study_was_never_defined_for_is_recorded_as_a_failed_run_before_anything_was_read(harness, tmp_path):
    harness.run_recorded(params(bar_size="5m"), job_id="bad-size-kept", root=tmp_path / "records")

    [(config, stored)] = saved_records(tmp_path / "records")
    assert config.status == "failed" and config.inputs["bar_size"] == "5m", "the record says what was asked"
    assert stored.result["refusal"]["code"] == "resolution_unsupported" and config.data_source is None


def test_a_repeat_served_from_the_cache_is_not_recorded_a_second_time(harness, tmp_path):
    harness.run_recorded(params(), job_id="first-kept", root=tmp_path / "records")

    state = harness.run_recorded(params(), job_id="repeat-kept", root=tmp_path / "records")

    assert state["cached"] == "1"
    assert [config.job_id for config, _ in saved_records(tmp_path / "records")] == ["first-kept"]


def test_a_rerun_names_the_record_it_was_prefilled_from(harness, tmp_path):
    harness.run_recorded(params(), job_id="first-kept", root=tmp_path / "records")
    [(first, _)] = saved_records(tmp_path / "records")

    harness.run_recorded(params(tenor_days=60), job_id="again-kept", root=tmp_path / "records", rerun_of=first.record_id)

    again = [config for config, _ in saved_records(tmp_path / "records") if config.job_id == "again-kept"]
    assert [config.rerun_of for config in again] == [first.record_id]


# ---------------------------------------------------------------------------
# T09: cache identity
# ---------------------------------------------------------------------------


def test_a_cache_entry_under_the_bare_job_type_is_neither_returned_nor_relabelled(harness):
    """This study had no result cache before #3525. Whatever a provider-era run might have filed under the bare job
    type is never read by a lake request and is left exactly as it was written."""
    poisoned = {"success": True, "symbol": "SPY", "from": "the provider"}
    old_hash = result_cache.store("realized_vs_iv", {"symbol": "SPY", "bar_size": "1d"}, poisoned)
    old_key = f"result:realized_vs_iv:{old_hash}"
    seeded = harness.fake.strings[old_key]

    state = harness.run(params(), job_id="fresh")

    result = harness.result("fresh")
    assert state["status"] == "completed" and state.get("cached") != "1"
    assert "from" not in result and result["data_source"]["cache"]["hit"] is False
    assert harness.fake.strings[old_key] == seeded
    assert harness.computed == 1
    assert any(key.startswith(f"result:{CACHE_NAMESPACE}:") for key in harness.cache_keys())


def test_an_identical_repeat_is_served_from_the_cache_without_recomputing(harness):
    harness.run(params(), job_id="first")

    state = harness.run(params(), job_id="second")

    first, second = harness.result("first"), harness.result("second")
    assert state["cached"] == "1" and state["status"] == "completed"
    assert harness.computed == 1
    assert {k: v for k, v in second.items() if k != "data_source"} == {k: v for k, v in first.items() if k != "data_source"}
    assert second["data_source"]["cache"]["hit"] is True and second["data_source"]["cache"]["cached_at_ms"] > 0
    assert second["data_source"]["coverage"]["window"]["reused"] == len(WINDOW), "the repeat reports what admission did this time"
    assert second["data_source"]["snapshot_digest"] == first["data_source"]["snapshot_digest"]


def test_force_recomputes_even_when_the_identity_matches(harness):
    harness.run(params(), job_id="first")

    state = harness.run(params(), job_id="forced", force=True)

    assert state.get("cached") != "1" and harness.computed == 2


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"session_policy": "extended"}, id="session_policy"),
        pytest.param({"tenor_days": 60}, id="tenor"),
        pytest.param({"windows": (5, 10)}, id="windows"),
        pytest.param({"window": (WINDOW[0], WINDOW[-2])}, id="window_end"),
    ],
)
def test_a_changed_policy_or_scientific_input_is_a_new_identity_not_a_cache_hit(harness, change):
    harness.run(params(), job_id="first")

    state = harness.run(params(**change), job_id="changed")

    assert state.get("cached") != "1" and harness.computed == 2


def test_repaired_lake_data_under_the_same_request_is_a_new_identity(harness):
    harness.run(params(), job_id="before")
    repair_day(harness)

    state = harness.run(params(), job_id="after")

    before, after = harness.result("before"), harness.result("after")
    assert state.get("cached") != "1" and harness.computed == 2
    assert after["data_source"]["snapshot_digest"] != before["data_source"]["snapshot_digest"]
    assert after["data_source"]["identity_digest"] == before["data_source"]["identity_digest"], "same request, new bytes"
    assert after["data_source"]["coverage"]["window"]["reused"] == len(WINDOW)


def test_a_new_recorder_snapshot_is_a_new_identity_because_recorded_iv_is_an_input(harness, recorder_store):
    recorder_store.write(recorded(WINDOW[10]))
    harness.run(params(), job_id="before")
    recorder_store.write(recorded(WINDOW[11], 0.24))

    state = harness.run(params(), job_id="after")

    before, after = harness.result("before"), harness.result("after")
    assert state.get("cached") != "1" and harness.computed == 2
    assert (before["data_source"]["iv_snapshots"], after["data_source"]["iv_snapshots"]) == (1, 2)
    assert before["data_source"]["iv_digest"] != after["data_source"]["iv_digest"]
    assert before["data_source"]["snapshot_digest"] == after["data_source"]["snapshot_digest"], "the bars did not change"
    assert [v is not None for v in after["iv30"]].count(True) == 2


# ---------------------------------------------------------------------------
# T07: this study has no saved or locked result of its own
# ---------------------------------------------------------------------------
#
# A job's result key is written once by the worker that computed it and nothing rewrites it, so "a saved result is
# unchanged after the lake is repaired" is true of any job and no test of it can fail. The study has no saved or
# locked surface (the page reloads to idle). What can change is the result cache: a repaired lake must add a new entry
# and leave the old one exactly as it was.


def test_a_repaired_lake_adds_a_cache_entry_and_leaves_the_earlier_one_byte_for_byte(harness):
    harness.run(params(), job_id="saved")
    saved_source = harness.result("saved")["data_source"]
    cached_before = {key: harness.fake.strings[key] for key in harness.cache_keys()}

    repair_day(harness)
    harness.run(params(), job_id="rerun")

    assert harness.result("rerun")["data_source"]["snapshot_digest"] != saved_source["snapshot_digest"]
    assert {key: harness.fake.strings[key] for key in cached_before} == cached_before
    assert len(harness.cache_keys()) == len(cached_before) + 1
    assert harness.computed == 2


# ---------------------------------------------------------------------------
# IV on the job: recorded snapshots reach bars, and the job says how many did
# ---------------------------------------------------------------------------


def test_the_result_reports_how_many_recorded_snapshots_reached_a_bar_and_the_page_can_say_why_the_rest_did_not(harness, recorder_store):
    recorder_store.write(recorded(WINDOW[10]))
    recorder_store.write(recorded(date(2024, 3, 9)))  # a Saturday

    harness.run(params(), job_id="iv")

    result = harness.result("iv")
    assert result["iv_source"] == "recorder"
    assert result["coverage"]["iv_alignment"] == {
        "rule": "snapshot_in_bar_latest", "snapshots_read": 2, "bars_with_iv": 1, "superseded": 0, "outside_bars": 1,
    }
    assert result["iv30"][10] == pytest.approx(0.21) and sum(v is not None for v in result["iv30"]) == 1


# ---------------------------------------------------------------------------
# The route: wiring, and the bar size the browser sends
# ---------------------------------------------------------------------------

ET_MIDNIGHT_MAR_4 = et_midnight_ms(date(2024, 3, 4))
BROWSER = {"job_id": "j", "symbol": "SPY", "bar_size": "15m", "tenor_days": 30, "hf_session": "ETH", "estimators": ["yz"], "windows": [5, 10, 30], "force": False}


def test_the_browser_body_is_accepted_in_snake_or_camel_case_and_defaults_are_the_studys_own():
    snake = RealizedVsIvJobRequest.model_validate(BROWSER)
    camel = RealizedVsIvJobRequest.model_validate(
        {"jobId": "j", "symbol": "spy", "barSize": "15m", "tenorDays": 30, "hfSession": "ETH", "estimators": ["yz"], "windows": [5, 10, 30]}
    )
    minimal = RealizedVsIvJobRequest.model_validate({"job_id": "j", "symbol": "SPY"})

    assert snake.symbol == camel.symbol == "SPY", "a ticker is upper-cased, as the store file name depends on it"
    assert (snake.bar_size, snake.tenor_days, snake.hf_session) == (camel.bar_size, camel.tenor_days, camel.hf_session)
    assert (minimal.bar_size, minimal.tenor_days, minimal.hf_session, minimal.session) == ("1d", 30, "ETH", None)
    assert minimal.estimators == ["yz"] and minimal.windows == [5, 10, 30] and minimal.iv_series is None


def test_the_estimators_a_request_may_name_are_exactly_the_ones_the_composition_computes():
    named = get_args(get_args(RealizedVsIvJobRequest.model_fields["estimators"].annotation)[0])

    assert set(named) == set(ESTIMATOR_FNS)


@pytest.mark.parametrize(
    "bad",
    [
        {"estimators": ["vol"]},
        {"estimators": []},
        {"windows": [1]},
        {"windows": []},
        {"hf_session": "AH"},
        {"session": "regular"},
        {"tenor_days": 0},
        {"window_start_ms": ET_MIDNIGHT_MAR_4},
        {"window_end_ms": ET_MIDNIGHT_MAR_4},
        {"window_start_ms": ET_MIDNIGHT_MAR_4 + 86_400_000, "window_end_ms": ET_MIDNIGHT_MAR_4},
        {"window_start_ms": ET_MIDNIGHT_MAR_4 + 1, "window_end_ms": ET_MIDNIGHT_MAR_4 + 86_400_000},
        {"window_start_ms": ET_MIDNIGHT_MAR_4, "window_end_ms": "2024-03-05"},
        {"from_date": "2024-03-04", "to_date": "2024-03-05"},
        {"symbol": "../etc"},
        {"bars": [{"ts": 1}]},
        {"iv_series": [{"ts": -1, "iv30": 0.2}]},
        {"iv_series": [{"ts": 1, "iv30": 0.2, "extra": 1}]},
    ],
)
def test_a_request_that_cannot_mean_anything_is_rejected_at_the_boundary(bad):
    with pytest.raises(ValidationError):
        RealizedVsIvJobRequest.model_validate({**BROWSER, **bad})


@pytest.mark.asyncio
async def test_the_route_hands_the_worker_the_study_it_was_asked_for(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Callable, **kwargs: Any) -> None:
        captured["work"] = work

    def fake_study(study_params: RealizedVsIvParams, **kwargs: Any) -> None:
        captured["params"], captured["kwargs"] = study_params, kwargs

    monkeypatch.setattr("app.routers.jobs.run_in_thread", fake_run_in_thread)
    monkeypatch.setattr("app.routers.jobs.run_realized_vs_iv_study", fake_study)
    request = RealizedVsIvJobRequest.model_validate(
        {**BROWSER, "job_id": "j1", "force": True, "session": "rth",
         "window_start_ms": et_midnight_ms(date(2024, 3, 4)), "window_end_ms": et_midnight_ms(date(2024, 5, 3)),
         "iv_series": [{"ts": 1_700_000_000_000, "iv30": 0.2, "health_score": None}]}
    )

    response = await start_realized_vs_iv_job(request)
    captured["work"]("emit", "cancel")

    assert response == {"job_id": "j1", "status": "queued"}
    assert captured["params"] == RealizedVsIvParams(
        symbol="SPY", bar_size="15m", tenor_days=30, hf_session="ETH", session_policy="regular",
        estimators=("yz",), windows=(5, 10, 30), confidence_floor=0.1,
        iv_series=[{"ts": 1_700_000_000_000, "iv30": 0.2, "health_score": None}],
        window=(date(2024, 3, 4), date(2024, 5, 3)),
    ), "an explicit null on the wire stays null: it is evidence about health, not an absent field"
    assert captured["kwargs"]["force"] is True and captured["kwargs"]["job_id"] == "j1"

    plain = RealizedVsIvJobRequest.model_validate({**BROWSER, "job_id": "j2"})
    await start_realized_vs_iv_job(plain)
    captured["work"]("emit", "cancel")
    assert captured["params"].session_policy is None and captured["params"].window is None
    assert captured["params"].iv_series is None

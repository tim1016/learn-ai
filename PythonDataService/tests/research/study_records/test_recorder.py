"""A study run leaves one record when it ends, however it ends (#3520).

These run a job's work through the recorder against a stand-in emitter and read the record back from a temporary
artifact root. The job routes' own wiring is in ``test_job_routes.py``; Indicator reliability's lake route is in
``tests/routers/test_indicator_reliability_endpoint.py`` and Realized vs IV's lake job is in
``tests/jobs/test_realized_vs_iv_job.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from app.jobs.progress import JobCancelled, ProgressEmitter
from app.research.study_records.models import StudyRecordConfig, StudyRecordResult, StudyType
from app.research.study_records.recorder import RunRecorder, capture_request, keeps_job_record, to_wire_json
from app.research.study_records.store import StudyRecordStore
from app.routers.jobs import CrossSectionalJobRequest, FeatureResearchJobRequest
from tests._helpers.study_runs import StubCancel, StubEmitter, a_cross_sectional_result, a_feature_result

FEATURE_REQUEST = {
    "job_id": "5f0c9a3e-1111-4b8a-9c11-0a0a0a0a0a0a",
    "symbol": "aapl",
    "from_date": "2024-01-02",
    "to_date": "2024-03-28",
    "feature_name": "rsi_14",
    "force": True,
}


def a_feature_request(**changes: Any) -> FeatureResearchJobRequest:
    return FeatureResearchJobRequest(**{**FEATURE_REQUEST, **changes})


def only_record(root: Path, study: StudyType = "feature-study") -> tuple[StudyRecordConfig, StudyRecordResult]:
    store = StudyRecordStore(study, root=root)
    ids = [record_id for record_id, _ in store.config_paths()]
    assert len(ids) == 1, f"expected one {study} record, found {len(ids)}"
    return store.load(ids[0])


def test_a_completed_job_leaves_its_inputs_result_and_provenance_without_the_switches_that_only_started_it(
    tmp_path: Path,
) -> None:
    emit = StubEmitter()
    result = a_feature_result(data_source={"kind": "lake", "snapshot_digest": "ab" * 32, "cache": {"hit": False}})
    work = keeps_job_record("feature-study", a_feature_request(), lambda emit, cancel: result, root=tmp_path)

    assert work(emit, StubCancel()) is result, "the study's own answer is returned untouched"

    config, stored = only_record(tmp_path)
    assert config.status == "completed" and config.status_detail is None
    assert config.inputs == {
        "symbol": "aapl",
        "from_date": "2024-01-02",
        "to_date": "2024-03-28",
        "timespan": "minute",
        "multiplier": 1,
        "session": "extended",
        "feature_name": "rsi_14",
    }, "job id, force and lineage say how it was started, not what it was asked"
    assert config.job_id == FEATURE_REQUEST["job_id"]
    assert config.headline == "Mean IC 0.0123, NW t-stat 2.10; stage 2 (Research candidate); 31,250 bars"
    assert config.data_source == result["data_source"], "the result's own provenance block is kept as it came"
    assert config.created_at_ms <= config.completed_at_ms
    assert stored.result is not None and stored.result["mean_ic"] == 0.0123
    assert emit.logs == []


def test_a_number_that_is_not_finite_is_stored_as_null_because_json_has_no_nan() -> None:
    wire = to_wire_json({"a": float("nan"), "b": [1.5, float("inf")], "c": ("x", 2)})

    assert wire == {"a": None, "b": [1.5, None], "c": ["x", 2]}


def test_a_result_without_a_provenance_block_is_stored_with_none_not_a_guess(tmp_path: Path) -> None:
    work = keeps_job_record("feature-study", a_feature_request(), lambda emit, cancel: a_feature_result(), root=tmp_path)

    work(StubEmitter(), StubCancel())

    config, _ = only_record(tmp_path)
    assert config.data_source is None and config.data_identity is None


def test_a_job_that_raises_leaves_a_failed_record_and_the_error_still_reaches_the_runner(tmp_path: Path) -> None:
    def work(emit: Any, cancel: Any) -> None:
        raise ValueError("No bars returned for AAPL in date range")

    recorded = keeps_job_record("feature-study", a_feature_request(), work, root=tmp_path)

    with pytest.raises(ValueError, match="No bars returned"):
        recorded(StubEmitter(), StubCancel())

    config, stored = only_record(tmp_path)
    assert config.status == "failed"
    assert config.status_detail == "No bars returned for AAPL in date range"
    assert config.headline is None and stored.result is None


def test_a_cancelled_job_leaves_a_cancelled_record(tmp_path: Path) -> None:
    def work(emit: Any, cancel: Any) -> None:
        raise JobCancelled("Cancelled by the user.")

    recorded = keeps_job_record("feature-study", a_feature_request(), work, root=tmp_path)

    with pytest.raises(JobCancelled):
        recorded(StubEmitter(), StubCancel())

    config, stored = only_record(tmp_path)
    assert config.status == "cancelled" and config.status_detail == "Cancelled by the user."
    assert stored.result is None


def test_a_failure_the_work_reports_itself_is_recorded_and_still_reaches_the_job(tmp_path: Path) -> None:
    """The work can end the job from inside, by calling ``emit.failed``, and return nothing: the run still failed."""

    def work(emit: Any, cancel: Any) -> None:
        emit.failed("DATA_NOT_ADMITTED", "The lake lacks 3 sessions.")

    emit = StubEmitter()
    recorded = keeps_job_record("feature-study", a_feature_request(), work, root=tmp_path)

    assert recorded(emit, StubCancel()) is None

    config, stored = only_record(tmp_path)
    assert config.status == "failed" and config.status_detail == "The lake lacks 3 sessions."
    assert stored.result is None
    assert emit.failures == [("DATA_NOT_ADMITTED", "The lake lacks 3 sessions.")], "the job's own terminal event is unchanged"


def test_a_refusal_the_work_parks_beside_its_failure_is_recorded_with_its_data_source(tmp_path: Path) -> None:
    """A lake study that is refused fails the job and parks the refusal as its result; the record keeps both."""
    refusal = {
        "success": False,
        "status": "refused",
        "error": "The lake cannot admit this window: 1 of 10 sessions is missing.",
        "refusal": {"code": "data_missing", "reason": "1 of 10 sessions is missing.", "sessions_total": 1},
        "data_source": {"kind": "lake", "snapshot_digest": None, "coverage": {"window": {"expected_sessions": 10, "missing": 1}}},
    }

    def work(emit: Any, cancel: Any) -> None:
        emit.failed("data_missing", refusal["error"], result=refusal)

    emit = StubEmitter()
    recorded = keeps_job_record("feature-study", a_feature_request(), work, root=tmp_path)

    assert recorded(emit, StubCancel()) is None

    config, stored = only_record(tmp_path)
    assert config.status == "failed" and config.status_detail == refusal["error"]
    assert config.headline is None, "a run with no result has no headline"
    assert config.data_source == refusal["data_source"], "the counts admission reached are the record's data source"
    assert stored.result == refusal
    assert emit.failures == [("data_missing", refusal["error"])]
    assert emit.failure_results == [refusal], "the job still parks the refusal for the page to read"


@pytest.mark.parametrize("method", ["log", "phase", "failed"])
def test_the_stub_emitter_takes_the_arguments_the_real_emitter_takes(method: str) -> None:
    """A stub that accepts more than the real emitter lets work pass here that raises in a job."""
    stub_parameters = inspect.signature(getattr(StubEmitter, method)).parameters
    real_parameters = inspect.signature(getattr(ProgressEmitter, method)).parameters

    assert [(p.name, p.kind) for p in stub_parameters.values()] == [(p.name, p.kind) for p in real_parameters.values()]


def test_a_result_the_recorder_cannot_read_is_reported_and_never_raised(tmp_path: Path) -> None:
    """Recording is guarded from the first step: a result of the wrong shape must not fail a study that ran."""
    run = RunRecorder("feature-study", a_feature_request(), capture_request, root=tmp_path)

    problem = run.deliver(["not", "a", "mapping"])  # type: ignore[arg-type]

    assert problem is not None and problem.startswith("This run could not be saved to the History")
    assert StudyRecordStore("feature-study", root=tmp_path).config_paths() == []


def test_a_result_served_from_the_cache_is_a_copy_of_a_recorded_run_and_is_not_recorded_again(tmp_path: Path) -> None:
    cached = a_feature_result(data_source={"kind": "lake", "cache": {"hit": True, "cached_at_ms": 1}})
    recorded = keeps_job_record("feature-study", a_feature_request(), lambda emit, cancel: cached, root=tmp_path)

    assert recorded(StubEmitter(), StubCancel()) is cached

    assert StudyRecordStore("feature-study", root=tmp_path).config_paths() == []


def test_work_that_answers_the_job_itself_without_failing_leaves_no_record(tmp_path: Path) -> None:
    recorded = keeps_job_record("feature-study", a_feature_request(), lambda emit, cancel: None, root=tmp_path)

    assert recorded(StubEmitter(), StubCancel()) is None

    assert StudyRecordStore("feature-study", root=tmp_path).config_paths() == []


def test_a_record_that_cannot_be_written_never_fails_the_study_and_the_job_log_says_so(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the artifact root should be")
    emit = StubEmitter()
    recorded = keeps_job_record("feature-study", a_feature_request(), lambda emit, cancel: a_feature_result(), root=blocked)

    result = recorded(emit, StubCancel())

    assert result is not None and result["mean_ic"] == 0.0123
    assert len(emit.logs) == 1
    level, message = emit.logs[0]
    assert level == "warn" and message.startswith("This run could not be saved to the History")


def test_a_rerun_names_the_record_it_was_prefilled_from_only_when_the_store_holds_it(tmp_path: Path) -> None:
    first = keeps_job_record("feature-study", a_feature_request(), lambda emit, cancel: a_feature_result(), root=tmp_path)
    first(StubEmitter(), StubCancel())
    original, _ = only_record(tmp_path)

    again = a_feature_request(job_id="second-job", rerun_of=original.record_id)
    keeps_job_record("feature-study", again, lambda emit, cancel: a_feature_result(mean_ic=0.02), root=tmp_path)(
        StubEmitter(), StubCancel()
    )
    nowhere = a_feature_request(job_id="third-job", rerun_of="f" * 32)
    keeps_job_record("feature-study", nowhere, lambda emit, cancel: a_feature_result(mean_ic=0.03), root=tmp_path)(
        StubEmitter(), StubCancel()
    )

    store = StudyRecordStore("feature-study", root=tmp_path)
    by_job = {c.job_id: c for c, _ in (store.load(i) for i, _ in store.config_paths())}
    assert by_job["second-job"].rerun_of == original.record_id
    assert by_job["third-job"].rerun_of is None, "lineage that points at nothing is not lineage"
    assert len(by_job) == 3, "a rerun is a new record; the one it came from is untouched"
    assert by_job[FEATURE_REQUEST["job_id"]].completed_at_ms == original.completed_at_ms


@pytest.mark.parametrize(
    ("evaluated", "status", "detail"),
    [
        (3, "completed", None),
        (2, "partial", "2 of 3 members evaluated: 1 errored"),
    ],
)
def test_a_cross_sectional_run_that_evaluated_fewer_members_than_it_was_asked_is_partial(
    tmp_path: Path, evaluated: int, status: str, detail: str | None
) -> None:
    request = CrossSectionalJobRequest(
        job_id="cs-job", symbols=["spy", "qqq", "aapl"], from_date="2025-09-02", to_date="2025-12-01", feature_name="iv_rank_60"
    )
    result = a_cross_sectional_result(asked=3, evaluated=evaluated)
    keeps_job_record("cross-sectional-study", request, lambda emit, cancel: result, root=tmp_path)(StubEmitter(), StubCancel())

    config, _ = only_record(tmp_path, "cross-sectional-study")
    assert (config.status, config.status_detail) == (status, detail)
    assert config.inputs["symbols"] == ["spy", "qqq", "aapl"] and config.inputs["target_type"] == "directional"

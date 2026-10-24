"""The three job routes record their runs: Feature, Signal and Cross-sectional (#3520).

Each test calls the route, takes the work it would have started on a worker thread and runs it on the spot, then reads the
record from a temporary artifact root. The cross-sectional universe study, which reads a provider's option chains,
answers with a report of its own. The Feature and Signal studies read admitted lake bars, so their completed, refused and
cache-served records are driven through the real lake in ``tests/jobs/test_feature_study_job.py`` and
``tests/jobs/test_signal_study_job.py``; what is left here for them is the cancel that comes before any lake read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app.routers.jobs as jobs
from app.jobs.progress import JobCancelled
from app.research.batch_runner import CrossSectionalReport
from app.research.batch_runner import TickerValidity as CrossSectionalValidity
from app.research.study_records.store import StudyRecordStore
from app.routers.jobs import CrossSectionalJobRequest, FeatureResearchJobRequest, SignalEngineJobRequest
from tests._helpers.study_runs import StubCancel, StubEmitter, captured_jobs


def the_records(root: Path, study: Any) -> list[tuple[Any, Any]]:
    store = StudyRecordStore(study, root=root)
    return [store.load(record_id) for record_id, _ in store.config_paths()]


@pytest.fixture
def artifact_root() -> Path:
    """The root the routes' recorder writes to: the conftest points the default root at a tmp path; the tests read it."""
    from app.research.artifact.root import default_artifacts_root

    return default_artifacts_root()


FEATURE = {"job_id": "feature-job", "symbol": "TEST", "from_date": "2024-01-02", "to_date": "2024-01-05", "feature_name": "rsi_14"}


async def test_a_feature_run_cancelled_before_it_loads_anything_is_recorded_as_cancelled(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)

    await jobs.start_feature_research_job(FeatureResearchJobRequest(**FEATURE))
    with pytest.raises(JobCancelled):
        taken.only()(StubEmitter(), StubCancel(cancelled=True))

    ((config, _),) = the_records(artifact_root, "feature-study")
    assert config.status == "cancelled"


async def test_a_signal_run_cancelled_before_it_loads_anything_is_recorded_as_cancelled(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)

    await jobs.start_signal_engine_job(SignalEngineJobRequest(**{**FEATURE, "job_id": "signal-job"}))
    with pytest.raises(JobCancelled):
        taken.only()(StubEmitter(), StubCancel(cancelled=True))

    ((config, _),) = the_records(artifact_root, "signal-study")
    assert config.status == "cancelled"


async def test_a_cross_sectional_run_with_a_member_it_could_not_evaluate_is_recorded_as_partial(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)
    report = CrossSectionalReport(
        feature_name="iv_rank_60",
        target_type="directional",
        tickers_tested_raw=3,
        tickers_valid=2,
        tickers_passed=1,
        validity_summary=CrossSectionalValidity(valid=2, invalid_iv=1),
        aggregate_ic=0.031,
        summary="2 of 3 evaluated",
    )
    monkeypatch.setattr(jobs, "run_cross_sectional_study", lambda **_: report)
    request = CrossSectionalJobRequest(
        job_id="cs-job", symbols=["SPY", "QQQ", "XYZ"], from_date="2025-09-02", to_date="2025-12-01", feature_name="iv_rank_60"
    )

    await jobs.start_cross_sectional_job(request)
    taken.only()(StubEmitter(), StubCancel())

    ((config, stored),) = the_records(artifact_root, "cross-sectional-study")
    assert config.status == "partial" and config.status_detail == "2 of 3 members evaluated: 1 had no usable IV"
    assert config.inputs["symbols"] == ["SPY", "QQQ", "XYZ"]
    assert stored.result is not None and stored.result["tickers_valid"] == 2


async def test_a_run_the_route_answers_from_the_result_cache_starts_no_work_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)
    monkeypatch.setattr(jobs.result_cache, "lookup", lambda *_: ("hash", {"result": {}, "cached_at": 1}))
    monkeypatch.setattr(jobs.result_cache, "serve_cached_result", lambda *_: None)

    request = CrossSectionalJobRequest(
        job_id="cs-job", symbols=["SPY", "QQQ"], from_date="2025-09-02", to_date="2025-12-01", feature_name="iv_rank_60"
    )

    answer = await jobs.start_cross_sectional_job(request)

    assert answer["status"] == "cached" and taken.works == []
    assert the_records(artifact_root, "cross-sectional-study") == []

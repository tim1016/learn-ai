"""The three job routes record their runs: Feature, Signal and Cross-sectional (#3520).

Each test calls the route, takes the work it would have started on a worker thread and runs it on the spot, then reads the
record from a temporary artifact root. The Feature, Signal and Cross-sectional studies read admitted lake bars, so their
completed, partial, refused and cache-served records are driven through the real lake in their tests/jobs suites; what is
left here is the cancel that comes before any lake read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app.routers.jobs as jobs
from app.jobs.progress import JobCancelled
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


async def test_a_cross_sectional_run_cancelled_before_it_loads_anything_is_recorded_as_cancelled(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)
    request = CrossSectionalJobRequest(
        job_id="cs-job", symbols=["SPY", "QQQ", "XYZ"], from_date="2025-09-02", to_date="2025-12-01", feature_name="iv_rank_60"
    )

    await jobs.start_cross_sectional_job(request)
    with pytest.raises(JobCancelled):
        taken.only()(StubEmitter(), StubCancel(cancelled=True))

    ((config, _),) = the_records(artifact_root, "cross-sectional-study")
    assert config.status == "cancelled"
    assert config.inputs["symbols"] == ["SPY", "QQQ", "XYZ"]

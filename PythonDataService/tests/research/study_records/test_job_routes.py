"""The three job routes record their runs: Feature, Signal and Cross-sectional (#3520).

Each test calls the route, takes the work it would have started on a worker thread and runs it on the spot, then reads the
record from a temporary artifact root. Signal computes on synthetic bars for real; the cross-sectional universe study,
which reads a provider's option chains, answers with a report of its own. The Feature study reads admitted lake bars, so
its completed, refused and cache-served records are driven through the real lake in
``tests/jobs/test_feature_study_job.py``; what is left here for it is the cancel that comes before any lake read.
"""

from __future__ import annotations

import math
import random
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


def synthetic_bars(n: int, *, step_ms: int, seed: int = 1) -> list[dict[str, float]]:
    rng = random.Random(seed)
    price, ts = 100.0, 1_700_000_000_000
    bars = []
    for i in range(n):
        price *= math.exp(rng.gauss(0.0, 0.001))
        bars.append(
            {
                "timestamp": ts + i * step_ms,
                "open": price * (1 + rng.gauss(0, 0.0002)),
                "high": price * (1 + abs(rng.gauss(0, 0.0005))),
                "low": price * (1 - abs(rng.gauss(0, 0.0005))),
                "close": price,
                "volume": 1_000_000.0 + rng.random() * 100_000,
            }
        )
    return bars


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


async def test_a_signal_run_is_recorded_with_the_options_it_was_given(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)
    monkeypatch.setattr(jobs.polygon_client, "fetch_aggregates", lambda **_: synthetic_bars(1_500, step_ms=900_000))
    request = SignalEngineJobRequest(**{**FEATURE, "job_id": "signal-job"}, flip_sign=False, regime_gate_enabled=False)

    await jobs.start_signal_engine_job(request)
    taken.only()(StubEmitter(), StubCancel())

    ((config, stored),) = the_records(artifact_root, "signal-study")
    assert config.status == "completed"
    assert config.inputs["flip_sign"] is False and config.inputs["regime_gate_enabled"] is False
    assert config.inputs["multiplier"] == 15, "the route's own default is part of what it was asked"
    assert stored.result is not None and stored.result["feature_name"] == "rsi_14"


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


async def test_a_run_answered_from_the_result_cache_starts_no_work_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, artifact_root: Path
) -> None:
    taken = captured_jobs(monkeypatch)
    monkeypatch.setattr(jobs.result_cache, "lookup", lambda *_: ("hash", {"result": {}, "cached_at": 1}))
    monkeypatch.setattr(jobs.result_cache, "serve_cached_result", lambda *_: None)

    answer = await jobs.start_signal_engine_job(SignalEngineJobRequest(**{**FEATURE, "job_id": "signal-job"}))

    assert answer["status"] == "cached" and taken.works == []
    assert the_records(artifact_root, "signal-study") == []

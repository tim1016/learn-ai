"""Doubles and sample runs for the tests of study records (#3520).

A job route hands ``run_in_thread`` the work to run on a worker thread; ``captured_jobs`` takes that work instead, so a
test runs it on the spot against a stand-in emitter and reads the record it leaves. Nothing here touches Redis, the
network or the real artifact root.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.jobs.progress import JobCancelled


class StubEmitter:
    """The part of ``ProgressEmitter`` a study's work calls, remembering what it was told.

    Each method keeps the real emitter's signature, so a call the real one would refuse fails here too.
    """

    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.phases: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.failure_results: list[Any] = []

    def log(self, message: str, *, level: str = "info") -> None:
        self.logs.append((level, message))

    def phase(self, name: str, *, friendly: str | None = None) -> None:
        self.phases.append(name)

    def progress(self, current: int, total: int, **_: Any) -> None:
        return None

    def failed(self, code: str, message: str, *, result: Any = None) -> None:
        self.failures.append((code, message))
        self.failure_results.append(result)


@dataclass
class StubCancel:
    """A cancellation check that raises ``JobCancelled`` once ``cancelled`` is set.

    Like the real ``CancellationCheck`` it is a dataclass with ``check_every_n``: a lake study copies the check with
    ``dataclasses.replace(cancel, check_every_n=1)`` for its phase boundaries.
    """

    cancelled: bool = False
    check_every_n: int = 1

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled("Cancelled by the user.")


@dataclass
class CapturedJobs:
    """The work a job route handed to ``run_in_thread``, in order."""

    works: list[Callable[..., Any]] = field(default_factory=list)

    def only(self) -> Callable[..., Any]:
        assert len(self.works) == 1, f"expected one job to be started, got {len(self.works)}"
        return self.works[0]


def captured_jobs(monkeypatch: pytest.MonkeyPatch) -> CapturedJobs:
    """Make the job routes' ``run_in_thread`` keep the work instead of starting a thread, and the result cache miss."""
    import app.routers.jobs as jobs
    from app.jobs import cache as result_cache

    taken = CapturedJobs()
    monkeypatch.setattr(jobs, "run_in_thread", lambda job_id, work, **_: taken.works.append(work))
    monkeypatch.setattr(result_cache, "lookup", lambda *_: None)
    monkeypatch.setattr(result_cache, "store", lambda *_: "stored")
    return taken


def a_feature_result(**changes: Any) -> dict[str, Any]:
    """What the Feature job delivers, with the fields the History reads."""
    return {
        "success": True,
        "ticker": "AAPL",
        "feature_name": "rsi_14",
        "start_date": "2024-01-02",
        "end_date": "2024-03-28",
        "bars_used": 31_250,
        "mean_ic": 0.0123,
        "ic_t_stat": 2.4,
        "nw_t_stat": 2.1,
        "ic_values": [0.01, float("nan"), -0.02],
        "validation_verdict": {"stage_info": {"stage": 2, "label": "Research candidate"}},
        "passed_validation": True,
        "error": None,
        **changes,
    }


def a_cross_sectional_result(*, asked: int = 3, evaluated: int = 3, **changes: Any) -> dict[str, Any]:
    """What the Cross-sectional job delivers: ``asked`` members requested, ``evaluated`` of them usable."""
    return {
        "success": True,
        "feature_name": "iv_rank_60",
        "target_type": "directional",
        "tickers_tested": asked,
        "tickers_tested_raw": asked,
        "tickers_valid": evaluated,
        "tickers_passed": 1,
        "aggregate_ic": 0.031,
        "validity_summary": {"valid": evaluated, "invalid_iv": 0, "invalid_data": 0, "errored": asked - evaluated},
        "stage_info": {"stage": 1, "label": "Weak"},
        "ticker_results": [],
        **changes,
    }

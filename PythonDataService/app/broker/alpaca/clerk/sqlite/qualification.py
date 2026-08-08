"""Stable import façade for Alpaca SQLite Clerk qualification workflows."""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite import qualification_synthetic_rehearsal as _synthetic
from app.broker.alpaca.clerk.sqlite.qualification_performance import (
    FIXED_NOW_MS,
    PERFORMANCE_BUDGETS_MS,
    PROFILE_SCALES,
    SMOKE_ADVERSARIAL_TESTS,
    LatencySummary,
    QualificationProfile,
    qualification_markdown,
    run_performance_profile,
)
from app.broker.alpaca.clerk.sqlite.qualification_polygon_replay import (
    POLYGON_FIXTURE_SHA256,
    SYNTHETIC_REPLAY_FEED_ID,
    PolygonMinuteAggregate,
    PolygonReplayMarketDataFeed,
    load_polygon_minute_fixture,
    run_polygon_replay,
    synthesize_polygon_5s_bars,
)
from app.broker.alpaca.clerk.sqlite.qualification_storage_recovery import (
    PRODUCTION_ARTIFACTS_ROOT,
    QUALIFICATION_ARTIFACTS_ROOT,
    QualificationStorageBoundary,
    SyntheticRecoveryCustodyError,
    assert_qualification_storage_boundary,
)
from app.broker.alpaca.clerk.sqlite.qualification_synthetic_rehearsal import (
    SyntheticRehearsalPhaseError,
    SyntheticTestOutcome,
    synthetic_rehearsal_markdown,
)
from app.schemas.account_custody_synthetic_qualification import (
    SyntheticCustodyRehearsalReport,
)

__all__ = (
    "FIXED_NOW_MS",
    "PERFORMANCE_BUDGETS_MS",
    "POLYGON_FIXTURE_SHA256",
    "PRODUCTION_ARTIFACTS_ROOT",
    "PROFILE_SCALES",
    "QUALIFICATION_ARTIFACTS_ROOT",
    "SMOKE_ADVERSARIAL_TESTS",
    "SYNTHETIC_REPLAY_FEED_ID",
    "LatencySummary",
    "PolygonMinuteAggregate",
    "PolygonReplayMarketDataFeed",
    "QualificationProfile",
    "QualificationStorageBoundary",
    "SyntheticRecoveryCustodyError",
    "SyntheticRehearsalPhaseError",
    "SyntheticTestOutcome",
    "load_polygon_minute_fixture",
    "qualification_markdown",
    "run_performance_profile",
    "run_polygon_replay",
    "run_synthetic_custody_rehearsal",
    "synthesize_polygon_5s_bars",
    "synthetic_rehearsal_markdown",
)

def run_synthetic_custody_rehearsal(
    *,
    artifacts_root: Path,
    production_artifacts_root: Path,
    fixture_directory: Path,
    production_account_id: str,
    test_outcomes: dict[str, SyntheticTestOutcome],
    generated_at_ms: int | None = None,
) -> SyntheticCustodyRehearsalReport:
    """Delegate with explicit dependencies for the qualification test seams."""

    return _synthetic.run_synthetic_custody_rehearsal(
        artifacts_root=artifacts_root,
        production_artifacts_root=production_artifacts_root,
        fixture_directory=fixture_directory,
        production_account_id=production_account_id,
        test_outcomes=test_outcomes,
        generated_at_ms=generated_at_ms,
        polygon_replay_runner=run_polygon_replay,
        storage_boundary_resolver=assert_qualification_storage_boundary,
    )

"""The Recency resume gate's refusal wording, without a database (#1938, #2755).

``resume_refusal`` is a pure function of the launch record and the job's
liveness answer. A deleted launch cannot be restored by any surface, so the
refusal states the fact and never promises a restore.
"""

from __future__ import annotations

import pytest

from app.research.recency import service
from app.research.recency.models import LaunchView


def _launch(*, status: str = "FAILED", deleted_at_ms: int | None = None) -> LaunchView:
    return LaunchView(
        launch_id="launch-1",
        status=status,
        job_id=None,
        attempt=1,
        expected_runs=4,
        succeeded_runs=2,
        failed_runs=0,
        created_at_ms=1_700_000_000_000,
        completed_at_ms=None,
        deleted_at_ms=deleted_at_ms,
        config_json="{}",
    )


def test_resume_refusal_deleted_launch_states_the_fact_without_promising_a_restore() -> None:
    refusal = service.resume_refusal(_launch(deleted_at_ms=1_700_000_100_000), live=False)

    assert refusal == "the launch is deleted"


def test_resume_refusal_deleted_launch_outranks_a_resumable_status() -> None:
    assert service.resume_refusal(_launch(status="CANCELLED", deleted_at_ms=1), live=None) == "the launch is deleted"


@pytest.mark.parametrize(
    ("status", "live", "expected"),
    [
        ("COMPLETED", False, "the Recency launch is complete"),
        ("RUNNING", True, "the Recency launch is still running"),
        ("RUNNING", None, "the Recency launch is still running"),
        ("RUNNING", False, None),
        ("FAILED", None, None),
        ("CANCELLED", None, None),
    ],
)
def test_resume_refusal_follows_status_and_liveness(status: str, live: bool | None, expected: str | None) -> None:
    assert service.resume_refusal(_launch(status=status), live=live) == expected

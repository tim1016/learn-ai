"""The LEAN orchestrator owns cancellation (#2463, review P1/P2).

The pre-launch seams live inside :func:`run_trusted_sample`, where stopping
can be atomic with cleanup: a cancelled run removes its workspace so the
``run_id`` stays reusable — a staged-but-never-launched run used to leave an
initialized workspace behind, and the duplicate-run_id guard then burned the
id on the operator's retry. Once the launch call is made the container cannot
be interrupted; the run then polls the flag while it runs and through final
persistence, acknowledging a too-late request exactly once via the typed
``on_cancel_too_late`` hook.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app.services.lean_sidecar_service as service
from app.lean_sidecar.launcher.models import LaunchResponse
from app.lean_sidecar.manifest import BarsSpec, DataPolicy
from app.services.lean_sidecar_service import (
    CANCEL_TOO_LATE_MESSAGE,
    LeanRunCancelled,
    TrustedRunRequest,
    run_trusted_sample,
)

_START_MS = 1_736_778_600_000  # 2025-01-13 09:30 ET
_END_MS = 1_737_147_600_000  # 2025-01-17 16:00 ET


def _synthetic_request(run_id: str) -> TrustedRunRequest:
    return TrustedRunRequest(
        run_id=run_id,
        starting_cash=100_000,
        start_ms_utc=_START_MS,
        end_ms_utc=_END_MS,
        data_policy=DataPolicy(
            source="synthetic",
            symbol="SPY",
            adjusted=False,
            session="regular",
            input_bars=BarsSpec(timespan="minute", multiplier=1),
            strategy_bars=BarsSpec(timespan="minute", multiplier=1),
            timestamp_policy="bar_close_ms_utc",
            timezone="America/New_York",
            provider_kind="live",
            fixture_id=None,
            fixture_sha256=None,
        ),
        parity_group_id=None,
    )


@pytest.fixture
def isolated_orchestrator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run the orchestrator against a tmp artifacts root with the launcher pinned off."""
    monkeypatch.setattr(service, "DEFAULT_ARTIFACTS_ROOT", tmp_path)
    # A really-pinned digest: the manifest's runtime provenance lookup only
    # answers for digests registered in the pinned-provenance map.
    from app.lean_sidecar.config import PINNED_LEAN_RUNTIME_PROVENANCE

    monkeypatch.setattr(service, "PINNED_LEAN_IMAGE_DIGEST", next(iter(PINNED_LEAN_RUNTIME_PROVENANCE)))
    monkeypatch.setattr(service, "assert_lean_persistence_source_current", lambda: None)
    # The image-metadata staging step shells out to podman against the pinned
    # digest; the cancellation seams under test are around it, not it.
    monkeypatch.setattr(service, "stage_lean_metadata_from_image", lambda *a, **k: None)
    return tmp_path


async def test_a_cancel_before_anything_starts_never_creates_a_workspace(
    isolated_orchestrator: Path,
) -> None:
    launched: list[object] = []

    async def fake_post_launch(request: object) -> None:  # pragma: no cover - must not run
        launched.append(request)

    service.post_launch = fake_post_launch  # type: ignore[assignment]
    try:
        with pytest.raises(LeanRunCancelled):
            await run_trusted_sample(
                _synthetic_request("cancel_pre_start"),
                cancel_requested=lambda: True,
            )
    finally:
        del service.post_launch

    assert launched == []


async def test_a_cancel_during_staging_removes_the_workspace_and_never_launches(
    isolated_orchestrator: Path,
) -> None:
    flag = {"cancel": False}
    phases: list[str] = []
    launched: list[object] = []

    def on_phase(name: str) -> None:
        phases.append(name)
        if name == "staging_data":
            # The operator presses Cancel while data stages.
            flag["cancel"] = True

    async def fake_post_launch(request: object) -> None:  # pragma: no cover - must not run
        launched.append(request)

    service.post_launch = fake_post_launch  # type: ignore[assignment]
    try:
        with pytest.raises(LeanRunCancelled) as cancelled:
            await run_trusted_sample(
                _synthetic_request("cancel_while_staging"),
                on_phase=on_phase,
                cancel_requested=lambda: flag["cancel"],
            )
    finally:
        del service.post_launch

    assert "cancelled before the LEAN container launched" in str(cancelled.value)
    assert launched == []
    # The between-staging-and-launch seam stops before announcing the launch.
    assert "staging_data" in phases and "launching_sidecar" not in phases
    # The workspace this run created is gone, so the run_id stays reusable:
    # the duplicate-run_id guard would otherwise burn it on the retry.
    assert not (isolated_orchestrator / "cancel_while_staging").exists()
    with pytest.raises(LeanRunCancelled):
        # And a retry with the same id is not refused for a leftover tree.
        await run_trusted_sample(
            _synthetic_request("cancel_while_staging"),
            cancel_requested=lambda: True,
        )


async def test_a_cancel_after_the_launch_runs_to_completion_and_is_acknowledged_once(
    isolated_orchestrator: Path,
) -> None:
    flag = {"cancel": False}
    acknowledgments: list[str] = []

    async def slow_container(request: object) -> LaunchResponse:
        flag["cancel"] = True  # pressed while the container runs
        await asyncio.sleep(1.5)  # outlast one watchdog poll
        return LaunchResponse(
            run_id="cancel_too_late",
            exit_code=1,
            duration_ms=1_500,
            timed_out=False,
            is_clean=False,
            lean_errors={},
            log_tail="crashed",
        )

    service.post_launch = slow_container  # type: ignore[assignment]
    try:
        result = await run_trusted_sample(
            _synthetic_request("cancel_too_late"),
            cancel_requested=lambda: flag["cancel"],
            on_cancel_too_late=acknowledgments.append,
        )
    finally:
        del service.post_launch

    # The finished result is kept: the acknowledgement never becomes a
    # false "cancelled", and it is delivered exactly once (the persisting
    # seam sees the flag again and does not repeat it).
    assert result.exit_code == 1
    assert acknowledgments == [CANCEL_TOO_LATE_MESSAGE]

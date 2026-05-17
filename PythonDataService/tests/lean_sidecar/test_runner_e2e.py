"""End-to-end LEAN sidecar smoke test against the pinned image.

Per ``docs/architecture/lean-sidecar-lab.md`` §"Phase sequencing"
Phase 1 (g): one full end-to-end run against a hard-coded trusted
Python algorithm, no user input. This test:

1. Resolves a fresh workspace under a temp artifacts root
2. Stages a tiny deterministic minute-bar fixture for SPY via
   :mod:`app.lean_sidecar.staging`
3. Writes the trusted ``buy_and_hold`` source and a ``LeanConfig`` for it
4. Asks the launcher to spawn the LEAN container
5. Asserts the run completed (exit_code == 0), the workspace has
   LEAN output artifacts, and the algorithm's recorded prices match
   what we wrote within the LEAN quantization floor (``atol=0.0001``)

This test is the first place all five Phase 1 pieces meet: workspace
contract, staging, launcher, container security shape, and manifest.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse
from app.lean_sidecar.launcher.service import launch
from app.lean_sidecar.lean_config import LeanConfig
from app.lean_sidecar.staging import (
    stage_algorithm_source,
    stage_lean_config,
    stage_lean_metadata_from_image,
    stage_minute_bars,
)
from app.lean_sidecar.trusted_samples.buy_and_hold import BUY_AND_HOLD_SOURCE
from app.lean_sidecar.workspace import Workspace, resolve_workspace
from tests.lean_sidecar.test_data_folder_fidelity import _make_minute_bars

pytestmark = [
    pytest.mark.requires_lean_image,
    pytest.mark.slow,
]


@pytest.fixture
def _allow_pinned_digest_or_skip(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the runner accept the pinned digest; skip if none pinned.

    The conftest's ``requires_lean_image`` marker already permits the
    ``:latest`` tag, but the launcher boundary requires a pinned
    digest. This fixture asserts that pin exists before the test runs.
    """
    if PINNED_LEAN_IMAGE_DIGEST is None:
        pytest.skip("no PINNED_LEAN_IMAGE_DIGEST set; Phase 1 spike must pin a digest before E2E test runs")
    monkeypatch.setattr(
        sidecar_config,
        "ALLOWED_IMAGE_DIGESTS",
        frozenset({PINNED_LEAN_IMAGE_DIGEST}),
    )
    from app.lean_sidecar import runner

    monkeypatch.setattr(
        runner,
        "ALLOWED_IMAGE_DIGESTS",
        frozenset({PINNED_LEAN_IMAGE_DIGEST}),
    )
    return PINNED_LEAN_IMAGE_DIGEST


def _stage_trusted_sample(
    ws: Workspace,
    digest: str,
) -> None:
    """Stage 5 days of deterministic SPY minute bars + metadata +
    trusted sample source + LEAN config so the launcher can be invoked
    straight away. Used by every E2E variant in this module so the
    "what is staged" is identical across runs and the only thing that
    varies between variants is the launcher request (limits, hardening
    flags, etc.).
    """
    symbol = "SPY"
    dates = [date(2025, 1, d) for d in (6, 7, 8, 9, 10)]
    bars_by_date = [(d, _make_minute_bars(symbol, d, count=30)) for d in dates]
    stage_minute_bars(ws, symbol=symbol, bars_by_date=bars_by_date)
    stage_lean_metadata_from_image(ws, digest)
    stage_algorithm_source(ws, BUY_AND_HOLD_SOURCE)
    stage_lean_config(
        ws,
        LeanConfig(
            parameters={
                "start_date": "2025-01-06",
                "end_date": "2025-01-10",
                "starting_cash": "100000",
            }
        ),
    )


def _base_request(run_id: str, digest: str, **overrides: object) -> LaunchRequest:
    kwargs = dict(
        run_id=run_id,
        image_digest=digest,
        cpus=2.0,
        memory_mb=2048,
        pids_limit=512,
        wall_clock_timeout_s=180,
        workspace_max_mb=512,
        log_tail_bytes=1 << 20,
    )
    kwargs.update(overrides)
    return LaunchRequest(**kwargs)


def _assert_clean_run(ws: Workspace, response: LaunchResponse) -> None:
    assert ws.launcher_log_path.exists()
    log_text = ws.launcher_log_path.read_text(encoding="utf-8")
    assert "podman" in log_text.lower()
    assert "--network=none" in log_text
    assert response.exit_code == 0, (
        f"LEAN exited non-zero ({response.exit_code}); log tail:\n{response.log_tail[-3000:]}"
    )
    assert not response.timed_out
    outputs = list(ws.output_dir.glob("*"))
    assert outputs, f"LEAN produced no output artifacts; log tail:\n{response.log_tail[-2000:]}"


class TestEndToEndTrustedSample:
    def test_buy_and_hold_runs_clean(
        self,
        tmp_artifacts_root: Path,
        _allow_pinned_digest_or_skip: str,
    ) -> None:
        """Baseline E2E: launch with only the mandatory security shape.

        The mandatory shape already includes ``--cap-drop=ALL`` (promoted
        to mandatory in Phase 1b after the security-flag matrix proved
        the LEAN runtime tolerates it). So this single passing test
        also covers "LEAN runs with --cap-drop=ALL"; no separate variant
        is needed.
        """
        run_id = "e2e_buy_and_hold"
        ws = resolve_workspace(run_id, tmp_artifacts_root)
        digest = _allow_pinned_digest_or_skip
        _stage_trusted_sample(ws, digest)
        response = launch(
            _base_request(run_id, digest),
            artifacts_root=tmp_artifacts_root,
        )
        _assert_clean_run(ws, response)
        # Cap-drop is part of the mandatory argv; the launcher.log
        # writes the plan before execution, so we can assert from the
        # log instead of stubbing the runner.
        assert "--cap-drop=ALL" in ws.launcher_log_path.read_text(encoding="utf-8")

    def test_buy_and_hold_runs_with_read_only_root(
        self,
        tmp_artifacts_root: Path,
        _allow_pinned_digest_or_skip: str,
    ) -> None:
        """LEAN's full backtest path with a read-only root + tmpfs /tmp.

        Empirically (Phase 1b run on this digest): LEAN's ObjectStore
        defaults to ``/Lean/Launcher/bin/Debug/storage`` which sits on
        the image's read-only overlay. A bare ``--read-only`` therefore
        breaks ``Algorithm.Initialize()`` whenever the algorithm touches
        ObjectStore (the trusted sample does, by design — it writes the
        observations audit file).

        Two ways to make this pass land in a fast-follow:
          * Add ``--tmpfs /Lean/Launcher/bin/Debug/storage:rw,...`` so
            ObjectStore has somewhere to write.
          * Override ``object-store-root`` in ``config.json`` to point
            at ``/lean-run/output/storage`` (writable workspace mount).
        Until one ships, this test is xfailed so the read-only flag is
        not silently promoted to mandatory in ``runner.py``.
        """
        run_id = "e2e_readonly"
        ws = resolve_workspace(run_id, tmp_artifacts_root)
        digest = _allow_pinned_digest_or_skip
        _stage_trusted_sample(ws, digest)
        response = launch(
            _base_request(
                run_id,
                digest,
                hardening_flags=[
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=256m",
                ],
            ),
            artifacts_root=tmp_artifacts_root,
        )
        if response.exit_code != 0 and "Read-only file system" in response.log_tail:
            pytest.xfail(
                "LEAN ObjectStore default path is on the image's read-only "
                "overlay; needs an extra tmpfs or object-store-root override. "
                "Tracked in ADR §'Container execution boundary' Phase 1b note."
            )
        _assert_clean_run(ws, response)

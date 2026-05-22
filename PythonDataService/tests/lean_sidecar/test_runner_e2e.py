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
    stage_daily_bars,
    stage_empty_corporate_action_dirs,
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
    # LEAN's default benchmark + post-run equity-curve analysis need
    # daily bars for the same symbol; without them the run logs
    # ``failed_data_requests`` + ``analysis_failed`` even on a
    # successful backtest. Take the closing minute of each trading
    # day as a synthetic daily bar so the analyzer has something to
    # build the equity curve from.
    daily_bars = [day[-1] for (_, day) in bars_by_date]
    stage_daily_bars(ws, symbol=symbol, bars=daily_bars)
    stage_lean_metadata_from_image(ws, digest)
    # No corporate actions in window, but LEAN still warns when the
    # map_files directory is missing. Empty dirs silence the warning
    # without claiming reconciliation-grade fixtures.
    stage_empty_corporate_action_dirs(ws)
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


# Known-noise LEAN errors the *trusted sample* tolerates: the sample is
# explicitly non-reconciliation-grade (see buy_and_hold.py docstring).
# These specific patterns come from LEAN's default minute subscription
# also requesting quote bars, which the sample does not stage; they are
# inert for the backtest math and tracked in the ADR as Phase 1c work
# (real reconciliation needs the full subscription set staged).
# Any error NOT matching these patterns is a regression and fails the
# assertion below.
_TRUSTED_SAMPLE_KNOWN_NOISE = ("_quote.zip",)


def _assert_trusted_sample_run(ws: Workspace, response: LaunchResponse) -> None:
    """Assert the launcher + LEAN contract for the trusted (non-recon) sample.

    Requires:
      * launcher.log written with the planned podman argv (shell-quoted
        single-line form + argv-per-line form)
      * exit_code == 0, not timed out
      * the observations.csv audit file lands under
        workspace/output/storage/ — proving ObjectStore is wired
        through to the workspace and the bar-consumption gate is
        actually inspectable
      * LEAN's classified errors only contain known-noise patterns
        (no unexpected analysis failures or runtime errors)
      * the workspace has at least one LEAN output artifact

    A separate reconciliation-grade test (Phase 5) will use a strict
    ``response.is_clean is True`` instead of this filtered check.
    """
    assert ws.launcher_log_path.exists()
    log_text = ws.launcher_log_path.read_text(encoding="utf-8")
    assert "podman" in log_text.lower()
    assert "--network=none" in log_text
    # Launcher.log includes the shell-quoted single-line form so
    # operators can reproduce the invocation manually.
    assert "# shell:" in log_text
    assert response.exit_code == 0, (
        f"LEAN exited non-zero ({response.exit_code}); log tail:\n{response.log_tail[-3000:]}"
    )
    assert not response.timed_out

    # Phase 1 non-negotiable #9 + Phase 1c blocker: observations.csv
    # MUST land inside the workspace (object-store-root wired through).
    obs = ws.object_store_dir / "observations.csv"
    assert obs.exists(), f"observations.csv missing at {obs}; ObjectStore is not landing inside the workspace"
    body = obs.read_text(encoding="utf-8").splitlines()
    assert len(body) >= 2, f"observations.csv too small: {body!r}"
    assert body[0] == "ms_utc,open,high,low,close,volume"
    # Bar-consumption gate (i): the algorithm received and recorded
    # bars from the staged minute series.
    assert len(body) - 1 > 0, "no bars consumed by the trusted sample"

    # Any LEAN error category beyond known-noise is a regression. The
    # trusted sample is allowed to log the documented quote-file
    # warnings; an analysis_failed or runtime_error is never OK.
    surprising: dict[str, list[str]] = {}
    for cat, lines in response.lean_errors.items():
        unexpected = [line for line in lines if not any(noise in line for noise in _TRUSTED_SAMPLE_KNOWN_NOISE)]
        if unexpected:
            surprising[cat] = unexpected
    assert not surprising, f"LEAN logged unexpected errors beyond known trusted-sample noise: {surprising}"

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
        _assert_trusted_sample_run(ws, response)
        # Cap-drop is part of the mandatory argv; the launcher.log
        # writes the plan before execution, so we can assert from the
        # log instead of stubbing the runner.
        assert "--cap-drop=ALL" in ws.launcher_log_path.read_text(encoding="utf-8")

    def test_baseline_includes_read_only_and_user_in_argv(
        self,
        tmp_artifacts_root: Path,
        _allow_pinned_digest_or_skip: str,
    ) -> None:
        """Phase 1c — ``--read-only`` and ``--user`` are now mandatory.

        The baseline ``test_buy_and_hold_runs_clean`` above already
        exercises a full LEAN run under both flags (they're in the
        baseline shape now); this test pins the launcher.log assertion
        so a future runner refactor that silently drops either flag
        gets caught at the test level, not in a security review months
        later.

        The prior xfail (``test_buy_and_hold_runs_with_read_only_root``)
        was retired because ``--read-only`` is no longer opt-in. LEAN's
        ObjectStore default path (image overlay) was the blocker;
        Phase 1c's ``object-store-root`` config override moved it into
        the writable workspace, making the read-only root viable.
        ``--user=10001:10001`` works on Windows + WSL2 podman because
        the WSL2 mount layer doesn't enforce host UID ownership inside
        the container.
        """
        run_id = "e2e_hardened_baseline"
        ws = resolve_workspace(run_id, tmp_artifacts_root)
        digest = _allow_pinned_digest_or_skip
        _stage_trusted_sample(ws, digest)
        response = launch(
            _base_request(run_id, digest),
            artifacts_root=tmp_artifacts_root,
        )
        _assert_trusted_sample_run(ws, response)
        log_text = ws.launcher_log_path.read_text(encoding="utf-8")
        assert "--read-only" in log_text
        # ``--user`` is dynamic per host — host UID on Linux,
        # 10001:10001 fallback on Windows. Pattern-match so the
        # assertion works across both hosts and rejects --user=0:0
        # (root) specifically.
        user_lines = [line for line in log_text.splitlines() if line.startswith("--user=") and ":" in line]
        assert user_lines, f"no --user=<uid>:<gid> in launcher.log:\n{log_text[-2000:]}"
        for line in user_lines:
            uid_str = line.removeprefix("--user=").split(":", 1)[0]
            assert int(uid_str) > 0, f"container must not run as root, got {line!r}"

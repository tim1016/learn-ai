"""Container security-flag viability matrix.

The viability matrix behind ADR 0070 Decision 4 (the container
execution boundary): tests each optional hardening flag against the
pinned LEAN image and records which ones survive. The results land in
ADR 0070; the runner then includes only the surviving flags.

These tests SKIP when ``requires_lean_image`` is unmet so the unit suite
runs on hosts without the image. They are NOT silent on failure: a
regression in image hardening surfaces as a failed test, not a missed
warning.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator

import pytest

from app.lean_sidecar.config import LEAN_IMAGE_REPO, PINNED_LEAN_IMAGE_DIGEST

pytestmark = [
    pytest.mark.requires_lean_image,
    pytest.mark.slow,
]


def _image_ref() -> str:
    """Use the pinned digest if available, otherwise :latest.

    The Phase 1 spike pulls :latest first, probes flags, then pins. The
    ADR record names the digest the matrix was proven against.
    """
    if PINNED_LEAN_IMAGE_DIGEST:
        return f"{LEAN_IMAGE_REPO}@{PINNED_LEAN_IMAGE_DIGEST}"
    return f"{LEAN_IMAGE_REPO}:latest"


def _run_smoke(flags: Iterator[str]) -> subprocess.CompletedProcess:
    """Run a noop ``echo`` inside the container with the given flags.

    The flag matrix is about whether *podman + image* accept the flag,
    not whether LEAN itself runs. Reducing to ``echo ok`` gives a fast,
    deterministic signal independent of LEAN's startup time.

    Note: most LEAN images do not have ``/bin/echo`` on PATH for a
    custom ENTRYPOINT override, so we use ``--entrypoint /bin/echo`` to
    bypass LEAN's normal entry point.
    """
    podman = shutil.which("podman")
    assert podman, "requires_lean_image marker should have skipped this test"
    argv: list[str] = [
        podman,
        "run",
        "--rm",
        "--network=none",
        "--security-opt=no-new-privileges",
        "--entrypoint=/bin/echo",
        *list(flags),
        _image_ref(),
        "ok",
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)


# Each test asserts one flag in isolation against a known-pinned image.
# A failing test does NOT fail the build silently — it produces a clear
# message naming the flag and the exact stderr the image returned.


class TestHardeningFlagMatrix:
    # The combined row is every survivor together; the ADR records this exact
    # set. It hard-asserts rather than xfails: if the combined shape ever
    # regresses, the launcher's mandatory flag set is no longer viable on the
    # pinned image and that is a gating PR-blocker, not a flaky-test note.
    @pytest.mark.parametrize(
        ("flags", "label"),
        [
            pytest.param(("--cap-drop=ALL",), "--cap-drop=ALL", id="cap_drop_all"),
            pytest.param(("--read-only",), "--read-only", id="read_only_root"),
            pytest.param(("--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"), "--tmpfs /tmp", id="tmpfs_tmp"),
            pytest.param(("--pids-limit=64",), "--pids-limit", id="pids_limit"),
            pytest.param(
                (
                    "--cap-drop=ALL",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m",
                    "--pids-limit=64",
                ),
                "combined hardening shape",
                id="combined_required_shape",
            ),
        ],
    )
    def test_hardening_flags_accepted_by_image(self, flags: tuple[str, ...], label: str) -> None:
        result = _run_smoke(iter(flags))
        assert result.returncode == 0, f"{label} rejected by image; stderr=\n{result.stderr}"

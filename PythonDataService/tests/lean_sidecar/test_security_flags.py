"""Container security-flag viability matrix.

Phase 1 spike per ``docs/architecture/lean-sidecar-lab.md`` §"Container
execution boundary": tests each optional hardening flag against the
pinned LEAN image and records which ones survive. The results land in
the ADR; the runner then includes only the surviving flags.

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
    def test_cap_drop_all(self) -> None:
        result = _run_smoke(iter(["--cap-drop=ALL"]))
        assert result.returncode == 0, f"--cap-drop=ALL rejected by image; stderr=\n{result.stderr}"

    def test_read_only_root(self) -> None:
        result = _run_smoke(iter(["--read-only"]))
        assert result.returncode == 0, f"--read-only rejected by image; stderr=\n{result.stderr}"

    def test_tmpfs_tmp(self) -> None:
        result = _run_smoke(iter(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]))
        assert result.returncode == 0, f"--tmpfs /tmp rejected by image; stderr=\n{result.stderr}"

    def test_pids_limit(self) -> None:
        result = _run_smoke(iter(["--pids-limit=64"]))
        assert result.returncode == 0, f"--pids-limit rejected by image; stderr=\n{result.stderr}"

    def test_user_nonroot(self) -> None:
        # A common LEAN image runs as root; this test documents that
        # reality with an xfail rather than a hard fail until the ADR
        # is updated. If the LEAN image starts shipping a non-root user
        # by default this should be promoted to a hard assertion.
        result = _run_smoke(iter(["--user=10001:10001"]))
        if result.returncode != 0:
            pytest.xfail(f"LEAN image does not run as non-root by default; stderr=\n{result.stderr}")

    def test_combined_required_shape(self) -> None:
        """All survivors together. The ADR records this exact set.

        Hard-asserts rather than xfails: if the combined shape ever
        regresses, the launcher's mandatory flag set is no longer
        viable on the pinned image and that is a gating PR-blocker,
        not a flaky-test note.
        """
        result = _run_smoke(
            iter(
                [
                    "--cap-drop=ALL",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m",
                    "--pids-limit=64",
                ]
            )
        )
        assert result.returncode == 0, f"combined hardening shape rejected by image; stderr=\n{result.stderr}"

"""Shared fixtures + image-availability gating for lean_sidecar tests.

Tests that require the pinned LEAN image to be locally available are
marked with ``@pytest.mark.requires_lean_image`` and skipped when the
image is not pulled. This lets the unit-test suite run on hosts that
have never seen the multi-GB LEAN image (CI defaults).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.lean_sidecar.config import LEAN_IMAGE_REPO, PINNED_LEAN_IMAGE_DIGEST


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_lean_image: needs the pinned LEAN image locally + podman on PATH",
    )
    config.addinivalue_line(
        "markers",
        "requires_podman: needs podman on PATH (does not need the LEAN image)",
    )


def _podman_available() -> bool:
    return shutil.which("podman") is not None


def _lean_image_available() -> bool:
    if not _podman_available():
        return False
    if PINNED_LEAN_IMAGE_DIGEST is None:
        # Allow the spike to run against ``quantconnect/lean:latest`` when
        # the env opts in; the launcher allow-list still refuses anything
        # not in ALLOWED_IMAGE_DIGESTS unless the test sets it up itself.
        ref = f"{LEAN_IMAGE_REPO}:latest"
    else:
        ref = f"{LEAN_IMAGE_REPO}@{PINNED_LEAN_IMAGE_DIGEST}"
    try:
        result = subprocess.run(
            ["podman", "image", "exists", ref],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # Only swallow the specific failure modes that genuinely mean
        # "image-availability check could not run": podman not on PATH,
        # subprocess timeout, etc. Any other exception propagates so a
        # real bug in the conftest is not silently turned into a skip.
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_no_image = pytest.mark.skip(
        reason="LEAN image not pulled locally; set LEAN_SIDECAR_ALLOW_PULL=1 and run podman pull quantconnect/lean"
    )
    skip_no_podman = pytest.mark.skip(reason="podman not on PATH")

    podman_ok = _podman_available()
    image_ok = _lean_image_available()
    for item in items:
        if "requires_podman" in item.keywords and not podman_ok:
            item.add_marker(skip_no_podman)
        if "requires_lean_image" in item.keywords and not image_ok:
            item.add_marker(skip_no_image)


@pytest.fixture
def tmp_artifacts_root(tmp_path: Path) -> Path:
    """A clean, real, on-disk artifacts root for workspace tests.

    Path is resolved to its canonical form so path-under-root checks
    behave deterministically across Windows short-name expansion.
    """
    root = tmp_path / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


@pytest.fixture
def lean_sidecar_env(tmp_artifacts_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LEAN_LAUNCHER_ARTIFACTS_ROOT", str(tmp_artifacts_root))
    monkeypatch.delenv("LEAN_LAUNCHER_TOKEN", raising=False)
    return tmp_artifacts_root

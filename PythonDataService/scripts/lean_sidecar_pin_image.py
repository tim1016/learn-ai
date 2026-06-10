"""Resolve the locally-built arm64 LEAN image to a digest and pin it.

Run after building the arm64 derivative to rewrite the
``PINNED_LEAN_IMAGE_DIGEST_ARM64`` constant in
``app/lean_sidecar/config.py`` and print the digest for the ADR.

This is intentionally a small one-off helper, not part of the runtime
data plane: the digest is committed source, not config that changes
between deployments. After running this you should:

  1. ``git diff app/lean_sidecar/config.py`` and confirm the digest
  2. Update ``docs/architecture/lean-sidecar-lab.md`` §"Runner choice"
     with the same digest
  3. Commit both changes in the same PR
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "app" / "lean_sidecar" / "config.py"
IMAGE_REF = "localhost/learn-ai/lean-sandbox:arm64-dotnet109"

# Replace this exact line in config.py — defensive against accidentally
# rewriting unrelated values.
_DIGEST_LINE_PATTERN = re.compile(
    r"^PINNED_LEAN_IMAGE_DIGEST_ARM64: str \| None = .+$",
    re.MULTILINE,
)


def resolve_digest() -> str:
    """Return the sha256 digest of the locally-pulled LEAN image.

    Uses ``podman image inspect``'s first repo digest so the config pins
    the local derivative by immutable digest, not by mutable tag.
    """
    completed = subprocess.run(
        ["podman", "image", "inspect", IMAGE_REF, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    ref = completed.stdout.strip()
    digest = ref.rsplit("@", maxsplit=1)[-1]
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"podman returned an unexpected digest ref {ref!r}; was the image built?")
    return digest


def rewrite_pinned_digest(new_digest: str) -> None:
    """Replace the arm64 ``PINNED_LEAN_IMAGE_DIGEST_ARM64`` line in config.py."""
    src = CONFIG_PATH.read_text(encoding="utf-8")
    new_line = f'PINNED_LEAN_IMAGE_DIGEST_ARM64: str | None = "{new_digest}"'
    if not _DIGEST_LINE_PATTERN.search(src):
        raise RuntimeError(f"could not locate PINNED_LEAN_IMAGE_DIGEST_ARM64 line in {CONFIG_PATH}")
    new_src = _DIGEST_LINE_PATTERN.sub(new_line, src)
    CONFIG_PATH.write_text(new_src, encoding="utf-8")


def main() -> int:
    digest = resolve_digest()
    rewrite_pinned_digest(digest)
    logger.info(digest)
    logger.info("updated %s", CONFIG_PATH.relative_to(Path.cwd()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

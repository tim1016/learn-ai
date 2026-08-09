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

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "app" / "lean_sidecar" / "config.py"
IMAGE_REF = "localhost/learn-ai/lean-sandbox:arm64-dotnet109"
RUNTIME_DIR = "/Lean/Launcher/bin/Debug"

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


def inspect_runtime() -> dict[str, Any]:
    """Read version, binary hashes, PDB hashes, and SourceLink from the image."""
    labels_result = subprocess.run(
        ["podman", "image", "inspect", IMAGE_REF, "--format", "{{json .Config.Labels}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    labels = json.loads(labels_result.stdout)
    names = [
        "QuantConnect.Common.dll",
        "QuantConnect.Lean.Engine.dll",
        "QuantConnect.Lean.Launcher.dll",
        "QuantConnect.Common.pdb",
        "QuantConnect.Lean.Engine.pdb",
        "QuantConnect.Lean.Launcher.pdb",
    ]
    hash_command = "sha256sum " + " ".join(f"{RUNTIME_DIR}/{name}" for name in names)
    hashes_result = subprocess.run(
        ["podman", "run", "--rm", "--entrypoint", "/bin/sh", IMAGE_REF, "-c", hash_command],
        capture_output=True,
        text=True,
        check=True,
    )
    hashes = {
        Path(line.split(maxsplit=1)[1]).name: line.split(maxsplit=1)[0]
        for line in hashes_result.stdout.splitlines()
        if line.strip()
    }
    source_link_command = "strings " + " ".join(
        f"{RUNTIME_DIR}/{name}" for name in names if name.endswith(".pdb")
    )
    source_link_result = subprocess.run(
        ["podman", "run", "--rm", "--entrypoint", "/bin/sh", IMAGE_REF, "-c", source_link_command],
        capture_output=True,
        text=True,
        check=True,
    )
    commits = sorted(
        set(re.findall(r"raw\.githubusercontent\.com/QuantConnect/Lean/([0-9a-f]{40})/", source_link_result.stdout))
    )
    return {
        "image_digest": resolve_digest(),
        "lean_version": labels.get("lean_version"),
        "source_link_commits": commits,
        "binary_sha256": {name: hashes.get(name) for name in names if name.endswith(".dll")},
        "pdb_sha256": {name: hashes.get(name) for name in names if name.endswith(".pdb")},
    }


def compare_runtime_receipt(receipt: dict[str, Any]) -> list[str]:
    """Return exact provenance mismatches against the committed runtime pin."""
    sys.path.insert(0, str(CONFIG_PATH.parents[2]))
    from app.lean_sidecar.config import PINNED_LEAN_RUNTIME_PROVENANCE_ARM64

    provenance = PINNED_LEAN_RUNTIME_PROVENANCE_ARM64
    expected = {
        "image_digest": provenance.image_digest,
        "lean_version": provenance.lean_version,
        "source_commit": provenance.source_commit,
        "binary_sha256": dict(provenance.binary_sha256),
        "pdb_sha256": dict(provenance.pdb_sha256),
    }
    mismatches: list[str] = []
    for field in ("image_digest", "lean_version"):
        if receipt.get(field) != expected[field]:
            mismatches.append(f"{field}: expected {expected[field]!r}, got {receipt.get(field)!r}")
    if receipt.get("source_link_commits") != [expected["source_commit"]]:
        mismatches.append(
            "source_link_commits: expected "
            f"{[expected['source_commit']]!r}, got {receipt.get('source_link_commits')!r}"
        )
    for namespace in ("binary_sha256", "pdb_sha256"):
        actual_hashes = receipt.get(namespace) or {}
        for name, expected_hash in expected[namespace].items():
            if actual_hashes.get(name) != expected_hash:
                mismatches.append(
                    f"{namespace}.{name}: expected {expected_hash!r}, got {actual_hashes.get(name)!r}"
                )
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Inspect the image and compare every provenance pin without editing config.py.",
    )
    args = parser.parse_args()

    receipt = inspect_runtime()
    mismatches = compare_runtime_receipt(receipt)
    logger.info(json.dumps(receipt, indent=2, sort_keys=True))
    if mismatches:
        for mismatch in mismatches:
            logger.error("MISMATCH %s", mismatch)
        return 1
    logger.info("verified LEAN runtime provenance: all committed pins match")
    if args.verify_only:
        return 0

    digest = resolve_digest()
    rewrite_pinned_digest(digest)
    logger.info("updated %s", CONFIG_PATH.relative_to(Path.cwd()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

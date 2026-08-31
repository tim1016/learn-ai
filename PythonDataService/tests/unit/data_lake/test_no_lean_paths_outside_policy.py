"""Lint test: forbid raw LEAN-path substrings outside path_policy.

LEAN paths must only be constructed by app/data_lake/path_policy.py. Any other
module containing these substrings is a violation — the path should flow
through the typed dataclasses.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = PROJECT_ROOT / "app"

# Substrings that uniquely identify LEAN on-disk paths.
FORBIDDEN_SUBSTRINGS = (
    "equity/usa/",
    "market-hours/",
    "symbol-properties/",
)

# Files in which the substrings ARE permitted. This list is meant to shrink:
# an entry that no longer needs to be here is a guard quietly weaker than it
# looks, so remove it rather than leaving it as harmless-looking history.
# #1893 removed two entries that way -- app/engine/data/policy_store.py (its
# docstring documented the retired cache layout) and app/routers/engine.py
# (route docstrings that described it).
# - app/data_lake/path_policy.py: the canonical path-policy module (only permitted author)
# - app/lean_sidecar/: pre-data-lake staging code
# - app/engine/data/lean_format.py: the LEAN reader; constructs the paths it reads
# - app/engine/data/polygon_bars.py: Polygon->TradeBar helpers; docstring references the layout
# - app/engine/data/availability.py: availability checker; docstrings reference paths
# - app/engine/tests/: engine-internal tests referencing the above
# - app/research/: research scripts with hardcoded paths
ALLOWLISTED = (
    "app/data_lake/path_policy.py",
    "app/lean_sidecar/",
    "app/engine/data/lean_format.py",
    "app/engine/data/polygon_bars.py",
    "app/engine/data/availability.py",
    "app/engine/tests/",
    "app/research/",
)


def _is_allowlisted(rel_path: Path) -> bool:
    rel_str = str(rel_path).replace("\\", "/")
    return any(rel_str.startswith(prefix) for prefix in ALLOWLISTED)


def test_lean_paths_only_in_path_policy() -> None:
    violations: list[tuple[str, int, str]] = []
    for py_file in APP_DIR.rglob("*.py"):
        rel = py_file.relative_to(PROJECT_ROOT)
        if _is_allowlisted(rel):
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in FORBIDDEN_SUBSTRINGS:
                if needle in line and not re.search(r"^\s*#", line):
                    violations.append((str(rel), lineno, line.strip()))
    assert not violations, "Found LEAN-path string outside path_policy.py:\n" + "\n".join(
        f"  {f}:{ln}: {snippet}" for f, ln, snippet in violations
    )

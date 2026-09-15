"""Reading this repo's committed compose files, for the contract tests.

Two contracts read the same compose surface — the env-example completeness
contract and the worker-service declaration contract — and both need the same
three things: which files the repo commits, the one Compose-tag-tolerant YAML
loader, and an `environment:` block flattened to plain key/value pairs. They
live here so neither contract imports the other's privates.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RENDER_SCRIPT = ROOT / "scripts" / "render_fleet_topology.py"


def tracked_compose_files() -> list[str]:
    """Every compose file this repo commits — discovered, not hand-listed.

    A hardcoded tuple silently stops covering the next overlay someone adds
    (`compose.fleet.qualification.yaml` was missed exactly this way, despite
    the env-example contract's own docstring promising every committed compose
    file). This is the same failure shape `restart.sh`'s comments warn about
    for its own hardcoded container-name list.
    """
    result = subprocess.run(
        ["git", "ls-files", "compose*.yaml", "compose*.yml"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return sorted(result.stdout.split())


def render_module() -> object:
    """Load scripts/render_fleet_topology.py directly (same dynamic-import
    pattern as test_documentation_contract.py's `_checker_module()`), so its
    Compose-tag-tolerant YAML loader has exactly one implementation."""
    spec = importlib.util.spec_from_file_location("render_fleet_topology", RENDER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("render_fleet_topology could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment_as_key_value(environment: object) -> dict[str, str]:
    """One service's `environment:` block as {key: value}, list form or map."""
    if isinstance(environment, list):
        pairs = (entry.split("=", 1) for entry in environment)
        return {key: (value[0] if value else "") for key, *value in pairs}
    return {str(key): "" if value is None else str(value) for key, value in (environment or {}).items()}

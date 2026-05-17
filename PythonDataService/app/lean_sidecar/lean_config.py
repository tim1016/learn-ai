"""LEAN ``config.json`` author.

The runner is driven by a ``config.json`` the data plane writes into
``workspace/project/config.json``. Keeping the keys here, in one place,
means a future LEAN version bump touches one file and one test rather
than scattered string literals.

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Config the
launcher writes". Field names are confirmed against the pinned image in
the Phase 1 spike; mismatches surface in ``test_lean_config.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.lean_sidecar.runner import CONTAINER_WORKSPACE_MOUNT

# Container-side paths. Hard-coded here because the runner always
# mounts the workspace at ``/lean-run``; if that ever changes both this
# module and ``runner.py`` move together.
CONTAINER_DATA_FOLDER = f"{CONTAINER_WORKSPACE_MOUNT}/data"
CONTAINER_RESULTS_FOLDER = f"{CONTAINER_WORKSPACE_MOUNT}/output"
CONTAINER_OBJECT_STORE_ROOT = f"{CONTAINER_WORKSPACE_MOUNT}/output/storage"
CONTAINER_PYTHON_ALGORITHM = f"{CONTAINER_WORKSPACE_MOUNT}/project/main.py"
CONTAINER_CSHARP_ASSEMBLY = f"{CONTAINER_WORKSPACE_MOUNT}/project/QuantConnect.Algorithm.CSharp.dll"

AlgorithmLanguage = Literal["Python", "CSharp"]


@dataclass(frozen=True, slots=True)
class LeanConfig:
    """Strongly-typed LEAN config for a single backtest run.

    Phase 1 only exercises the Python path; ``algorithm_language`` is
    nonetheless parametric so the C# spike (deferred per Phase 1
    decision) can land without re-shaping this module.
    """

    algorithm_language: AlgorithmLanguage = "Python"
    algorithm_type_name: str = "MyAlgorithm"
    parameters: Mapping[str, str] = ()
    environment: str = "backtesting"

    def to_payload(self) -> dict[str, object]:
        """Return the dict serialized into ``config.json``.

        Only keys the pinned LEAN image consumes are included; future
        LEAN versions adding optional keys can be supported additively
        without breaking the existing manifest hash for old runs.
        """
        if self.algorithm_language == "Python":
            algorithm_location = CONTAINER_PYTHON_ALGORITHM
        else:
            algorithm_location = CONTAINER_CSHARP_ASSEMBLY
        return {
            "environment": self.environment,
            "algorithm-language": self.algorithm_language,
            "algorithm-type-name": self.algorithm_type_name,
            "algorithm-location": algorithm_location,
            "data-folder": CONTAINER_DATA_FOLDER,
            "results-destination-folder": CONTAINER_RESULTS_FOLDER,
            # Override LEAN's default ObjectStore root (image overlay at
            # /Lean/Launcher/bin/Debug/storage) so per-run audit files
            # like the trusted sample's observations.csv land in the
            # workspace where the manifest can hash them and the
            # operator can inspect them.
            "object-store-root": CONTAINER_OBJECT_STORE_ROOT,
            "parameters": dict(self.parameters),
            # The defaults below match the pinned LEAN image's documented
            # local-backtest config. Phase 1 spike re-asserts they are
            # still required keys when the digest is pinned.
            "debugging": False,
            "show-missing-data-logs": True,
            "maximum-data-points-per-chart-series": 4000,
        }

    def write(self, dest: Path) -> Path:
        """Write the config to ``dest`` and return the path.

        Pretty-printed with sorted keys so the manifest's
        ``config_json_sha256`` is stable across Python dict-iteration
        order changes.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(self.to_payload(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return dest

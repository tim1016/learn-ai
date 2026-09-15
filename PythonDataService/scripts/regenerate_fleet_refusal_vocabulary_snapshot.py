"""Regenerate the fleet refusal vocabulary snapshot (#2067).

The fleet control plane's closed refusal vocabulary is authored on the
Python side in ``app/broker/fleet/refusal_vocabulary.py``
(``FLEET_REFUSAL_REASONS``). This script writes **two identical** JSON
snapshot files -- one in the PythonDataService tree, one in the Frontend
tree -- so the two test containers (which do not share a working tree) each
lock against their own copy, mirroring the established pattern for the
broker-v2 panel vocabulary (see
``scripts/regenerate_broker_v2_vocabulary_snapshot.py``):

- pytest ``tests/broker/fleet/test_refusal_vocabulary_snapshot.py`` asserts
  the live 28-class ``FleetControlError`` subclass closure plus the 5 codes
  minted outside it equals the committed Python-tree snapshot exactly.
  Failing means a refusal family was added, renamed, or had its status code
  changed without regenerating.

- The Frontend's ``fleet-refusal-copy.spec.ts`` (Task 8, #2067 follow-up)
  will load the Frontend-tree snapshot and assert its copy map covers
  exactly the same code set.

A CI job (``broker-v2-vocabulary-contract``, extended by Task 7d) regenerates
both files from live source on every PR and diffs them against the committed
copies, so a hand-edit to either file -- even one applied identically to
both -- fails CI.

Usage::

    DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.regenerate_fleet_refusal_vocabulary_snapshot

The script is idempotent: the same source always produces the same JSON
output (sorted-by-code dict, deterministic key order).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Final

from app.broker.fleet.refusal_vocabulary import FLEET_REFUSAL_REASONS

logger = logging.getLogger(__name__)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_PYTHON_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "PythonDataService"
    / "app"
    / "broker"
    / "fleet"
    / "refusal_vocabulary.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT / "Frontend" / "src" / "app" / "fleet" / "fleet-refusal-vocabulary.snapshot.json"
)

_SNAPSHOT_COMMENT: Final[str] = (
    "Snapshot of the fleet control plane's closed refusal vocabulary (#2067): "
    "the 28-class FleetControlError subclass closure plus 5 codes minted "
    "outside it. Two test surfaces lock against this file. Pytest "
    "PythonDataService/tests/broker/fleet/test_refusal_vocabulary_snapshot.py "
    "reads the Python-tree copy and asserts equality with the live subclass "
    "closure. The Frontend Vitest surface reads the Frontend-tree copy. "
    "Adding a family requires (a) declaring it in refusal_vocabulary.py, "
    "(b) re-running "
    "PythonDataService/scripts/regenerate_fleet_refusal_vocabulary_snapshot.py, "
    "(c) adding the Frontend copy entry. Any missing step fails a parity test."
)


def build_snapshot() -> dict[str, object]:
    """Return the snapshot dict, deterministically ordered by reason code."""
    return {
        "$comment": _SNAPSHOT_COMMENT,
        "generated_by": "PythonDataService/scripts/regenerate_fleet_refusal_vocabulary_snapshot.py",
        "source_files": [
            "PythonDataService/app/broker/fleet/refusal_vocabulary.py (FLEET_REFUSAL_REASONS)",
        ],
        "reasons": {
            code: {"status_code": family.status_code, "meaning": family.meaning}
            for code, family in sorted(FLEET_REFUSAL_REASONS.items())
        },
    }


def _write(path: Path, snapshot: dict[str, object]) -> None:
    """Atomically write ``snapshot`` as pretty-printed JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, indent=2, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")


def write_snapshots() -> tuple[Path, Path]:
    """Write both snapshot files. Returns the paths written."""
    snapshot = build_snapshot()
    _write(_PYTHON_SNAPSHOT_PATH, snapshot)
    _write(_FRONTEND_SNAPSHOT_PATH, snapshot)
    return _PYTHON_SNAPSHOT_PATH, _FRONTEND_SNAPSHOT_PATH


def main() -> int:
    """CLI entry point -- regenerate both snapshot copies."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    py_path, fe_path = write_snapshots()
    logger.info("wrote snapshot", extra={"path": str(py_path)})
    logger.info("wrote snapshot", extra={"path": str(fe_path)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

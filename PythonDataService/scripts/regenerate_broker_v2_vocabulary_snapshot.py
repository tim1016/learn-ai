"""Regenerate the broker-v2 panel vocabulary snapshot.

The broker-v2 bot control panel (spec §13) renders a **closed** operator
vocabulary authored on the Python side in
``app/broker/v2panel/vocabulary.py`` (``ALL_VOCABULARY_CODES``). This script
writes **two identical** JSON snapshot files — one in the PythonDataService
tree, one in the Frontend tree — so the two test containers (which do not share
a working tree) each lock against their own copy:

- pytest ``tests/broker/v2panel/test_vocabulary_snapshot.py`` asserts the live
  ``ALL_VOCABULARY_CODES`` set equals the Python-tree snapshot AND that every
  code carries non-trivial server-authored copy. Failing means a code was added
  (or copy omitted) without regenerating.

- Vitest (S5, when the Angular surface lands) will load the Frontend-tree
  snapshot and assert its emergency copy-fallback map covers exactly the same
  code set.

A CI job (``broker-v2-vocabulary-contract``) regenerates both files from live
source on every PR and diffs them against the committed copies, so a hand-edit
to either file — even one applied identically to both, which neither file's
own drift alone would catch — fails CI. ``test_vocabulary_snapshot.py``
additionally asserts the two committed copies are byte-identical to each other
and that every code's committed ``copy`` matches live ``OPERATOR_COPY``
exactly, not merely non-trivially.

Usage::

    podman exec polygon-data-service python -m scripts.regenerate_broker_v2_vocabulary_snapshot

The script is idempotent: same input set always produces the same JSON output
(sorted code array, deterministic key order).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Final

from app.broker.v2panel.vocabulary import ALL_VOCABULARY_CODES, OPERATOR_COPY

logger = logging.getLogger(__name__)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_PYTHON_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "PythonDataService"
    / "app"
    / "broker"
    / "v2panel"
    / "vocabulary.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "v2-panel"
    / "lib"
    / "broker-v2-vocabulary.snapshot.json"
)

_SNAPSHOT_COMMENT: Final[str] = (
    "Snapshot of the closed broker-v2 panel operator vocabulary "
    "(phases, desired states, duty-outcome kinds, hold reasons, "
    "reconciliation verdicts, channel states, station ids, station "
    "states, action ids). PAUSED and Continue encode the same-run hold/release "
    "lifecycle. Two test surfaces lock against this file. Pytest "
    "PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py "
    "reads the Python-tree copy and asserts equality with the live "
    "ALL_VOCABULARY_CODES frozenset and non-trivial copy for every code. "
    "The Frontend Vitest surface (S5) reads the Frontend-tree copy. "
    "Adding a code requires (a) updating vocabulary.py, (b) re-running "
    "PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py, "
    "(c) adding the emergency-fallback copy on the Frontend map. Any "
    "missing step fails a parity test."
)


def build_snapshot() -> dict[str, object]:
    """Return the snapshot dict, deterministically ordered.

    ``copy`` carries the server-authored label per code so the Frontend
    emergency-fallback map can be diffed against the exact server prose, not a
    hand-copied approximation.
    """
    return {
        "$comment": _SNAPSHOT_COMMENT,
        "generated_by": "PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py",
        "source_files": [
            "PythonDataService/app/broker/v2panel/vocabulary.py (ALL_VOCABULARY_CODES, OPERATOR_COPY)",
        ],
        "codes": sorted(ALL_VOCABULARY_CODES),
        "copy": {
            code: {"label": OPERATOR_COPY[code].label, "explanation": OPERATOR_COPY[code].explanation}
            for code in sorted(ALL_VOCABULARY_CODES)
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
    """CLI entry point — regenerate both snapshot copies."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    py_path, fe_path = write_snapshots()
    logger.info("wrote snapshot", extra={"path": str(py_path)})
    logger.info("wrote snapshot", extra={"path": str(fe_path)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

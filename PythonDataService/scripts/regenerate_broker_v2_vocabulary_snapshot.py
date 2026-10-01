"""Regenerate the broker-v2 panel vocabulary snapshot.

The broker-v2 bot control panel (spec §13) renders a **closed** operator
vocabulary authored on the Python side in
``app/broker/v2panel/vocabulary.py`` (``ALL_VOCABULARY_CODES``). This script
writes its JSON snapshot in the PythonDataService tree. Pytest
``tests/broker/v2panel/test_vocabulary_snapshot.py`` asserts the live
``ALL_VOCABULARY_CODES`` set equals the snapshot AND that every code carries
non-trivial server-authored copy. Failing means a code was added (or copy
omitted) without regenerating.

``test_vocabulary_snapshot.py`` also regenerates the snapshot from live source
on every PR and requires the committed copy to match it byte for byte, so a
hand-edit fails CI. It also asserts that every code's committed ``copy``
matches live ``OPERATOR_COPY`` exactly, not merely
non-trivially.

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

_SNAPSHOT_COMMENT: Final[str] = (
    "Snapshot of the closed broker-v2 panel operator vocabulary "
    "(phases, desired states, duty-outcome kinds, hold reasons, "
    "reconciliation verdicts, channel states, station ids, station "
    "states, action ids). Deploy starts a fresh identity; Stop is terminal. "
    "Pytest "
    "PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py "
    "asserts the committed file equals freshly generated output. "
    "Adding a code requires (a) updating vocabulary.py and (b) re-running "
    "PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py. "
    "Either missing step fails a parity test."
)


def build_snapshot() -> dict[str, object]:
    """Return the snapshot dict, deterministically ordered.

    ``copy`` carries the server-authored label and explanation per code, so
    the committed snapshot pins the exact server prose.
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


def write_snapshots() -> Path:
    """Write the snapshot file. Returns the path written."""
    _write(_PYTHON_SNAPSHOT_PATH, build_snapshot())
    return _PYTHON_SNAPSHOT_PATH


def main() -> int:
    """CLI entry point — regenerate the snapshot."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("wrote snapshot", extra={"path": str(write_snapshots())})
    return 0


if __name__ == "__main__":
    sys.exit(main())

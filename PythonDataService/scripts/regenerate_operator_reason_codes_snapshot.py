"""Regenerate the operator reason-codes snapshot that pins the closed
server-authored vocabulary across the Python + Frontend boundary.

The Frontend operator-language copy map
(``Frontend/src/app/components/broker/bot-control/lib/disabled-reason-copy.ts``)
maintains a TypeScript ``OperatorReasonCode`` union that must cover
every code the server can emit on
``ActionCapability.disabled_reason_code`` /
``ActionCapability.disabled_reasons``. The 2026-06-22 cockpit-audit
review (F-R4) flagged that the original parity test compared two
manually-maintained TypeScript lists rather than the live Python
source-of-truth — so a new code on the server wouldn't fail it.

This script writes **two identical** JSON snapshot files (one in the
PythonDataService tree, one in the Frontend tree) because the two
test containers do not share a working tree. Each side's parity test
reads its own snapshot copy and asserts the file matches that side's
source-of-truth:

- pytest ``test_operator_reason_codes_snapshot.py`` asserts the live
  Python ``REASON_CODES`` set equals the Python-tree snapshot. Failing
  means a code was added on the server without regenerating the
  snapshot.

- Vitest ``disabled-reason-copy.spec.ts`` loads the Frontend-tree
  snapshot and asserts ``ALL_OPERATOR_REASON_CODES`` equals it.
  Failing means the Frontend map drifted from the snapshot.

If a developer hand-edits one snapshot to "fix" a failing test, the
other side's test will still fail against its own source-of-truth.
Drift surfaces from either direction.

Usage::

    podman exec polygon-data-service python -m scripts.regenerate_operator_reason_codes_snapshot

The script is idempotent: same input set always produces the same
JSON output (sorted code array, deterministic key order).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Final

from app.services.operator_capability import REASON_CODES

logger = logging.getLogger(__name__)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_PYTHON_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "PythonDataService"
    / "app"
    / "services"
    / "operator_reason_codes.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "bot-control"
    / "lib"
    / "operator-reason-codes.snapshot.json"
)

_SNAPSHOT_COMMENT: Final[str] = (
    "Snapshot of the closed reason-code vocabulary on the server side "
    "(REASON_CODES = action-conflict matrix + live-binding gates "
    "∪ RESUME_REASON_CODES = resume-guard codes). Two test surfaces "
    "lock against this file. Pytest "
    "PythonDataService/tests/services/test_operator_reason_codes_snapshot.py "
    "reads the Python-tree copy and asserts equality with the live "
    "REASON_CODES frozenset. Vitest "
    "Frontend/src/app/components/broker/bot-control/lib/disabled-reason-copy.spec.ts "
    "reads the Frontend-tree copy and asserts equality with the "
    "ALL_OPERATOR_REASON_CODES export. Adding a code on the server "
    "requires (a) updating REASON_CODES / RESUME_REASON_CODES, (b) "
    "re-running PythonDataService/scripts/"
    "regenerate_operator_reason_codes_snapshot.py to refresh both "
    "snapshot copies, (c) updating disabled-reason-copy.ts to add the "
    "OperatorReasonCode entry + OPERATOR_REASON_COPY string. Any "
    "missing step fails one of the two parity tests."
)


def build_snapshot() -> dict[str, object]:
    """Return the snapshot dict, deterministically ordered."""
    return {
        "$comment": _SNAPSHOT_COMMENT,
        "generated_by": "PythonDataService/scripts/regenerate_operator_reason_codes_snapshot.py",
        "source_files": [
            "PythonDataService/app/services/operator_capability.py (REASON_CODES)",
            "PythonDataService/app/services/resume_guard_state.py (RESUME_REASON_CODES)",
        ],
        "codes": sorted(REASON_CODES),
    }


def _write(path: Path, snapshot: dict[str, object]) -> None:
    """Atomically write ``snapshot`` as pretty-printed JSON to ``path``.

    Ensures parent directories exist, preserves the dict's insertion
    order (so the ``$comment`` / ``generated_by`` / ``source_files`` /
    ``codes`` keys appear in the order the snapshot writer chose), and
    appends a trailing newline so the file is git-clean.
    """
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

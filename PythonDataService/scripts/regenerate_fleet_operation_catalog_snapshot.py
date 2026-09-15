"""Regenerate the fleet operation catalog snapshot (#2076, Task 10 of #2103).

The operation catalog is the single routing contract (ADR 0062 addendum,
item 4): the coordinator's forwarding allowlist, the public clerk-scoped
routes, the exported OpenAPI contract and the frontend's ``operationUrl``
builder all derive from the provider-declared
``app.broker.fleet.provider.ProviderOperation`` set
(``app/broker/alpaca/clerk/fleet_adapter.py``'s ``ALPACA_OPERATIONS`` today).
This script writes **two identical** JSON snapshot files -- one in the
PythonDataService tree, one in the Frontend tree -- so the two test
containers (which do not share a working tree) each lock against their own
copy, mirroring the established pattern for the broker-v2 panel vocabulary
and the fleet refusal vocabulary (see
``scripts/regenerate_broker_v2_vocabulary_snapshot.py`` and
``scripts/regenerate_fleet_refusal_vocabulary_snapshot.py``):

- pytest ``tests/broker/fleet/test_operation_catalog_snapshot.py`` asserts
  the live catalog from ``production_provider_adapters()`` equals the
  committed Python-tree snapshot exactly (operation ids, methods, path
  templates and account-scoping). Failing means an operation was added,
  renamed, or had its route or scoping changed without regenerating.

- The Frontend's ``operation-url.ts`` imports the Frontend-tree snapshot
  directly and derives its compile-time-checked operation-id union from it;
  ``operation-url.spec.ts`` exercises ``operationUrl`` against it.

A CI job (``broker-v2-vocabulary-contract``, extended by Task 10) regenerates
all three snapshots (broker-v2 panel, fleet refusal, fleet operation
catalog) from live source on every PR and diffs each pair against its
committed copies, so a hand-edit to either operation-catalog file -- even
one applied identically to both -- fails CI.

Usage::

    DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.regenerate_fleet_operation_catalog_snapshot

The script is idempotent: the same source always produces the same JSON
output (sorted-by-operation-id dict, deterministic key order).

Today exactly one provider (Alpaca) is registered in
``production_provider_adapters()``, so ``adapter_version`` pins that single
adapter's build label. A second production provider would need this script
(and the snapshot shape) extended to carry one version per provider --
deliberately deferred until that provider actually exists (YAGNI), not
designed for speculatively.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Final

from app.broker.fleet_composition import production_provider_adapters

logger = logging.getLogger(__name__)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_PYTHON_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT / "PythonDataService" / "app" / "broker" / "fleet" / "operation_catalog.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH: Final[Path] = (
    _REPO_ROOT / "Frontend" / "src" / "app" / "fleet" / "fleet-operation-catalog.snapshot.json"
)

_SNAPSHOT_COMMENT: Final[str] = (
    "Snapshot of the fleet's operation catalog (#2076, #2103): every "
    "ProviderOperation the production adapter registry declares. Two test "
    "surfaces lock against this file. Pytest "
    "PythonDataService/tests/broker/fleet/test_operation_catalog_snapshot.py "
    "reads the Python-tree copy and asserts equality with the live "
    "production_provider_adapters() catalog. The Frontend's "
    "Frontend/src/app/fleet/operation-url.ts imports the Frontend-tree copy "
    "and derives its compile-time-checked operation-id union from it. "
    "Adding an operation requires (a) declaring it in fleet_adapter.py (or "
    "a future provider's adapter), (b) re-running "
    "PythonDataService/scripts/regenerate_fleet_operation_catalog_snapshot.py. "
    "Any missing step fails a parity test."
)


def build_snapshot() -> dict[str, object]:
    """Return the snapshot dict, deterministically ordered by operation id.

    Sources from ``production_provider_adapters()`` rather than importing
    ``ALPACA_OPERATIONS`` directly so a malformed catalog (duplicate ids,
    inconsistent path parameters) is caught by that function's own
    validation before this script ever writes a snapshot for it.
    """
    adapters = production_provider_adapters()
    alpaca = adapters["alpaca"]
    operations = {
        operation.operation_id: {
            "method": operation.method,
            "path_template": operation.path_template,
            "requires_account": operation.requires_effective_account,
        }
        for operation in alpaca.operations()
    }
    return {
        "$comment": _SNAPSHOT_COMMENT,
        "generated_by": "PythonDataService/scripts/regenerate_fleet_operation_catalog_snapshot.py",
        "source_files": [
            "PythonDataService/app/broker/fleet_composition.py (production_provider_adapters)",
            "PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py (ALPACA_OPERATIONS)",
        ],
        "adapter_version": alpaca.adapter_version,
        "operations": dict(sorted(operations.items())),
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

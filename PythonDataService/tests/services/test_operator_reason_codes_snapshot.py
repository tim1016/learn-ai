"""Cross-stack parity test for the closed operator reason-code
vocabulary.

The snapshot at
``Frontend/src/app/components/broker/bot-control/lib/operator-reason-codes.snapshot.json``
is anchored by two parity tests:

- This one (pytest) asserts the live Python
  ``REASON_CODES`` set equals the snapshot's ``codes`` array. Failing
  means a code was added on the server without regenerating the
  snapshot via
  ``PythonDataService/scripts/regenerate_operator_reason_codes_snapshot.py``.

- Vitest ``disabled-reason-copy.spec.ts`` asserts the snapshot's
  ``codes`` array equals ``ALL_OPERATOR_REASON_CODES`` exported by
  ``disabled-reason-copy.ts``. Failing means the snapshot drifted
  from the Frontend map.

Together they form a true cross-stack contract (2026-06-22 cockpit
audit F-R4 closure). The earlier parity test compared two manually
maintained TypeScript lists; this set-up actually inspects the
server vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.operator_capability import REASON_CODES

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "services"
    / "operator_reason_codes.snapshot.json"
)

# The Frontend-tree copy is committed alongside ``disabled-reason-copy.ts``.
# In CI's host workdir (and on the dev host) both files are visible; in
# the polygon-data-service container only the Python copy is mounted.
# The cross-snapshot byte-equality test below skips when the Frontend
# copy is not visible (container-local) and runs (with assertion) when
# both are reachable (CI host, developer host).
FRONTEND_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "bot-control"
    / "lib"
    / "operator-reason-codes.snapshot.json"
)


def test_snapshot_file_exists() -> None:
    """The committed Python-tree snapshot file must exist."""
    assert SNAPSHOT_PATH.exists(), (
        f"snapshot not found at {SNAPSHOT_PATH} — regenerate via "
        "PythonDataService/scripts/regenerate_operator_reason_codes_snapshot.py"
    )


def test_snapshot_matches_live_python_set() -> None:
    """Live REASON_CODES MUST equal the snapshot's codes array.

    On failure: regenerate via
    ``python -m scripts.regenerate_operator_reason_codes_snapshot``
    and commit the updated JSON alongside the Frontend copy-map
    update.
    """
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    snapshot_codes = set(snapshot["codes"])
    live_codes = set(REASON_CODES)

    missing_from_snapshot = sorted(live_codes - snapshot_codes)
    extra_in_snapshot = sorted(snapshot_codes - live_codes)

    assert not missing_from_snapshot, (
        f"REASON_CODES added on the server but the snapshot is stale: "
        f"missing {missing_from_snapshot}. Regenerate via "
        "`python -m scripts.regenerate_operator_reason_codes_snapshot` "
        "and update disabled-reason-copy.ts."
    )
    assert not extra_in_snapshot, (
        f"snapshot references codes the server no longer authors: "
        f"extra {extra_in_snapshot}. Regenerate the snapshot."
    )


def test_snapshot_codes_are_sorted_and_unique() -> None:
    """The snapshot writer is deterministic; non-sorted / duplicate
    entries indicate hand-editing or a writer regression."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    codes = snapshot["codes"]
    assert codes == sorted(codes), "snapshot codes are not sorted"
    assert len(codes) == len(set(codes)), "snapshot codes contain duplicates"


@pytest.mark.parametrize("required_key", ["$comment", "generated_by", "source_files", "codes"])
def test_snapshot_has_required_keys(required_key: str) -> None:
    """Schema lock — adding / renaming a key in the snapshot writer
    breaks this test so the file shape stays stable for the Vitest
    side."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert required_key in snapshot, (
        f"snapshot missing required key '{required_key}' — regenerate via "
        "`python -m scripts.regenerate_operator_reason_codes_snapshot`."
    )


def test_frontend_and_python_snapshots_are_byte_identical() -> None:
    """Drift gap closure (CX-1).

    The original design wrote two snapshots and let each side anchor to
    its own copy. That admits a partial-regeneration drift: if a
    developer regenerates only one half (or hand-edits one to pass a
    failing test), both Vitest and pytest stay green but the cockpit is
    missing copy for a server-emitted code.

    This test asserts the two committed snapshot files are byte-
    identical. The regenerate script writes them identically; this test
    catches any subsequent hand-edit or partial regen.

    Skipped inside the polygon-data-service container because the
    Frontend tree is not mounted there. Active in CI host context and
    on the developer host where the full repo checkout is present.
    """
    if not FRONTEND_SNAPSHOT_PATH.exists():
        pytest.skip(
            f"Frontend snapshot not visible at {FRONTEND_SNAPSHOT_PATH} — "
            "this test runs in CI host / developer host contexts where the "
            "full repo checkout is available."
        )
    py_bytes = SNAPSHOT_PATH.read_bytes()
    fe_bytes = FRONTEND_SNAPSHOT_PATH.read_bytes()
    assert py_bytes == fe_bytes, (
        "Python snapshot and Frontend snapshot have drifted. Regenerate "
        "both via `python -m scripts.regenerate_operator_reason_codes_snapshot` "
        "to restore byte-equality (the writer is deterministic and writes "
        "both files identically)."
    )

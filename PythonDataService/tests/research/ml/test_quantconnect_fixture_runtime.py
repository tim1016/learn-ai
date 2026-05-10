"""Real-fixture runtime parity tests against the captured QC export.

Activates when both:

1. ``tests/fixtures/golden/qc-precomputed-predictions/qc_export.json`` exists, AND
2. ``tests/research/ml/fixtures/qc_known_hashes.json`` carries the
   ``run_ledger_prediction_set_hash`` and ``result_hash`` keys.

Until condition #2 is met (pending the §C runtime PR — building the
StrategySpec, running it through ``run_strategy_spec``, capturing
``RunLedger.prediction_set_hash`` / ``result_hash``), the tests skip
with a clear reason. This is an intentional staged gate, not a silent
approval: when the runtime work lands, the keys land too, and the
inner ``pytest.fail`` bodies fail loudly until real assertions are
written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "golden" / "qc-precomputed-predictions"
)
_QC_EXPORT = _FIXTURE_DIR / "qc_export.json"
_KNOWN_HASHES_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "qc_known_hashes.json"
)


def _runtime_hashes_pinned() -> bool:
    if not _QC_EXPORT.is_file() or not _KNOWN_HASHES_PATH.is_file():
        return False
    data = json.loads(_KNOWN_HASHES_PATH.read_text(encoding="utf-8"))
    return (
        "run_ledger_prediction_set_hash" in data
        and "result_hash" in data
    )


pytestmark = pytest.mark.skipif(
    not _runtime_hashes_pinned(),
    reason="QC runtime hashes not yet pinned — see §C runtime PR follow-up",
)


def test_qc_fixture_strategy_spec_run_ledger_hash_pinned() -> None:
    pytest.fail(
        "§C TODO: build a StrategySpec referencing the imported QC artifact, "
        "run via run_strategy_spec, assert RunLedger.prediction_set_hash equals "
        "the pinned value. Implement now that runtime hashes are pinned."
    )


def test_qc_fixture_strategy_spec_result_hash_pinned() -> None:
    pytest.fail(
        "§C TODO: assert run result_hash equals the pinned value in "
        "tests/research/ml/fixtures/qc_known_hashes.json. "
        "Implement now that runtime hashes are pinned."
    )

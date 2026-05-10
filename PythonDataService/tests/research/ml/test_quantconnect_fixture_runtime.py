"""Real-fixture runtime parity tests. Skipped until §B captures qc_export.json.

When the captured fixture lands, the module-level ``skipif`` no longer
fires and each test body FAILS with a TODO message — that's intentional.
A skipped test silently approves missing work; a failing test forces the
§C runtime assertions (pinned ``RunLedger.prediction_set_hash``,
``result_hash``) to be written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "golden" / "qc-precomputed-predictions"
)
_QC_EXPORT = _FIXTURE_DIR / "qc_export.json"

pytestmark = pytest.mark.skipif(
    not _QC_EXPORT.is_file(),
    reason="QC fixture not yet captured — see §B in the parity spec",
)


def test_qc_fixture_strategy_spec_run_ledger_hash_pinned() -> None:
    pytest.fail(
        "§C TODO: build a StrategySpec referencing the imported QC artifact, "
        "run via run_strategy_spec, assert RunLedger.prediction_set_hash equals "
        "the pinned value. Implement now that the fixture exists."
    )


def test_qc_fixture_strategy_spec_result_hash_pinned() -> None:
    pytest.fail(
        "§C TODO: assert run result_hash equals the pinned value in "
        "tests/research/ml/fixtures/qc_known_hashes.json. "
        "Implement now that the fixture exists."
    )

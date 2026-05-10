"""Real-fixture parity tests. Skipped until §B captures qc_export.json.

When the captured fixture lands at
``PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/qc_export.json``,
the module-level ``skipif`` no longer fires. At that point each test body
below FAILS with a TODO message — that's intentional. A skipped test
silently approves missing work; a failing test forces the §C assertions
(per-row tolerance check, pinned ``prediction_set_hash``) to be written.
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


def test_qc_fixture_parity_per_row_predictions_match() -> None:
    pytest.fail(
        "§C TODO: assert every imported prediction equals QC's published value "
        "within atol=1e-9, rtol=0 (spec D8). Implement now that the fixture exists."
    )


def test_qc_fixture_prediction_set_hash_pinned() -> None:
    pytest.fail(
        "§C TODO: assert importer's prediction_set_hash equals the value "
        "pinned in tests/research/ml/fixtures/qc_known_hashes.json. "
        "Implement now that the fixture exists."
    )

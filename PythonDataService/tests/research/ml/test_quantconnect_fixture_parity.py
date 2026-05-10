"""Real-fixture parity tests. Skipped until §B captures qc_export.json.

When the captured fixture lands at
``PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/qc_export.json``,
the module-level skip lifts and the inner ``pytest.skip`` calls below are
replaced with real assertions that:

- the importer's output ``prediction_set_hash`` equals the value pinned in
  ``tests/research/ml/fixtures/qc_known_hashes.json`` (§C);
- every per-row prediction value equals QC's published value within
  ``atol=1e-9, rtol=0`` (spec D8).
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
    pytest.skip("§C parity test — implement when fixture lands")


def test_qc_fixture_prediction_set_hash_pinned() -> None:
    pytest.skip("§C parity test — implement when fixture lands")

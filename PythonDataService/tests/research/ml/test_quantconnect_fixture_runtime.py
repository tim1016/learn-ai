"""Real-fixture runtime parity tests. Skipped until §B captures qc_export.json.

When the captured fixture lands, the module-level skip lifts and the
inner ``pytest.skip`` calls below are replaced with real assertions that
a ``StrategySpec`` consuming the imported artifact produces the pinned
``RunLedger.prediction_set_hash`` and ``result_hash`` (§C).
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
    pytest.skip("§C runtime test — implement when fixture lands")


def test_qc_fixture_strategy_spec_result_hash_pinned() -> None:
    pytest.skip("§C runtime test — implement when fixture lands")

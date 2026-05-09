"""Test 1 — QC ↔ Python same-bar engine parity (§ 8.1).

Strict-tolerance comparison of the QC Cloud algorithm output against
Python's output, both run on the *same* SPY minute fixture (the LEAN
parity fixture used by ``test_spy_validation.py``).

Per spec § 8.1, this test is the pre-paper-week engine-equivalence
gate. Engine-class divergence here is a real port bug.

Transitive equivalence: ``test_spy_validation.py`` already proves
Python is bit-exact against LEAN over this fixture window. So the
LEAN-extracted ``spy_lean_trades.csv`` IS the Python output. This test
compares QC's export against it directly — Python parity is implied.

The QC export must be present at ``references/qc-shadow/backtests/
lean-parity-fixture/trades.csv`` (committed by the operator after the
QC Cloud Test 1 run; see ``references/qc-shadow/README.md``).
The test ``pytest.skip``s cleanly until that file appears, so the test
acts as scaffolding now and as a real gate later.

Tolerances per § 6.3 same-bar engine parity:
  * trade timestamps exact
  * trade prices exact (to LEAN's 2-decimal log precision)
  * total trade count exact
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

EASTERN = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[4]
QC_TRADES_PATH = (
    REPO_ROOT / "references" / "qc-shadow" / "backtests" / "lean-parity-fixture" / "trades.csv"
)
LEAN_FIXTURE_PATH = (
    REPO_ROOT / "PythonDataService" / "app" / "engine" / "tests" / "fixtures" / "spy_lean_trades.csv"
)

requires_qc_export = pytest.mark.skipif(
    not QC_TRADES_PATH.exists(),
    reason=(
        "QC Cloud export not yet checked in at "
        "references/qc-shadow/backtests/lean-parity-fixture/trades.csv. "
        "Operator must run Test 1 in QC Cloud and commit the export."
    ),
)


def _eastern_str_to_ms(text: str) -> int:
    """Parse 'YYYY-MM-DD HH:MM' (Eastern) → int64 ms UTC.

    The LEAN fixture stores timestamps as wall-clock Eastern strings
    (e.g. '2024-04-11 12:00'). The QC export uses canonical ms UTC.
    Convert here so the comparison is in the canonical format from the
    repo's ``numerical-rigor`` rules.
    """
    dt = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)
    return int(dt.timestamp() * 1000)


def _load_lean_trades_as_ms() -> list[dict]:
    rows: list[dict] = []
    with LEAN_FIXTURE_PATH.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "entry_time_ms": _eastern_str_to_ms(r["entry"]),
                    "exit_time_ms": _eastern_str_to_ms(r["exit"]),
                    "entry_price": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]),
                }
            )
    return rows


@requires_qc_export
def test_qc_trades_match_lean_fixture_bit_exact() -> None:
    """Strict trade-by-trade match — § 8.1 hard gate.

    Failure here means the QC port is not equivalent to the Python
    algorithm and paper week is blocked.
    """
    qc = pd.read_csv(QC_TRADES_PATH)
    expected = _load_lean_trades_as_ms()

    assert len(qc) == len(expected), (
        f"trade count mismatch: QC={len(qc)} expected={len(expected)} "
        "(spec § 8.1 requires exact total trade count)"
    )

    for i, exp in enumerate(expected):
        actual = qc.iloc[i]
        diffs: list[str] = []

        if int(actual["entry_time_ms"]) != exp["entry_time_ms"]:
            diffs.append(
                f"entry_time_ms: QC={int(actual['entry_time_ms'])} "
                f"expected={exp['entry_time_ms']}"
            )
        if int(actual["exit_time_ms"]) != exp["exit_time_ms"]:
            diffs.append(
                f"exit_time_ms: QC={int(actual['exit_time_ms'])} "
                f"expected={exp['exit_time_ms']}"
            )
        # Prices to 2 decimal places — LEAN's log precision (§ 6.3 says
        # exact, the LEAN fixture is the canonical exact reference at
        # this precision).
        if round(float(actual["entry_price"]), 2) != round(exp["entry_price"], 2):
            diffs.append(
                f"entry_price: QC={float(actual['entry_price'])} "
                f"expected={exp['entry_price']}"
            )
        if round(float(actual["exit_price"]), 2) != round(exp["exit_price"], 2):
            diffs.append(
                f"exit_price: QC={float(actual['exit_price'])} "
                f"expected={exp['exit_price']}"
            )

        assert not diffs, f"trade {i + 1} mismatch:\n  " + "\n  ".join(diffs)

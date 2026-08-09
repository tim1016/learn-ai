"""Offline reconciliation gate for Engine Lab compatibility runs 95 / 96.

Formula: compatibility statistics and readiness are derived from the shared
closed-trade ledger; LEAN-native statistics remain an independent namespace.
Reference: QuantConnect LEAN source commit
  261366a7e26ae942df858ab20df4fef8fa07de67 and the retained run-96 artifacts.
Canonical implementation: app.services.lean_sidecar_persistence and
  app.services.run_verdict_service.
Validated against: this immutable run-95/96 fixture.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.engine.data.policy_store import snapshot_minute_trade_zips
from app.services.lean_sidecar_persistence import build_persist_payload

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "golden"
    / "engine-lab-compatibility-95-96-v1"
)
FILL_PRICE_ATOL = 0.01
PNL_ATOL = 1e-6


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def test_fixture_artifacts_match_immutable_manifest() -> None:
    manifest = _load(FIXTURE / "manifest.json")

    assert manifest["fixture_id"] == "engine-lab-compatibility-95-96-v1"
    assert manifest["source_runs"] == {"lean": 96, "python": 95}
    assert manifest["lean_source_commit"] == "261366a7e26ae942df858ab20df4fef8fa07de67"
    assert manifest["tolerances"] == {
        "fill_price_atol": "0.01",
        "native_statistics_atol": "0.0000500001",
        "pnl_atol": "0.000001",
        "quantities": "0",
        "rtol": "0",
        "timestamps_ms": "0",
    }
    for relative, receipt in manifest["artifacts"].items():
        artifact = FIXTURE / relative
        assert artifact.stat().st_size == receipt["size_bytes"], relative
        assert _sha256_file(artifact) == receipt["sha256"], relative


def test_fixture_recomputes_the_exact_shared_bar_snapshot() -> None:
    expected = _load(FIXTURE / "input" / "bars-manifest.json")

    actual = snapshot_minute_trade_zips(
        [FIXTURE / "workspace" / "data"],
        symbol="SPY",
        start=date(2026, 7, 8),
        end=date(2026, 8, 7),
        adjusted=False,
        session="regular",
    )

    assert actual == expected
    assert actual["fixture_id"] == "bar-store-v1-6e96f0f2c5383e2d"
    assert actual["fixture_sha256"] == "6e96f0f2c5383e2d1ad40f7308433aacc862bbfd6a9828f0b263cade70f0460e"


def test_pinned_python_and_lean_trades_match_trade_by_trade() -> None:
    expected = _load(FIXTURE / "expected" / "pair.json")
    python_trades = expected["python"]["trades"]
    lean_trades = expected["lean"]["trades"]

    assert len(python_trades) == len(lean_trades) == 5
    for python_trade, lean_trade in zip(python_trades, lean_trades, strict=True):
        assert python_trade["entry_ms_utc"] == lean_trade["entry_ms_utc"]
        assert python_trade["exit_ms_utc"] == lean_trade["exit_ms_utc"]
        assert _as_decimal(python_trade["quantity"]) == _as_decimal(lean_trade["quantity"])
        assert float(python_trade["entry_price"]) == pytest.approx(
            float(lean_trade["entry_price"]), abs=FILL_PRICE_ATOL, rel=0
        )
        assert float(python_trade["exit_price"]) == pytest.approx(
            float(lean_trade["exit_price"]), abs=FILL_PRICE_ATOL, rel=0
        )
        assert float(python_trade["pnl"]) == pytest.approx(
            float(lean_trade["pnl"]), abs=PNL_ATOL, rel=0
        )
        assert python_trade["is_synthetic_exit"] is False
        assert lean_trade["is_synthetic_exit"] is False


def test_pinned_lean_workspace_reproduces_platform_readiness_and_analysis() -> None:
    expected = _load(FIXTURE / "expected" / "pair.json")
    source_manifest = _load(FIXTURE / "source" / "lean-run-manifest.json")

    payload = build_persist_payload(
        workspace_path=FIXTURE,
        run_id="companion-pg-d2236694f523410fbb91",
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover_signal",
        start_date_ms=1_783_468_800_000,
        end_date_ms=1_786_060_800_000,
        manifest=source_manifest,
        cleanliness={
            "is_clean": True,
            "is_reconciliation_grade": True,
            "error_counts": {},
        },
        parity_group_id="pg-d2236694f523410fbb91",
    )

    lean_expected = expected["lean"]
    assert payload["fill_mode"] == "signal_bar_close"
    assert payload["total_trades"] == lean_expected["headline_kpis"]["total_trades"]
    assert payload["total_pnl"] == pytest.approx(
        float(lean_expected["headline_kpis"]["total_pnl"]), abs=PNL_ATOL, rel=0
    )
    assert payload["total_fees"] == pytest.approx(
        float(lean_expected["headline_kpis"]["total_fees"]), abs=PNL_ATOL, rel=0
    )
    # The persisted headline is currency-rounded to $98829.69.  The adapter
    # must retain LEAN's full ledger precision before that transport rounding.
    assert payload["final_equity"] == pytest.approx(
        float(lean_expected["native_statistics"]["portfolio"]["end_equity"]),
        abs=PNL_ATOL,
        rel=0,
    )

    verdict = json.loads(payload["run_verdict_json"])
    assert verdict["parity_signature"] == lean_expected["readiness"]["parity_signature"]
    assert verdict["status"] == "complete"
    assert verdict["available_required_metrics"] == verdict["required_metrics"] == 17
    assert (verdict["grade"], verdict["composite"], verdict["signal"]) == ("C", 44, "Rework")

    native_receipt = payload["lean_statistics"]["namespaces"]
    assert native_receipt["status"] == "match"
    assert native_receipt["native_metric_count"] == 66
    assert native_receipt["formatted_metric_count"] == 25
    assert native_receipt["divergences"] == []

    assert json.loads(payload["lean_analysis_json"]) == lean_expected["lean_analysis"]
    assert len(lean_expected["lean_analysis"]) == 3


def test_pinned_pair_has_identical_readiness_signature_and_agree_receipt() -> None:
    expected = _load(FIXTURE / "expected" / "pair.json")
    python_readiness = expected["python"]["readiness"]
    lean_readiness = expected["lean"]["readiness"]
    parity = expected["parity_verdict"]

    assert python_readiness["parity_signature"] == lean_readiness["parity_signature"]
    assert (python_readiness["grade"], python_readiness["composite"], python_readiness["signal"]) == (
        "C",
        44,
        "Rework",
    )
    assert parity["status"] == "agree"
    assert parity["divergences"] == []
    assert parity["counts_by_category"] == {}
    assert parity["input_parity"]["status"] == "match"
    assert parity["readiness_parity"] == {
        "compared_field_count": 17,
        "mismatched_fields": [],
        "reason": None,
        "status": "match",
    }
    assert parity["native_metric_parity"]["status"] == "match"
    assert parity["native_metric_parity"]["native_metric_count"] == 66
    assert parity["native_metric_parity"]["formatted_metric_count"] == 25

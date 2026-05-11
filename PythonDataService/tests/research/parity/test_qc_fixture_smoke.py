"""Phase 3 capture-smoke: validate QC fixture shape once it lands.

Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is
committed. The first test ensures the orders payload has every event
field the reconciler reads; the second logs ``FEE_PRESENCE_BRANCH=A|B``
so reviewers know whether commission parity is in scope for this fixture.

See ``docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`` §2.1.2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-aapl-phase3"
_ORDERS = _FIXTURE_DIR / "qc_orders.json"
_PRICES = _FIXTURE_DIR / "qc_price_history.csv"
_EQUITY = _FIXTURE_DIR / "qc_equity.json"


pytestmark = pytest.mark.skipif(
    not _ORDERS.is_file(),
    reason=(
        "Phase 3 QC fixture not yet captured at "
        f"{_FIXTURE_DIR}. See docs/superpowers/specs/"
        "2026-05-11-phase3-pnl-parity-design.md §2.1."
    ),
)


def _orders_payload() -> list[dict]:
    payload = json.loads(_ORDERS.read_text())
    raw = payload.get("orders") if isinstance(payload, dict) else payload
    return raw if raw is not None else payload


def test_orders_fixture_has_expected_event_fields() -> None:
    raw = _orders_payload()
    assert raw, "qc_orders.json contains no orders"

    sample = raw[0]
    assert "events" in sample, "order missing 'events'"
    assert sample["events"], "first order has empty 'events'"

    event = sample["events"][0]
    for key in ("time", "fillQuantity", "fillPrice", "direction"):
        assert key in event, f"event missing '{key}'"


def test_orders_fixture_fee_presence_branch_decider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _orders_payload()
    any_nonzero_fee = any(
        event.get("orderFeeAmount") is not None and float(event["orderFeeAmount"]) != 0.0
        for order in raw
        for event in order.get("events", [])
    )
    print(
        f"FEE_PRESENCE_BRANCH={'A' if any_nonzero_fee else 'B'} "
        f"(any non-zero orderFeeAmount in qc_orders.json = {any_nonzero_fee})"
    )
    # Smoke test only — never fails on branch identity; the print is the
    # decision signal for whether assert_fees=True is valid in
    # test_qc_aapl_phase3_trade_parity.py.
    assert _ORDERS.is_file()


def test_price_history_fixture_has_daily_ohlcv() -> None:
    lines = _PRICES.read_text().splitlines()
    assert lines, "qc_price_history.csv is empty"
    assert lines[0].strip().lower() == "time,open,high,low,close,volume", f"unexpected CSV header: {lines[0]!r}"
    assert len(lines) > 1, "qc_price_history.csv has no data rows"


def test_equity_fixture_parses() -> None:
    json.loads(_EQUITY.read_text())  # diagnostic — just confirm valid JSON

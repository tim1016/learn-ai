"""Unit tests for ``QcReconciler``.

Covers the four private steps (parse, audit, align, classify) and the
public ``reconcile_qc_aapl_phase3`` entry point. The two skipped tests
in ``test_qc_aapl_phase3_trade_parity.py`` are the live acceptance
test once the fixture lands; these tests run on every PR.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.research.parity.fixture_data_reader import FixtureDataReader
from app.research.parity.qc_reconciler import (
    DivergenceCategory,
    FixtureSchemaError,
    OurFill,
    QcFill,
    RoundTripPairingError,
    Tolerances,
    _align_fills,
    _audit_fixture,
    _classify_divergences,
    _pair_round_trips,
    _parse_qc_orders,
    reconcile_qc_aapl_phase3,
)

# ---------- helpers --------------------------------------------------------


def _qc_fill(
    side: str,
    qty: int,
    price: str,
    day: str,
    *,
    order_id: int = 1,
    fee: str | None = "1.00",
    order_type_code: int = 0,
) -> QcFill:
    dt = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    return QcFill(
        order_id=order_id,
        symbol="AAPL",
        side=side,  # type: ignore[arg-type]
        fill_qty=qty,
        fill_price=Decimal(price),
        fill_time_ms=int(dt.timestamp() * 1000),
        fee=None if fee is None else Decimal(fee),
        order_type_code=order_type_code,
    )


def _our_fill(
    side: str,
    qty: int,
    price: str,
    day: str,
    fee: str = "0.00",
) -> OurFill:
    dt = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    return OurFill(
        symbol="AAPL",
        side=side,  # type: ignore[arg-type]
        fill_qty=qty,
        fill_price=Decimal(price),
        fill_time_ms=int(dt.timestamp() * 1000),
        fee=Decimal(fee),
    )


_CSV_TWO_BARS = (
    "time,open,high,low,close,volume\n"
    "2026-02-10,189.50,190.10,188.20,189.80,52341000\n"
    "2026-02-11,190.00,191.20,189.30,190.55,48127000\n"
    "2026-02-25,194.80,195.90,194.10,195.30,50000000\n"
)


@pytest.fixture
def price_csv(tmp_path: Path) -> Path:
    p = tmp_path / "qc_price_history.csv"
    p.write_text(_CSV_TWO_BARS)
    return p


@pytest.fixture
def reader(price_csv: Path) -> FixtureDataReader:
    return FixtureDataReader(price_csv)


# ---------- _parse_qc_orders ----------------------------------------------


def test_parse_qc_orders_extracts_one_fill_per_event(tmp_path: Path) -> None:
    payload = {
        "orders": [
            {
                "id": 1,
                "symbol": "AAPL R735QTJ8XC9X",
                "type": 0,
                "direction": 0,
                "quantity": 526,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 526,
                        "fillPrice": 190.00,
                        "direction": 0,
                        "orderFeeAmount": 2.63,
                    }
                ],
            },
            {
                "id": 2,
                "symbol": "AAPL R735QTJ8XC9X",
                "type": 0,
                "direction": 1,
                "quantity": -526,
                "events": [
                    {
                        "time": "2026-02-25T13:30:00Z",
                        "fillQuantity": -526,
                        "fillPrice": 195.20,
                        "direction": 1,
                        "orderFeeAmount": 2.63,
                    }
                ],
            },
        ]
    }
    p = tmp_path / "qc_orders.json"
    p.write_text(json.dumps(payload))

    fills = _parse_qc_orders(p)

    assert len(fills) == 2
    assert fills[0].symbol == "AAPL"  # security-id suffix stripped
    assert fills[0].side == "buy"
    assert fills[0].fill_qty == 526
    assert fills[0].fill_price == Decimal("190.00")
    assert fills[0].fee == Decimal("2.63")
    assert fills[1].side == "sell"
    assert fills[1].fill_qty == -526


def test_parse_qc_orders_skips_zero_quantity_events(tmp_path: Path) -> None:
    payload = {
        "orders": [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 0,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 0,
                        "fillPrice": 0.0,
                        "direction": 0,
                    },
                    {
                        "time": "2026-02-11T13:31:00Z",
                        "fillQuantity": 100,
                        "fillPrice": 190.0,
                        "direction": 0,
                    },
                ],
            }
        ]
    }
    p = tmp_path / "qc_orders.json"
    p.write_text(json.dumps(payload))

    fills = _parse_qc_orders(p)
    assert len(fills) == 1
    assert fills[0].fill_qty == 100


def test_parse_qc_orders_handles_missing_fee_field(tmp_path: Path) -> None:
    payload = {
        "orders": [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 100,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 100,
                        "fillPrice": 190.0,
                        "direction": 0,
                    }
                ],
            }
        ]
    }
    p = tmp_path / "qc_orders.json"
    p.write_text(json.dumps(payload))

    fills = _parse_qc_orders(p)
    assert fills[0].fee is None  # Branch-B fixture: fee absent / explicitly missing


# ---------- _audit_fixture -------------------------------------------------


def test_audit_fixture_passes_when_fill_explained_by_bar_open(
    reader: FixtureDataReader,
) -> None:
    qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
    audits = _audit_fixture(qc, reader, Tolerances.phase3_default())
    assert audits == []


def test_audit_fixture_flags_price_not_within_tolerance(
    reader: FixtureDataReader,
) -> None:
    qc = [_qc_fill("buy", 526, "195.55", "2026-02-11")]  # bar open is 190.00
    audits = _audit_fixture(qc, reader, Tolerances.phase3_default())
    assert len(audits) == 1
    assert audits[0].expected_open == Decimal("190.00")
    assert "not explained" in audits[0].reason


def test_audit_fixture_flags_missing_bar(reader: FixtureDataReader) -> None:
    qc = [_qc_fill("buy", 526, "200.00", "2026-03-15")]  # no bar for that date
    audits = _audit_fixture(qc, reader, Tolerances.phase3_default())
    assert len(audits) == 1
    assert "no bar in fixture" in audits[0].reason


# ---------- _align_fills ---------------------------------------------------


def test_align_fills_pairs_by_date_and_side() -> None:
    qc = [
        _qc_fill("buy", 526, "190.00", "2026-02-11"),
        _qc_fill("sell", -526, "195.20", "2026-02-25"),
    ]
    ours = [
        _our_fill("buy", 526, "190.01", "2026-02-11"),
        _our_fill("sell", -526, "195.19", "2026-02-25"),
    ]
    pairs = _align_fills(qc, ours)
    assert len(pairs) == 2
    assert all(p.qc is not None and p.ours is not None for p in pairs)
    assert pairs[0].trading_date.isoformat() == "2026-02-11"


def test_align_fills_emits_half_pair_when_one_side_missing() -> None:
    qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
    ours: list[OurFill] = []
    pairs = _align_fills(qc, ours)
    assert len(pairs) == 1
    assert pairs[0].qc is not None
    assert pairs[0].ours is None


# ---------- _classify_divergences ------------------------------------------


def test_classify_decision_mismatch_on_missing_side() -> None:
    qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
    pairs = _align_fills(qc, [])
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    assert {d.category for d in divs} == {DivergenceCategory.DECISION_MISMATCH}


def test_classify_fill_price_drift_above_tolerance() -> None:
    qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
    ours = [_our_fill("buy", 526, "190.50", "2026-02-11")]
    pairs = _align_fills(qc, ours)
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    assert DivergenceCategory.FILL_PRICE_DRIFT in {d.category for d in divs}


def test_classify_fill_price_within_tolerance_emits_no_drift() -> None:
    qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
    ours = [_our_fill("buy", 526, "190.01", "2026-02-11")]
    pairs = _align_fills(qc, ours)
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    assert DivergenceCategory.FILL_PRICE_DRIFT not in {d.category for d in divs}


def test_classify_quantity_mismatch() -> None:
    qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
    ours = [_our_fill("buy", 500, "190.00", "2026-02-11")]
    pairs = _align_fills(qc, ours)
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    assert DivergenceCategory.QUANTITY_MISMATCH in {d.category for d in divs}


def test_classify_order_type_mismatch_flags_non_market() -> None:
    qc = [_qc_fill("buy", 100, "190.00", "2026-02-11", order_type_code=2)]  # non-market
    ours = [_our_fill("buy", 100, "190.00", "2026-02-11")]
    pairs = _align_fills(qc, ours)
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    assert DivergenceCategory.ORDER_TYPE_MISMATCH in {d.category for d in divs}


def test_classify_commission_drift_only_when_assert_fees_true() -> None:
    qc = [_qc_fill("buy", 100, "190.00", "2026-02-11", fee="9.99")]
    ours = [_our_fill("buy", 100, "190.00", "2026-02-11")]
    pairs = _align_fills(qc, ours)
    # With assert_fees=False, COMMISSION_DRIFT is suppressed:
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    assert DivergenceCategory.COMMISSION_DRIFT not in {d.category for d in divs}
    # With assert_fees=True, supplying a mismatched expected value emits it:
    divs = _classify_divergences(
        pairs,
        Tolerances.phase3_default(),
        assert_fees=True,
        computed_ibkr_fees={1: Decimal("1.00")},
    )
    assert DivergenceCategory.COMMISSION_DRIFT in {d.category for d in divs}


# ---------- reconcile_qc_aapl_phase3 (public entry point) -----------------


def _write_orders_json(path: Path, orders: list[dict]) -> None:
    path.write_text(json.dumps({"orders": orders}))


def test_reconcile_passes_when_fills_match_within_tolerance(tmp_path: Path) -> None:
    orders_path = tmp_path / "qc_orders.json"
    prices_path = tmp_path / "qc_prices.csv"
    prices_path.write_text(_CSV_TWO_BARS)
    _write_orders_json(
        orders_path,
        [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 526,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 526,
                        "fillPrice": 190.00,
                        "direction": 0,
                    }
                ],
            },
            {
                "id": 2,
                "symbol": "AAPL",
                "type": 0,
                "quantity": -526,
                "events": [
                    {
                        "time": "2026-02-25T13:30:00Z",
                        "fillQuantity": -526,
                        "fillPrice": 194.80,
                        "direction": 1,
                    }
                ],
            },
        ],
    )
    our_fills = [
        _our_fill("buy", 526, "190.00", "2026-02-11"),
        _our_fill("sell", -526, "194.80", "2026-02-25"),
    ]
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=orders_path,
        qc_price_history_path=prices_path,
        our_fills=our_fills,
    )
    assert report.status == "passed"
    assert report.summary.n_qc_fills == 2
    assert report.summary.n_our_fills == 2
    # Propagated PnL atol = (526 + 526) * $0.01 + 2 * $0.01 = $10.52 + $0.02 = $10.54
    assert report.diagnostics.propagated_pnl_atol == Decimal("10.54")


def test_reconcile_fails_on_price_drift(tmp_path: Path) -> None:
    orders_path = tmp_path / "qc_orders.json"
    prices_path = tmp_path / "qc_prices.csv"
    prices_path.write_text(_CSV_TWO_BARS)
    _write_orders_json(
        orders_path,
        [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 526,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 526,
                        "fillPrice": 190.00,
                        "direction": 0,
                    }
                ],
            }
        ],
    )
    our_fills = [_our_fill("buy", 526, "190.50", "2026-02-11")]  # 50c drift > 1c tol
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=orders_path,
        qc_price_history_path=prices_path,
        our_fills=our_fills,
    )
    assert report.status == "failed"
    assert any(d.category == DivergenceCategory.FILL_PRICE_DRIFT for d in report.divergences)


def test_reconcile_halts_on_fixture_insufficient(tmp_path: Path) -> None:
    orders_path = tmp_path / "qc_orders.json"
    prices_path = tmp_path / "qc_prices.csv"
    prices_path.write_text(_CSV_TWO_BARS)
    _write_orders_json(
        orders_path,
        [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 526,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 526,
                        "fillPrice": 200.00,  # bar open is 190.00 → unexplained
                        "direction": 0,
                    }
                ],
            }
        ],
    )
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=orders_path,
        qc_price_history_path=prices_path,
        our_fills=[],
    )
    assert report.status == "failed"
    cats = {d.category for d in report.divergences}
    # FIXTURE_INSUFFICIENT short-circuits classification, so DECISION_MISMATCH
    # (from our missing side) is NOT emitted in the same run.
    assert cats == {DivergenceCategory.FIXTURE_INSUFFICIENT}
    assert len(report.fixture_audit) == 1


def test_render_markdown_includes_status_and_window(tmp_path: Path) -> None:
    orders_path = tmp_path / "qc_orders.json"
    prices_path = tmp_path / "qc_prices.csv"
    prices_path.write_text(_CSV_TWO_BARS)
    _write_orders_json(
        orders_path,
        [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 526,
                "events": [
                    {
                        "time": "2026-02-11T13:30:00Z",
                        "fillQuantity": 526,
                        "fillPrice": 190.00,
                        "direction": 0,
                    }
                ],
            }
        ],
    )
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=orders_path,
        qc_price_history_path=prices_path,
        our_fills=[_our_fill("buy", 526, "190.00", "2026-02-11")],
    )
    md = report.render_markdown()
    assert "PASSED" in md
    assert "2026-02-11" in md


# ---------- P1 #2: parser strictness (canonical schema) --------------------


def test_parse_qc_orders_rejects_missing_orders_key(tmp_path: Path) -> None:
    p = tmp_path / "qc_orders.json"
    p.write_text(json.dumps([{"id": 1, "symbol": "AAPL", "events": []}]))  # top-level list
    with pytest.raises(FixtureSchemaError, match="missing top-level 'orders' key"):
        _parse_qc_orders(p)


def test_parse_qc_orders_rejects_nested_symbol_object(tmp_path: Path) -> None:
    p = tmp_path / "qc_orders.json"
    p.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "id": 1,
                        "symbol": {"value": "AAPL"},  # not normalized to plain string
                        "type": 0,
                        "events": [{"time": "2026-02-11T13:30:00Z", "fillQuantity": 100, "fillPrice": 190.0}],
                    }
                ]
            }
        )
    )
    with pytest.raises(FixtureSchemaError, match="'symbol' must be a string"):
        _parse_qc_orders(p)


def test_parse_qc_orders_rejects_orderEvents_camelCase(tmp_path: Path) -> None:
    p = tmp_path / "qc_orders.json"
    p.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "id": 1,
                        "symbol": "AAPL",
                        "type": 0,
                        "orderEvents": [  # raw QC shape — runbook must normalize to 'events'
                            {"time": "2026-02-11T13:30:00Z", "fillQuantity": 100, "fillPrice": 190.0}
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(FixtureSchemaError, match="missing 'events' key"):
        _parse_qc_orders(p)


def test_parse_qc_orders_accepts_numeric_ms_time(tmp_path: Path) -> None:
    # 2026-02-11 13:30 UTC == 1770773400000 ms
    p = tmp_path / "qc_orders.json"
    p.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "id": 1,
                        "symbol": "AAPL",
                        "type": 0,
                        "events": [
                            {
                                "time": 1770773400000,
                                "fillQuantity": 100,
                                "fillPrice": 190.0,
                                "direction": 0,
                            }
                        ],
                    }
                ]
            }
        )
    )
    fills = _parse_qc_orders(p)
    assert len(fills) == 1
    assert fills[0].fill_time_ms == 1770773400000


def test_parse_qc_orders_rejects_ambiguous_numeric_time(tmp_path: Path) -> None:
    p = tmp_path / "qc_orders.json"
    p.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "id": 1,
                        "symbol": "AAPL",
                        "type": 0,
                        "events": [
                            {"time": 500_000_000_000, "fillQuantity": 100, "fillPrice": 190.0}
                            # 5e11 — between epoch-seconds and epoch-ms; refuse
                        ],
                    }
                ]
            }
        )
    )
    with pytest.raises(FixtureSchemaError, match="ambiguous"):
        _parse_qc_orders(p)


# ---------- P1 #3: duplicate same-day same-side fills ----------------------


def test_align_fills_surfaces_duplicate_same_day_same_side_as_half_pairs() -> None:
    # Two QC buys on the same day with no matching ours fills — both
    # should appear as half-pairs, not collapse silently.
    qc = [
        _qc_fill("buy", 200, "190.00", "2026-02-11", order_id=1),
        _qc_fill("buy", 300, "190.05", "2026-02-11", order_id=2),
    ]
    ours: list[OurFill] = [_our_fill("buy", 526, "190.00", "2026-02-11")]
    pairs = _align_fills(qc, ours)
    # 2 QC buys × 1 ours buy → 2 pair slots (seq 0 matched, seq 1 unmatched)
    assert len(pairs) == 2
    assert pairs[0].qc is not None and pairs[0].ours is not None
    assert pairs[1].qc is not None and pairs[1].ours is None  # surplus QC fill surfaced
    # Classification should emit DECISION_MISMATCH for the unmatched second fill.
    divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
    cats = [d.category for d in divs]
    assert DivergenceCategory.DECISION_MISMATCH in cats


# ---------- P1 #1: round-trip P&L parity -----------------------------------


def test_pair_round_trips_computes_realized_pnl() -> None:
    fills = [
        _qc_fill("buy", 100, "190.00", "2026-02-11", fee="1.00"),
        _qc_fill("sell", -100, "195.00", "2026-02-25", fee="1.00"),
    ]
    round_trips = _pair_round_trips(fills, Tolerances.phase3_default())
    assert len(round_trips) == 1
    rt = round_trips[0]
    # (195 - 190) * 100 - 1 - 1 = 498
    assert rt.realized_pnl == Decimal("498.00")
    # propagated atol = (100 + 100) * 0.01 + 2 * 0.01 = 2.02
    assert rt.propagated_atol == Decimal("2.02")


def test_pair_round_trips_rejects_two_consecutive_buys() -> None:
    fills = [
        _qc_fill("buy", 100, "190.00", "2026-02-11"),
        _qc_fill("buy", 100, "191.00", "2026-02-12"),  # no sell in between
    ]
    with pytest.raises(RoundTripPairingError, match="consecutive buys"):
        _pair_round_trips(fills, Tolerances.phase3_default())


def test_pair_round_trips_rejects_sell_without_open_position() -> None:
    fills = [_qc_fill("sell", -100, "190.00", "2026-02-11")]
    with pytest.raises(RoundTripPairingError, match="no open position"):
        _pair_round_trips(fills, Tolerances.phase3_default())


def test_reconcile_emits_no_pnl_drift_on_clean_round_trip(tmp_path: Path) -> None:
    orders_path = tmp_path / "qc_orders.json"
    prices_path = tmp_path / "qc_prices.csv"
    prices_path.write_text(_CSV_TWO_BARS)
    _write_orders_json(
        orders_path,
        [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 100,
                "events": [{"time": "2026-02-11T13:30:00Z", "fillQuantity": 100, "fillPrice": 190.00, "direction": 0}],
            },
            {
                "id": 2,
                "symbol": "AAPL",
                "type": 0,
                "quantity": -100,
                "events": [{"time": "2026-02-25T13:30:00Z", "fillQuantity": -100, "fillPrice": 194.80, "direction": 1}],
            },
        ],
    )
    our_fills = [
        _our_fill("buy", 100, "190.00", "2026-02-11"),
        _our_fill("sell", -100, "194.80", "2026-02-25"),
    ]
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=orders_path,
        qc_price_history_path=prices_path,
        our_fills=our_fills,
    )
    assert report.status == "passed"
    cats = {d.category for d in report.divergences}
    assert DivergenceCategory.PNL_DRIFT not in cats
    # Round trips computed on both sides for diagnostics
    assert len(report.diagnostics.qc_round_trips) == 1
    assert len(report.diagnostics.our_round_trips) == 1
    assert report.diagnostics.qc_round_trips[0].realized_pnl == Decimal("480.00")


def test_reconcile_emits_pnl_drift_when_realized_pnl_disagrees(
    tmp_path: Path,
) -> None:
    # Inject an our_fill with wrong exit price big enough to bust BOTH
    # FILL_PRICE_DRIFT and the propagated P&L atol (so PNL_DRIFT fires
    # as a real assertion, not just an algebraic implication).
    orders_path = tmp_path / "qc_orders.json"
    prices_path = tmp_path / "qc_prices.csv"
    prices_path.write_text(_CSV_TWO_BARS)
    _write_orders_json(
        orders_path,
        [
            {
                "id": 1,
                "symbol": "AAPL",
                "type": 0,
                "quantity": 100,
                "events": [{"time": "2026-02-11T13:30:00Z", "fillQuantity": 100, "fillPrice": 190.00, "direction": 0}],
            },
            {
                "id": 2,
                "symbol": "AAPL",
                "type": 0,
                "quantity": -100,
                "events": [{"time": "2026-02-25T13:30:00Z", "fillQuantity": -100, "fillPrice": 194.80, "direction": 1}],
            },
        ],
    )
    our_fills = [
        _our_fill("buy", 100, "190.00", "2026-02-11"),
        _our_fill("sell", -100, "200.00", "2026-02-25"),  # $5.20 too high
    ]
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=orders_path,
        qc_price_history_path=prices_path,
        our_fills=our_fills,
    )
    cats = {d.category for d in report.divergences}
    # Both fire — fill drift is the upstream cause; PNL_DRIFT is the
    # downstream concrete assertion that the P&L claim is honest.
    assert DivergenceCategory.FILL_PRICE_DRIFT in cats
    assert DivergenceCategory.PNL_DRIFT in cats
    assert report.status == "failed"

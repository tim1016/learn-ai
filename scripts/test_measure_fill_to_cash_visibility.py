"""Tests for scripts/measure_fill_to_cash_visibility.py (#2487).

Stdlib-only (unittest), mirroring the script's own zero-dependency design and
``scripts/test_alpaca_onboarding_gates.py``. Every rule that turns wire data
into a visibility verdict is pinned here: the nanosecond timestamp parse, the
fill-event gate, the reflection band, the pre/post-receipt classification
inputs, and the nearest-rank percentiles the committed fixture's distribution
is stated in.

Run directly: ``python3 scripts/test_measure_fill_to_cash_visibility.py``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_fill_to_cash_visibility import (  # noqa: E402
    CashReading,
    FillEvent,
    _floor_to_increment,
    baseline_cash,
    first_reflecting_read,
    parse_fill_event,
    parse_iso_ms,
    read_reflects_fill,
    summarize,
)


def _fill(
    receipt_at_ms: int = 1_000,
    *,
    side: str = "buy",
    qty: float = 1.0,
    price: float = 100.0,
) -> FillEvent:
    return FillEvent(
        receipt_at_ms=receipt_at_ms,
        execution_id="exec-1",
        event_type="fill",
        symbol="BTC/USD",
        side=side,
        qty=qty,
        price=price,
        order_id="order-1",
        broker_at_ms=receipt_at_ms - 10,
        raw={},
    )


class ParseIsoMsTest(unittest.TestCase):
    def test_nanosecond_precision_truncates_to_milliseconds(self) -> None:
        # The exact wire shape of a trade_updates timestamp (9 fractional
        # digits), which datetime.fromisoformat on 3.9 cannot parse at all.
        self.assertEqual(
            parse_iso_ms("2026-09-28T01:59:34.488884708Z"),
            parse_iso_ms("2026-09-28T01:59:34.488884Z"),
        )

    def test_plain_and_offset_forms_parse(self) -> None:
        self.assertIsNotNone(parse_iso_ms("2026-09-28T01:59:34Z"))
        self.assertIsNotNone(parse_iso_ms("2026-09-28T01:59:34.5+02:00"))
        self.assertIsNotNone(parse_iso_ms("2026-09-28T01:59:34"))

    def test_garbage_is_none_never_a_guess(self) -> None:
        self.assertIsNone(parse_iso_ms("not a date"))
        self.assertIsNone(parse_iso_ms(None))
        self.assertIsNone(parse_iso_ms(""))


class ParseFillEventTest(unittest.TestCase):
    def _data(self, **overrides: object) -> dict:
        base = {
            "event": "fill",
            "execution_id": "e1",
            "qty": "0.000174984",
            "price": "84034.69",
            "side": "buy",
            "timestamp": "2026-09-28T01:59:34.488884708Z",
            "order": {"id": "oid", "symbol": "BTC/USD", "side": "buy"},
        }
        base.update(overrides)
        return base

    def test_a_fill_event_parses_with_its_broker_timestamp(self) -> None:
        event = parse_fill_event(self._data(), receipt_at_ms=5_000)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.order_id, "oid")
        self.assertAlmostEqual(event.qty, 0.000174984)
        self.assertAlmostEqual(event.price, 84034.69)
        self.assertEqual(event.receipt_at_ms, 5_000)
        self.assertIsNotNone(event.broker_at_ms)
        self.assertAlmostEqual(event.notional_usd, event.qty * event.price)

    def test_non_fills_and_malformed_payloads_are_skipped(self) -> None:
        self.assertIsNone(parse_fill_event({"event": "new"}, 1))
        self.assertIsNone(parse_fill_event(self._data(qty=None), 1))
        self.assertIsNone(parse_fill_event(self._data(price="NaN"), 1))
        self.assertIsNone(parse_fill_event(self._data(qty="-1"), 1))
        self.assertIsNone(parse_fill_event(self._data(order={}), 1))
        # partial_fill is a real execution and must parse.
        self.assertIsNotNone(parse_fill_event(self._data(event="partial_fill"), 1))


class ReflectionTest(unittest.TestCase):
    def test_buys_reflect_below_and_sells_above_the_band(self) -> None:
        buy = _fill(side="buy")
        self.assertTrue(read_reflects_fill(890.0, buy, 1_000.0))
        self.assertFalse(read_reflects_fill(910.01, buy, 1_000.0))
        sell = _fill(side="sell")
        self.assertTrue(read_reflects_fill(1_090.0, sell, 1_000.0))
        self.assertFalse(read_reflects_fill(1_089.99, sell, 1_000.0))

    def test_a_sell_fee_inside_the_band_still_counts_as_reflected(self) -> None:
        # An equity sell's regulatory fee is well inside the 10% band.
        sell = _fill(side="sell")
        self.assertTrue(read_reflects_fill(1_099.98, sell, 1_000.0))

    def test_baseline_is_the_last_read_strictly_before_the_instant(self) -> None:
        readings = [
            CashReading(issued_at_ms=600, answered_at_ms=650, cash=1_000.0),
            CashReading(issued_at_ms=900, answered_at_ms=950, cash=None),  # failed read
            CashReading(issued_at_ms=1_100, answered_at_ms=1_150, cash=900.0),
        ]
        self.assertEqual(baseline_cash(readings, 1_000), 1_000.0)
        self.assertEqual(baseline_cash(readings, 700), 1_000.0)
        self.assertIsNone(baseline_cash(readings, 500))


class FirstReflectingReadTest(unittest.TestCase):
    def test_a_transition_after_receipt_measures_the_delay(self) -> None:
        fill = _fill(receipt_at_ms=1_000)
        readings = [
            CashReading(600, 650, 1_000.0),
            CashReading(1_100, 1_150, 1_000.0),
            CashReading(1_500, 1_550, 890.0),
        ]
        reading = first_reflecting_read(fill, readings, 1_000.0, from_ms=600)
        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertEqual(reading.issued_at_ms, 1_500)
        self.assertEqual(reading.issued_at_ms - fill.receipt_at_ms, 500)

    def test_a_transition_before_receipt_is_the_cash_lead_case(self) -> None:
        # The dominant observation of the 2026-09-28 paper run: cash lands
        # before the stream event. The scan, anchored before the order's
        # submission, still finds it -- and the caller classifies it as
        # visible_before_receipt rather than never visible.
        fill = _fill(receipt_at_ms=1_000)
        readings = [
            CashReading(600, 650, 1_000.0),
            CashReading(700, 750, 890.0),
            CashReading(1_100, 1_150, 890.0),
        ]
        reading = first_reflecting_read(fill, readings, 1_000.0, from_ms=600)
        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertEqual(reading.issued_at_ms, 700)
        self.assertLess(reading.issued_at_ms, fill.receipt_at_ms)

    def test_no_transition_is_none(self) -> None:
        fill = _fill(receipt_at_ms=1_000)
        readings = [CashReading(600, 650, 1_000.0), CashReading(1_200, 1_250, 1_000.0)]
        self.assertIsNone(first_reflecting_read(fill, readings, 1_000.0, from_ms=600))


class SummarizeTest(unittest.TestCase):
    def test_nearest_rank_percentiles(self) -> None:
        delays = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1_000]
        summary = summarize(delays)
        self.assertEqual(summary["count"], 10)
        self.assertEqual(summary["min_ms"], 100)
        self.assertEqual(summary["p50_ms"], 500)
        self.assertEqual(summary["p90_ms"], 900)
        self.assertEqual(summary["p95_ms"], 1_000)
        self.assertEqual(summary["max_ms"], 1_000)

    def test_empty_input_reports_nothing_rather_than_zero(self) -> None:
        summary = summarize([])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["max_ms"])


class FloorToIncrementTest(unittest.TestCase):
    def test_floors_within_floating_point_noise(self) -> None:
        self.assertEqual(_floor_to_increment(0.000175579, 0.000000001), 0.000175579)
        self.assertEqual(_floor_to_increment(0.000174984, 0.00001), 0.00017)


if __name__ == "__main__":
    unittest.main()

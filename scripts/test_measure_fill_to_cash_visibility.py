"""Tests for scripts/measure_fill_to_cash_visibility.py (#2487).

Stdlib-only (unittest), mirroring the script's own zero-dependency design and
``scripts/test_alpaca_onboarding_gates.py``. Every rule that turns wire data
into a visibility verdict is pinned here: the nanosecond timestamp parse, the
fill-event gate (including side validation and temporal normalization), the
reflection band, the answered-time visibility classification, the
settled-reads identity, the event requeue, the passive overlap censoring,
and the nearest-rank percentiles the committed fixture's distribution is
stated in.

Run directly: ``python3 scripts/test_measure_fill_to_cash_visibility.py``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_fill_to_cash_visibility import (
    AccountPoller,
    CashReading,
    FillEvent,
    FillSink,
    _await_order_fill_events,
    _await_settled_reads,
    _classify_visibility,
    _correlate_passive_fills,
    _floor_to_increment,
    baseline_cash,
    first_reflecting_read,
    normalize_raw_event,
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
            "order": {
                "id": "oid",
                "symbol": "BTC/USD",
                "side": "buy",
                "created_at": "2026-09-28T01:59:34Z",
            },
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

    def test_a_drifted_side_is_skipped_not_guessed(self) -> None:
        # Every non-"buy" side would be treated as a sell by the reflection
        # band, silently reversing the direction the measurement asserts.
        self.assertIsNone(parse_fill_event(self._data(side="", **{"order": {"id": "o", "symbol": "X"}}), 1))
        self.assertIsNone(parse_fill_event(self._data(side="BUYSELL"), 1))

    def test_raw_temporal_fields_are_normalized_to_int_ms(self) -> None:
        event = parse_fill_event(self._data(), receipt_at_ms=5_000)
        self.assertIsNotNone(event)
        assert event is not None
        raw = event.raw
        self.assertNotIn("timestamp", raw)
        self.assertEqual(raw["timestamp_ms"], parse_iso_ms("2026-09-28T01:59:34.488884708Z"))
        self.assertNotIn("created_at", raw["order"])
        self.assertIn("created_at_ms", raw["order"])
        # Non-temporal fields keep their wire values.
        self.assertEqual(raw["qty"], "0.000174984")


class NormalizeRawEventTest(unittest.TestCase):
    def test_unparseable_temporal_strings_are_dropped_not_stored_as_iso(self) -> None:
        normalized = normalize_raw_event(
            {"timestamp": "2026-09-28T01:59:34Z", "filled_at": "not a date", "qty": "1"}
        )
        self.assertEqual(normalized["timestamp_ms"], parse_iso_ms("2026-09-28T01:59:34Z"))
        self.assertNotIn("filled_at", normalized)
        self.assertEqual(normalized["qty"], "1")

    def test_at_key_is_temporal_and_recurses(self) -> None:
        normalized = normalize_raw_event(
            {"at": "2026-09-28T01:59:34.5Z", "order": {"submitted_at": "2026-09-28T01:59:33Z"}}
        )
        self.assertIn("at_ms", normalized)
        self.assertIn("submitted_at_ms", normalized["order"])


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


class ClassifyVisibilityTest(unittest.TestCase):
    """What a reflecting read proves depends on where its ANSWER landed."""

    def test_a_read_answered_before_the_receipt_provably_led_the_stream(self) -> None:
        fill = _fill(receipt_at_ms=1_000)
        reading = CashReading(issued_at_ms=600, answered_at_ms=976, cash=890.0)
        status, bound = _classify_visibility(fill, reading)
        self.assertEqual(status, "led_stream")
        self.assertEqual(bound, 0)

    def test_a_read_issued_at_or_after_the_receipt_binds_at_issue_time(self) -> None:
        fill = _fill(receipt_at_ms=1_000)
        reading = CashReading(issued_at_ms=1_500, answered_at_ms=1_800, cash=890.0)
        status, bound = _classify_visibility(fill, reading)
        self.assertEqual(status, "resolved")
        self.assertEqual(bound, 500)

    def test_a_read_straddling_the_receipt_is_interval_censored_at_the_answer(self) -> None:
        # Issued before the receipt, answered after it: the cash may have
        # been visible before the receipt, but the answer cannot prove that —
        # the bound is the answer time, never zero (#2549 review P1).
        fill = _fill(receipt_at_ms=1_000)
        reading = CashReading(issued_at_ms=700, answered_at_ms=1_329, cash=890.0)
        status, bound = _classify_visibility(fill, reading)
        self.assertEqual(status, "interval_censored_at_receipt")
        self.assertEqual(bound, 329)


def _poller_with_readings(*readings: CashReading) -> AccountPoller:
    poller = AccountPoller(rest=None, interval_ms=1_000)  # type: ignore[arg-type]
    poller.readings = list(readings)
    return poller


def _deliver_readings(poller: AccountPoller, *readings: CashReading, delay: float = 0.05) -> None:
    """Append readings over time, as the live poller thread would."""
    import threading
    import time as _time

    def run() -> None:
        for reading in readings:
            _time.sleep(delay)
            poller.readings.append(reading)

    threading.Thread(target=run, daemon=True).start()


class AwaitSettledReadsTest(unittest.TestCase):
    def test_only_new_readings_count_towards_stability(self) -> None:
        # The settle loop wakes faster than the poller reads; counting an
        # unchanged snapshot twice would certify one read as two (#2549
        # review P1). One delivered read alone must never satisfy
        # equal_reads=2, however long the loop stares at it.
        poller = _poller_with_readings()
        _deliver_readings(poller, CashReading(600, 650, 1_000.0))
        self.assertFalse(_await_settled_reads(poller, equal_reads=2, timeout_s=0.4))

    def test_two_consecutive_equal_new_readings_settle(self) -> None:
        poller = _poller_with_readings()
        _deliver_readings(
            poller,
            CashReading(600, 650, 1_000.0),
            CashReading(900, 950, 1_000.0),
        )
        self.assertTrue(_await_settled_reads(poller, equal_reads=2, timeout_s=2.0))

    def test_a_changed_level_resets_the_count(self) -> None:
        poller = _poller_with_readings()
        _deliver_readings(
            poller,
            CashReading(600, 650, 1_000.0),
            CashReading(900, 950, 900.0),
            CashReading(1_200, 1_250, 900.0),
        )
        self.assertTrue(_await_settled_reads(poller, equal_reads=2, timeout_s=2.0))
        poller = _poller_with_readings()
        _deliver_readings(
            poller,
            CashReading(600, 650, 1_000.0),
            CashReading(900, 950, 900.0),
        )
        self.assertFalse(_await_settled_reads(poller, equal_reads=2, timeout_s=0.4))


class AwaitOrderFillEventsTest(unittest.TestCase):
    def test_trailing_fills_are_collected_and_other_events_requeued(self) -> None:
        sink = FillSink()
        head = parse_fill_event(
            {
                "event": "partial_fill",
                "qty": "0.0001",
                "price": "100",
                "side": "buy",
                "order": {"id": "o1", "symbol": "BTC/USD"},
            },
            receipt_at_ms=1_000,
        )
        self.assertIsNotNone(head)

        def push(order_id: str, event_type: str) -> None:
            sink.push(
                {
                    "event": event_type,
                    "qty": "0.0001",
                    "price": "100",
                    "side": "buy",
                    "order": {"id": order_id, "symbol": "BTC/USD"},
                },
                1_000,
            )

        push("o1", "partial_fill")

        import threading

        def deliver_trailing() -> None:
            import time as _time

            _time.sleep(0.05)
            push("o1", "fill")  # the completing fill, in a later drain
            push("o2", "fill")  # the next order's fill, must be requeued

        threading.Thread(target=deliver_trailing, daemon=True).start()
        events = _await_order_fill_events(sink, "o1", timeout_s=3.0, trailing_grace_s=0.15)
        self.assertEqual([event.event_type for event in events], ["partial_fill", "fill"])
        requeued = sink.drain()
        self.assertEqual(len(requeued), 1)
        self.assertEqual(requeued[0][0]["order"]["id"], "o2")


class CorrelatePassiveFillsTest(unittest.TestCase):
    def test_overlapping_attribution_windows_censor_both_fills(self) -> None:
        # Two fills whose windows overlap: the shared cash series cannot
        # attribute a step to either one alone (#2549 review P1).
        early = _fill(receipt_at_ms=2_000)
        late = _fill(receipt_at_ms=2_400)
        readings = [
            CashReading(1_000, 1_100, 10_000.0),  # baseline for both
            CashReading(2_600, 2_700, 9_800.0),   # one combined drop
        ]
        records = _correlate_passive_fills([early, late], readings, visibility_timeout_s=30.0)
        self.assertEqual([record["status"] for record in records], [
            "overlapping_fills",
            "overlapping_fills",
        ])

    def test_isolated_fills_keep_their_classification(self) -> None:
        first = _fill(receipt_at_ms=1_000)
        second = _fill(receipt_at_ms=5_000)
        readings = [
            CashReading(600, 650, 10_000.0),
            CashReading(1_500, 1_600, 9_800.0),   # first reflected
            CashReading(4_000, 4_100, 9_800.0),   # settled
            CashReading(5_200, 5_300, 9_600.0),   # second reflected
        ]
        records = _correlate_passive_fills([first, second], readings, visibility_timeout_s=30.0)
        self.assertEqual(records[0]["status"], "resolved")
        self.assertEqual(records[0]["visibility_bound_ms"], 500)
        self.assertEqual(records[1]["status"], "resolved")
        self.assertEqual(records[1]["visibility_bound_ms"], 200)


if __name__ == "__main__":
    unittest.main()

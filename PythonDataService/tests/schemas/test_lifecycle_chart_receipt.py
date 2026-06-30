"""Schema tests for LifecycleChartReceipt and LifecycleChartNode node-receipt fields.

Covers the ts_ms / ts_ms_resolved timestamp-resolution contract added in the
lifecycle node receipts slice, plus the LifecycleChartReceipt model itself.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.live_runs import LifecycleChartNode, LifecycleChartReceipt


# ---------------------------------------------------------------------------
# LifecycleChartReceipt — construction

class TestLifecycleChartReceiptConstruction:
    def test_minimal_receipt_with_no_timestamp(self) -> None:
        receipt = LifecycleChartReceipt(label="state", value="PASSED")

        assert receipt.label == "state"
        assert receipt.value == "PASSED"
        assert receipt.unit is None
        assert receipt.source is None
        assert receipt.gate_id is None
        assert receipt.ts_ms is None
        assert receipt.ts_ms_resolved is False

    def test_receipt_with_resolved_timestamp(self) -> None:
        receipt = LifecycleChartReceipt(
            label="last_reconcile_ms",
            value="1700000000000",
            unit="ms UTC",
            source="reconciliation_projection",
            ts_ms=1_700_000_000_000,
            ts_ms_resolved=True,
        )

        assert receipt.ts_ms == 1_700_000_000_000
        assert receipt.ts_ms_resolved is True
        assert receipt.unit == "ms UTC"
        assert receipt.source == "reconciliation_projection"

    def test_receipt_with_gate_id(self) -> None:
        receipt = LifecycleChartReceipt(
            label="gate_result",
            value="pass",
            gate_id="engine_ready",
            ts_ms=1_700_000_000_000,
            ts_ms_resolved=True,
        )

        assert receipt.gate_id == "engine_ready"

    def test_receipt_accepts_zero_timestamp(self) -> None:
        receipt = LifecycleChartReceipt(
            label="epoch",
            value="0",
            ts_ms=0,
            ts_ms_resolved=True,
        )

        assert receipt.ts_ms == 0
        assert receipt.ts_ms_resolved is True

    def test_receipt_accepts_max_timestamp(self) -> None:
        max_ts = 9_223_372_036_854_775_807
        receipt = LifecycleChartReceipt(
            label="ts",
            value=str(max_ts),
            ts_ms=max_ts,
            ts_ms_resolved=True,
        )

        assert receipt.ts_ms == max_ts


# ---------------------------------------------------------------------------
# LifecycleChartReceipt — timestamp-resolution contract violations

class TestLifecycleChartReceiptTimestampContract:
    def test_ts_ms_resolved_true_without_ts_ms_raises(self) -> None:
        with pytest.raises(ValidationError, match="ts_ms_resolved cannot be true when ts_ms is absent"):
            LifecycleChartReceipt(
                label="state",
                value="PASSED",
                ts_ms=None,
                ts_ms_resolved=True,
            )

    def test_ts_ms_present_without_ts_ms_resolved_raises(self) -> None:
        with pytest.raises(ValidationError, match="ts_ms_resolved=false is reserved for absent timestamps"):
            LifecycleChartReceipt(
                label="state",
                value="PASSED",
                ts_ms=1_700_000_000_000,
                ts_ms_resolved=False,
            )

    def test_ts_ms_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            LifecycleChartReceipt(
                label="state",
                value="PASSED",
                ts_ms=-1,
                ts_ms_resolved=True,
            )

    def test_ts_ms_exceeds_max_raises(self) -> None:
        with pytest.raises(ValidationError):
            LifecycleChartReceipt(
                label="state",
                value="PASSED",
                ts_ms=9_223_372_036_854_775_808,
                ts_ms_resolved=True,
            )

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LifecycleChartReceipt.model_validate(
                {"label": "state", "value": "PASSED", "unknown_field": "rejected"}
            )


# ---------------------------------------------------------------------------
# LifecycleChartReceipt — model_validate round-trip

class TestLifecycleChartReceiptModelValidate:
    def test_round_trip_from_dict_no_timestamp(self) -> None:
        data = {"label": "reconciliation.state", "value": "FAILED"}
        receipt = LifecycleChartReceipt.model_validate(data)

        assert receipt.label == "reconciliation.state"
        assert receipt.value == "FAILED"
        assert receipt.ts_ms is None
        assert receipt.ts_ms_resolved is False

    def test_round_trip_from_dict_with_timestamp(self) -> None:
        data = {
            "label": "event_type",
            "value": "BrokerOrderUncertain",
            "source": "intent_wal",
            "ts_ms": 1_700_000_001_000,
            "ts_ms_resolved": True,
        }
        receipt = LifecycleChartReceipt.model_validate(data)

        assert receipt.label == "event_type"
        assert receipt.source == "intent_wal"
        assert receipt.ts_ms == 1_700_000_001_000
        assert receipt.ts_ms_resolved is True

    def test_model_dump_preserves_all_fields(self) -> None:
        receipt = LifecycleChartReceipt(
            label="failure_reason",
            value="Broker snapshot disagrees with the intent WAL.",
            source="reconciliation_projection",
            ts_ms=1_700_000_000_000,
            ts_ms_resolved=True,
        )
        dumped = receipt.model_dump()

        assert dumped["label"] == "failure_reason"
        assert dumped["value"] == "Broker snapshot disagrees with the intent WAL."
        assert dumped["source"] == "reconciliation_projection"
        assert dumped["ts_ms"] == 1_700_000_000_000
        assert dumped["ts_ms_resolved"] is True
        assert dumped["unit"] is None
        assert dumped["gate_id"] is None


# ---------------------------------------------------------------------------
# LifecycleChartNode — ts_ms / ts_ms_resolved / receipts fields

class TestLifecycleChartNodeReceiptFields:
    def _base_node_kwargs(self) -> dict:
        return {
            "id": "reconcile",
            "label": "Reconcile",
            "lane": "bot",
            "status": "passed",
            "status_label": "Passed",
        }

    def test_node_defaults_to_empty_receipt_fields(self) -> None:
        node = LifecycleChartNode(**self._base_node_kwargs())

        assert node.ts_ms is None
        assert node.ts_ms_resolved is False
        assert node.receipts == []

    def test_node_with_resolved_timestamp(self) -> None:
        node = LifecycleChartNode(
            **self._base_node_kwargs(),
            ts_ms=1_700_000_000_000,
            ts_ms_resolved=True,
        )

        assert node.ts_ms == 1_700_000_000_000
        assert node.ts_ms_resolved is True

    def test_node_with_receipts_list(self) -> None:
        receipt = LifecycleChartReceipt(
            label="reconciliation.state",
            value="PASSED",
            source="reconciliation_projection",
            ts_ms=1_700_000_000_000,
            ts_ms_resolved=True,
        )
        node = LifecycleChartNode(
            **self._base_node_kwargs(),
            ts_ms=1_700_000_000_000,
            ts_ms_resolved=True,
            receipts=[receipt],
        )

        assert len(node.receipts) == 1
        assert node.receipts[0].label == "reconciliation.state"

    def test_node_ts_ms_resolved_true_without_ts_ms_raises(self) -> None:
        with pytest.raises(ValidationError, match="ts_ms_resolved cannot be true when ts_ms is absent"):
            LifecycleChartNode(
                **self._base_node_kwargs(),
                ts_ms=None,
                ts_ms_resolved=True,
            )

    def test_node_ts_ms_present_without_ts_ms_resolved_raises(self) -> None:
        with pytest.raises(ValidationError, match="ts_ms_resolved=false is reserved for absent timestamps"):
            LifecycleChartNode(
                **self._base_node_kwargs(),
                ts_ms=1_700_000_000_000,
                ts_ms_resolved=False,
            )

    def test_node_ts_ms_zero_is_valid(self) -> None:
        node = LifecycleChartNode(
            **self._base_node_kwargs(),
            ts_ms=0,
            ts_ms_resolved=True,
        )

        assert node.ts_ms == 0

    def test_node_ts_ms_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            LifecycleChartNode(
                **self._base_node_kwargs(),
                ts_ms=-1,
                ts_ms_resolved=True,
            )

    def test_node_receipts_default_factory_produces_distinct_lists(self) -> None:
        node_a = LifecycleChartNode(**self._base_node_kwargs())
        node_b = LifecycleChartNode(**self._base_node_kwargs())

        # Modifying one list must not affect the other.
        node_a.receipts.append(
            LifecycleChartReceipt(label="x", value="y")
        )
        assert len(node_b.receipts) == 0

    def test_node_extra_field_rejected(self) -> None:
        data = {**self._base_node_kwargs(), "unknown_key": "bad"}
        with pytest.raises(ValidationError):
            LifecycleChartNode.model_validate(data)

    def test_node_multiple_receipts_round_trip(self) -> None:
        receipts_data = [
            {"label": "reconciliation.state", "value": "FAILED"},
            {
                "label": "last_reconcile_ms",
                "value": "1700000000000",
                "unit": "ms UTC",
                "source": "reconciliation_projection",
                "ts_ms": 1_700_000_000_000,
                "ts_ms_resolved": True,
            },
        ]
        data = {
            **self._base_node_kwargs(),
            "ts_ms": 1_700_000_000_000,
            "ts_ms_resolved": True,
            "receipts": receipts_data,
        }
        node = LifecycleChartNode.model_validate(data)

        assert len(node.receipts) == 2
        assert node.receipts[0].label == "reconciliation.state"
        assert node.receipts[1].unit == "ms UTC"
        assert node.receipts[1].ts_ms_resolved is True
from __future__ import annotations

from collections import namedtuple

import pytest

from app.broker.ibkr.api_evidence import (
    IbkrApiEvidenceRecorder,
    evidence_request,
    evidence_response,
)


def test_evidence_recorder_backfills_after_seq() -> None:
    recorder = IbkrApiEvidenceRecorder()
    first = recorder.record(
        source="test.first",
        request=evidence_request("reqMktData", symbol="SPY"),
        response=evidence_response("tickSnapshot", fields={"bid": 1.0}),
    )
    second = recorder.record(
        source="test.second",
        request=evidence_request("reqPnL", account="DU123", modelCode=""),
        response=evidence_response("pnl", fields={"dailyPnL": 2.0}),
    )

    assert first.seq == 1
    assert second.seq == 2
    assert recorder.backfill(after_seq=1) == [second]


def test_evidence_recorder_retains_more_than_default_activity_backfill_window() -> None:
    recorder = IbkrApiEvidenceRecorder()

    for i in range(1_005):
        recorder.record(
            source=f"test.bulk.{i}",
            request=evidence_request("reqMktData", symbol="SPY"),
        )

    events = recorder.backfill(after_seq=0, limit=1_005)
    assert len(events) == 1_005
    assert events[0].seq == 1


def test_evidence_recorder_clear_resets_events_and_sequence() -> None:
    recorder = IbkrApiEvidenceRecorder()
    recorder.record(source="test.clear", request=evidence_request("reqPositionsAsync"))

    recorder.clear()
    event = recorder.record(source="test.after_clear", request=evidence_request("reqPnL"))

    assert event.seq == 1
    assert recorder.backfill(after_seq=0) == [event]


@pytest.mark.asyncio
async def test_evidence_recorder_broadcasts_to_subscribers() -> None:
    recorder = IbkrApiEvidenceRecorder()
    subscription = recorder.subscribe()

    event = recorder.record(
        source="test.broadcast",
        request=evidence_request("reqPositionsAsync"),
        response=evidence_response("position", fields={"row_count": 1}),
    )

    assert await subscription.queue.get() == event
    recorder.unsubscribe(subscription)
    assert await subscription.queue.get() is None


def test_evidence_response_snapshots_namedtuple_account_value() -> None:
    account_value = namedtuple("AccountValue", "account tag value currency")(
        account="DU1234567",
        tag="NetLiquidation",
        value="100123.45",
        currency="USD",
    )

    response = evidence_response(
        "accountSummary",
        fields={"row_count": 1},
        objects=[account_value],
    )

    assert response.fields["row_count"] == 1
    assert response.fields["object_0"] == {
        "object_type": "tests.broker.ibkr.test_api_evidence.AccountValue",
        "fields": {
            "account": "DU1234567",
            "tag": "NetLiquidation",
            "value": "100123.45",
            "currency": "USD",
        },
    }

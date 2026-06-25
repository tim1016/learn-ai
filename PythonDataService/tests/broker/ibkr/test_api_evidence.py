from __future__ import annotations

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

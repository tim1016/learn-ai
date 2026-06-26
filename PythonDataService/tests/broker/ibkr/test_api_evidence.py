from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.broker.ibkr.api_evidence import (
    IbkrApiEvidenceRecorder,
    evidence_request,
    evidence_response,
)


@dataclass(frozen=True)
class FakeContract:
    conId: int
    symbol: str


@dataclass(frozen=True)
class FakeExecution:
    execId: str
    permId: int
    contract: FakeContract


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
    assert response.serializer_warnings == []


def test_evidence_response_snapshots_ibkr_object_matrix() -> None:
    account_value = namedtuple("AccountValue", "account tag value currency")(
        account="DU1234567",
        tag="NetLiquidation",
        value="100123.45",
        currency="USD",
    )
    position = SimpleNamespace(
        account="DU1234567",
        contract=FakeContract(conId=756733, symbol="SPY"),
        position=1.0,
        avgCost=732.63,
    )
    portfolio_item = SimpleNamespace(
        contract=FakeContract(conId=756733, symbol="SPY"),
        position=1.0,
        marketPrice=733.0,
        marketValue=733.0,
    )
    execution = FakeExecution(
        execId="00025b49.6a44f082.01.01",
        permId=535649342,
        contract=FakeContract(conId=756733, symbol="SPY"),
    )
    fill = SimpleNamespace(
        contract=FakeContract(conId=756733, symbol="SPY"),
        execution=execution,
        time=None,
    )
    commission_report = namedtuple("CommissionReport", "execId commission currency")(
        execId="00025b49.6a44f082.01.01",
        commission=0.35,
        currency="USD",
    )

    response = evidence_response(
        "execDetails",
        objects=[
            account_value,
            position,
            portfolio_item,
            execution,
            fill,
            commission_report,
        ],
    )

    assert response.fields["object_0"]["fields"]["tag"] == "NetLiquidation"
    assert response.fields["object_1"]["fields"]["contract"]["symbol"] == "SPY"
    assert response.fields["object_2"]["fields"]["marketValue"] == 733.0
    assert response.fields["object_3"]["fields"]["execId"] == "00025b49.6a44f082.01.01"
    assert response.fields["object_4"]["fields"]["execution"]["permId"] == 535649342
    assert response.fields["object_5"]["fields"]["commission"] == 0.35


def test_evidence_response_unknown_object_is_placeholder_not_crash(caplog) -> None:
    class UnsupportedEvidenceObject:
        __slots__ = ("value",)

        def __init__(self) -> None:
            self.value = "opaque"

    response = evidence_response(
        "position",
        fields={"row_count": 1},
        objects=[UnsupportedEvidenceObject()],
    )

    placeholder = response.fields["object_0"]
    assert placeholder["object_type"].endswith("UnsupportedEvidenceObject")
    assert placeholder["fields"]["serializer_error"].startswith(
        "Cannot snapshot unsupported IBKR evidence object"
    )
    assert len(response.serializer_warnings) == 1
    assert response.serializer_warnings[0].object_type.endswith("UnsupportedEvidenceObject")
    assert response.serializer_warnings[0].serializer_error.startswith(
        "Cannot snapshot unsupported IBKR evidence object"
    )
    assert response.fields["row_count"] == 1
    assert "Cannot snapshot unsupported IBKR evidence object" in caplog.text


def test_evidence_response_conversion_error_is_placeholder_not_crash(caplog) -> None:
    class ExplodingDatetime(datetime):
        def timestamp(self) -> float:
            raise ValueError("timestamp outside supported range")

    response = evidence_response(
        "position",
        objects=[
            SimpleNamespace(
                observed_at=ExplodingDatetime(2026, 6, 25, tzinfo=UTC),
            )
        ],
    )

    placeholder = response.fields["object_0"]
    assert placeholder["fields"]["serializer_error"] == "timestamp outside supported range"
    assert len(response.serializer_warnings) == 1
    assert response.serializer_warnings[0].serializer_error == "timestamp outside supported range"
    assert "timestamp outside supported range" in caplog.text

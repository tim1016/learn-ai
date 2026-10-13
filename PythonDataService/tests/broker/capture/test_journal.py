"""Tests for the broker-neutral capture journal (Broker System v2, ADR 0032)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.broker.capture.journal import (
    REDACTED,
    CaptureEndpoint,
    CaptureJournal,
    get_capture_journal,
    reset_capture_journal_for_testing,
)

# A fixed instant so day-file names and captured_at_ms are deterministic.
_FIXED_MS = 1_700_000_000_000
_DAY = datetime.fromtimestamp(_FIXED_MS / 1000, tz=UTC).strftime("%Y-%m-%d")


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _fixed(tmp_path: Path) -> CaptureJournal:
    return CaptureJournal(capture_dir=tmp_path, clock=lambda: _FIXED_MS)


def test_record_round_trips_verbatim_utf8_body(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)
    body = b'{"account_number":"PA12345","cash":"1000.50","status":"ACTIVE"}'

    assert journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ACCOUNT,
        method="get",
        params={"nested": True},
        status=200,
        raw_body=body,
    )

    path = tmp_path / "alpaca" / "account" / f"{_DAY}.jsonl"
    [record] = _read_records(path)
    assert record["raw_body"].encode("utf-8") == body
    assert "body_encoding" not in record
    assert record["status"] == 200
    assert record["captured_at_ms"] == _FIXED_MS
    assert record["method"] == "GET"
    assert record["broker"] == "alpaca"


def test_utc_day_rotation_writes_separate_day_files(tmp_path: Path) -> None:
    times = iter([_FIXED_MS, _FIXED_MS + 86_400_000])
    journal = CaptureJournal(capture_dir=tmp_path, clock=lambda: next(times))

    for _ in range(2):
        journal.record(
            broker="alpaca",
            endpoint=CaptureEndpoint.POSITIONS,
            method="get",
            params={},
            status=200,
            raw_body=b"[]",
        )

    files = sorted((tmp_path / "alpaca" / "positions").glob("*.jsonl"))
    assert len(files) == 2


def test_error_responses_are_captured(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ACCOUNT,
        method="get",
        params={},
        status=403,
        raw_body=b'{"message":"forbidden"}',
    )

    [record] = _read_records(tmp_path / "alpaca" / "account" / f"{_DAY}.jsonl")
    assert record["status"] == 403
    assert json.loads(record["raw_body"])["message"] == "forbidden"


def test_non_utf8_body_falls_back_to_base64(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)
    body = b"\xff\xfe\x00\x01not-text"

    journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.CLOCK,
        method="get",
        params={},
        status=200,
        raw_body=body,
    )

    [record] = _read_records(tmp_path / "alpaca" / "clock" / f"{_DAY}.jsonl")
    assert record["body_encoding"] == "base64"
    assert base64.b64decode(record["raw_body"]) == body


def test_secret_like_params_are_redacted(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ORDERS,
        method="get",
        params={
            "symbol": "AAPL",
            "APCA_API_KEY_ID": "PKLIVEKEY",
            "secret": "shh",
            "authorization": "Bearer x",
            "filters": {
                "access_token": "nested-token",
                "symbols": ["MSFT", {"api_secret": "nested-secret"}],
            },
        },
        status=200,
        raw_body=b"[]",
    )

    [record] = _read_records(tmp_path / "alpaca" / "orders" / f"{_DAY}.jsonl")
    params = record["params"]
    assert params["symbol"] == "AAPL"
    assert params["APCA_API_KEY_ID"] == REDACTED
    assert params["secret"] == REDACTED
    assert params["authorization"] == REDACTED
    assert params["filters"]["access_token"] == REDACTED
    assert params["filters"]["symbols"][0] == "MSFT"
    assert params["filters"]["symbols"][1]["api_secret"] == REDACTED


def test_unsafe_broker_component_is_nonfatal_and_counted(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    ok = journal.record(
        broker="../../etc",
        endpoint=CaptureEndpoint.ACCOUNT,
        method="get",
        params={},
        status=200,
        raw_body=b"{}",
    )

    assert ok is False
    assert journal.failure_count == 1
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_multiple_records_append_to_same_day_file(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    for _ in range(3):
        journal.record(
            broker="alpaca",
            endpoint=CaptureEndpoint.ACTIVITIES,
            method="get",
            params={},
            status=200,
            raw_body=b"[]",
        )

    [path] = list((tmp_path / "alpaca" / "activities").glob("*.jsonl"))
    assert len(_read_records(path)) == 3


def test_a_line_of_an_exchange_carries_when_its_request_began_and_when_its_answer_ended(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    assert journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ORDERS,
        method="post",
        params={},
        status=200,
        raw_body=b"{}",
        began_at_ms=_FIXED_MS - 250,
        ended_at_ms=_FIXED_MS - 10,
    )

    [record] = _read_records(tmp_path / "alpaca" / "orders" / f"{_DAY}.jsonl")
    # The line was written at the clock's instant, as it always was; the exchange's own times are beside it.
    assert (record["began_at_ms"], record["ended_at_ms"], record["captured_at_ms"]) == (
        _FIXED_MS - 250,
        _FIXED_MS - 10,
        _FIXED_MS,
    )


def test_a_line_with_no_exchange_times_and_no_request_body_is_written_as_it_always_was(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.STREAM,
        method="WS",
        params={"stream": "trade_updates"},
        status=0,
        raw_body=b"{}",
    )

    [record] = _read_records(tmp_path / "alpaca" / "stream" / f"{_DAY}.jsonl")
    assert set(record) == {"broker", "endpoint", "method", "params", "status", "captured_at_ms", "raw_body"}


@pytest.mark.parametrize(
    ("began", "ended"),
    [(1, None), (None, 1), (5, 4), (-1, 3), (True, 3), ("1", 3)],
    ids=["no end", "no beginning", "ends before it began", "negative", "a bool", "text"],
)
def test_exchange_times_that_are_not_both_whole_and_in_order_are_refused_and_counted(
    tmp_path: Path, began: object, ended: object
) -> None:
    journal = _fixed(tmp_path)

    ok = journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ORDERS,
        method="post",
        params={},
        status=200,
        raw_body=b"{}",
        began_at_ms=began,  # type: ignore[arg-type]
        ended_at_ms=ended,  # type: ignore[arg-type]
    )

    assert ok is False
    assert journal.failure_count == 1
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_a_utf8_json_request_body_is_kept_with_its_terms_and_the_answer_beside_it(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)
    sent = {"symbol": "SPY261120C00450000", "qty": "1", "limit_price": "1.10"}

    journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ORDERS,
        method="post",
        params={},
        status=200,
        raw_body=b'{"status":"new"}',
        request_body=json.dumps(sent).encode("utf-8"),
    )

    [record] = _read_records(tmp_path / "alpaca" / "orders" / f"{_DAY}.jsonl")
    # The terms as they were sent: a limit of "1.10" is not rewritten as 1.1.
    assert json.loads(record["request_body"]) == sent
    assert '"1.10"' in record["request_body"]
    assert "request_body_encoding" not in record
    assert record["raw_body"] == '{"status":"new"}'


@pytest.mark.parametrize(
    ("sent", "why"),
    [
        # Text a redactor cannot read keys from: the secret would survive as it was.
        (b"api_key=sk-live-123456&symbol=SPY261120C00450000", "not JSON"),
        # JSON in UTF-16 (with or without a byte-order mark): it parses as JSON elsewhere, and would be kept as base64.
        (json.dumps({"api_key": "sk-live-123456", "symbol": "SPY261120C00450000"}).encode("utf-16"), "not UTF-8"),
        (json.dumps({"api_key": "sk-live-123456"}).encode("utf-16-le"), "not JSON"),
        (b"\xff\xfe\x00\x01not-text", "not UTF-8"),
        # A JSON scalar has no keys to redact: a string holding a secret is not an object.
        (b'"api_key=sk-live-123456"', "not a JSON object or array"),
    ],
    ids=["text", "utf-16 with a mark", "utf-16 without a mark", "not text at all", "a json string"],
)
def test_a_request_body_that_cannot_be_redacted_is_dropped_never_written_and_the_drop_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sent: bytes, why: str
) -> None:
    journal = _fixed(tmp_path)

    with caplog.at_level("WARNING", logger="app.broker.capture.journal"):
        ok = journal.record(
            broker="alpaca",
            endpoint=CaptureEndpoint.ORDERS,
            method="post",
            params={},
            status=200,
            raw_body=b'{"status":"accepted"}',
            request_body=sent,
        )

    # The answer is evidence and is written; the body is not, in any encoding.
    assert ok is True
    path = tmp_path / "alpaca" / "orders" / f"{_DAY}.jsonl"
    [record] = _read_records(path)
    assert "request_body" not in record and "request_body_encoding" not in record
    assert record["raw_body"] == '{"status":"accepted"}'
    written = path.read_text(encoding="utf-8")
    assert "sk-live-123456" not in written
    assert base64.b64encode(sent).decode("ascii") not in written
    [dropped] = [entry for entry in caplog.records if getattr(entry, "action", None) == "capture_request_body_dropped"]
    assert (dropped.broker, dropped.endpoint) == ("alpaca", "orders")
    assert why in dropped.reason
    assert journal.failure_count == 0


def test_a_secret_like_key_in_a_request_body_is_redacted_and_the_rest_is_kept(tmp_path: Path) -> None:
    journal = _fixed(tmp_path)

    journal.record(
        broker="alpaca",
        endpoint=CaptureEndpoint.ORDERS,
        method="post",
        params={},
        status=200,
        raw_body=b"{}",
        request_body=b'{"symbol":"SPY261120C00450000","api_secret":"shh","legs":[{"token":"x","side":"buy"}]}',
    )

    [record] = _read_records(tmp_path / "alpaca" / "orders" / f"{_DAY}.jsonl")
    body = json.loads(record["request_body"])
    assert body["symbol"] == "SPY261120C00450000"
    assert body["api_secret"] == REDACTED
    assert body["legs"] == [{"token": REDACTED, "side": "buy"}]
    assert "shh" not in record["request_body"]


def test_capture_dir_read_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_CAPTURE_DIR", str(tmp_path / "from-env"))
    reset_capture_journal_for_testing()
    try:
        journal = get_capture_journal()
        journal.record(
            broker="alpaca",
            endpoint=CaptureEndpoint.ASSETS,
            method="get",
            params={},
            status=200,
            raw_body=b"[]",
        )
        assert (tmp_path / "from-env" / "alpaca" / "assets").is_dir()
    finally:
        reset_capture_journal_for_testing()

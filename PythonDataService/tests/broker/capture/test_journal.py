"""Tests for the broker-neutral capture journal (Broker System v2, §6)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

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
    assert journal.records_written == 1


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
    assert journal.records_written == 0
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
    assert journal.records_written == 3


def test_capture_dir_read_from_env(monkeypatch, tmp_path: Path) -> None:
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

"""Tests for the verbatim capture hook on the alpaca-py requests session.

alpaca-py drives ``requests``; ``responses`` is the only mock that exercises
the real ``requests.Session`` hook path (respx/pytest-httpx cannot). These
tests install the hook on a plain Session and drive it with ``responses``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import responses
from requests.sessions import Session

from app.broker.alpaca.capture_hook import install_capture_hook
from app.broker.capture.journal import CaptureJournal

_FIXED_MS = 1_700_000_000_000
_DAY = datetime.fromtimestamp(_FIXED_MS / 1000, tz=UTC).strftime("%Y-%m-%d")
_BASE = "https://paper-api.alpaca.markets"


def _journal(tmp_path: Path) -> CaptureJournal:
    return CaptureJournal(capture_dir=tmp_path, clock=lambda: _FIXED_MS)


def _records(tmp_path: Path, family: str) -> list[dict]:
    path = tmp_path / "alpaca" / family / f"{_DAY}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@responses.activate
def test_hook_journals_verbatim_success(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal, broker="alpaca")
    body = b'{"account_number":"PA1","status":"ACTIVE"}'
    responses.add(responses.GET, f"{_BASE}/v2/account", body=body, status=200)

    session.get(f"{_BASE}/v2/account")

    [record] = _records(tmp_path, "account")
    assert record["raw_body"].encode("utf-8") == body
    assert record["endpoint"] == "account"
    assert record["status"] == 200
    assert record["method"] == "GET"


@responses.activate
def test_hook_captures_error_responses(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal)
    responses.add(
        responses.GET, f"{_BASE}/v2/positions", body=b'{"message":"forbidden"}', status=403
    )

    session.get(f"{_BASE}/v2/positions")

    [record] = _records(tmp_path, "positions")
    assert record["status"] == 403


@responses.activate
def test_hook_records_query_params(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal)
    responses.add(responses.GET, f"{_BASE}/v2/orders", body=b"[]", status=200)

    session.get(f"{_BASE}/v2/orders", params={"status": "open", "limit": "5"})

    [record] = _records(tmp_path, "orders")
    assert record["params"] == {"status": "open", "limit": "5"}


@responses.activate
def test_activities_path_routes_to_activities_family(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal)
    responses.add(
        responses.GET, f"{_BASE}/v2/account/activities", body=b"[]", status=200
    )

    session.get(f"{_BASE}/v2/account/activities")

    assert len(_records(tmp_path, "activities")) == 1
    assert _records(tmp_path, "account") == []


@responses.activate
def test_unknown_path_is_not_captured(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal)
    responses.add(
        responses.GET, f"{_BASE}/v2/account/portfolio/history", body=b"{}", status=200
    )

    session.get(f"{_BASE}/v2/account/portfolio/history")

    assert list(tmp_path.rglob("*.jsonl")) == []
    assert journal.records_written == 0

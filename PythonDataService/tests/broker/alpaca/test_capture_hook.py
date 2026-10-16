"""Tests for the verbatim capture hook on the alpaca-py requests session.

alpaca-py drives ``requests``; ``responses`` is the only mock that exercises
the real ``requests.Session`` hook path (respx/pytest-httpx cannot). These
tests install the hook on a plain Session and drive it with ``responses``.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
from requests import PreparedRequest, Response
from requests.sessions import Session

from app.broker.alpaca.capture_hook import install_capture_hook
from app.broker.capture.journal import CaptureJournal

_FIXED_MS = 1_700_000_000_000
_DAY = datetime.fromtimestamp(_FIXED_MS / 1000, tz=UTC).strftime("%Y-%m-%d")
_BASE = "https://paper-api.alpaca.markets"
_CALL = "SPY261120C00450000"
_PUT = "SPY261120P00440000"


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


def test_hook_requests_identity_encoding_before_capturing(tmp_path: Path) -> None:
    session = Session()

    install_capture_hook(session, _journal(tmp_path))

    assert session.headers["Accept-Encoding"] == "identity"


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
def test_portfolio_history_path_routes_to_portfolio_history_family(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal)
    responses.add(
        responses.GET, f"{_BASE}/v2/account/portfolio/history", body=b"{}", status=200
    )

    session.get(f"{_BASE}/v2/account/portfolio/history")

    assert len(_records(tmp_path, "portfolio_history")) == 1


@responses.activate
def test_unrecognized_path_is_not_captured(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    session = Session()
    install_capture_hook(session, journal)
    responses.add(responses.GET, f"{_BASE}/v2/watchlists", body=b"[]", status=200)

    session.get(f"{_BASE}/v2/watchlists")

    assert list(tmp_path.rglob("*.jsonl")) == []


# ── an exchange's two times, an option order's body, and the option contract reference (#3102) ──


def _answered(
    url: str, *, method: str = "GET", body: bytes = b"{}", sent: bytes | None = None, waited_ms: int = 0
) -> Response:
    """A response as ``requests`` hands it to a hook: headers arrived ``waited_ms`` after the request was sent."""
    prepared = PreparedRequest()
    prepared.method = method
    prepared.url = url
    prepared.body = sent
    response = Response()
    response.status_code = 200
    response.url = url
    response.request = prepared
    response.elapsed = timedelta(milliseconds=waited_ms)
    response._content = body  # how a test hands a response its body
    return response


def test_a_line_carries_when_the_request_began_and_when_the_answer_ended(tmp_path: Path) -> None:
    session = Session()
    hook = install_capture_hook(session, _journal(tmp_path), clock=lambda: _FIXED_MS)

    hook(_answered(f"{_BASE}/v2/account", waited_ms=250))

    [record] = _records(tmp_path, "account")
    # The headers arrived at the clock's instant, 250 ms after the request was sent; the body read took no time here.
    assert record["began_at_ms"] == _FIXED_MS - 250
    assert record["ended_at_ms"] >= _FIXED_MS
    assert record["captured_at_ms"] == _FIXED_MS


@responses.activate
def test_a_real_exchange_through_the_session_is_journaled_with_its_times_in_order(tmp_path: Path) -> None:
    session = Session()
    install_capture_hook(session, _journal(tmp_path), clock=lambda: _FIXED_MS)
    responses.add(responses.GET, f"{_BASE}/v2/positions", body=b"[]", status=200)

    session.get(f"{_BASE}/v2/positions")

    [record] = _records(tmp_path, "positions")
    assert record["began_at_ms"] <= record["ended_at_ms"]


@responses.activate
def test_a_read_of_the_option_contract_reference_is_journaled_under_its_own_family(tmp_path: Path) -> None:
    session = Session()
    install_capture_hook(session, _journal(tmp_path))
    listed = json.dumps({"symbol": _CALL, "tradable": True, "type": "call"}).encode()
    responses.add(responses.GET, f"{_BASE}/v2/options/contracts/{_CALL}", body=listed, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/options/contracts/NOTLISTED", body=b'{"message":"not found"}', status=404)
    responses.add(responses.GET, f"{_BASE}/v2/options/contracts", body=b'{"option_contracts":[]}', status=200)

    session.get(f"{_BASE}/v2/options/contracts/{_CALL}")
    session.get(f"{_BASE}/v2/options/contracts/NOTLISTED")
    session.get(f"{_BASE}/v2/options/contracts", params={"underlying_symbols": "SPY"})

    records = _records(tmp_path, "option_contracts")
    assert [record["status"] for record in records] == [200, 404, 200]
    assert records[0]["raw_body"].encode("utf-8") == listed
    assert records[2]["params"] == {"underlying_symbols": "SPY"}
    # Not the orders, positions or assets families the path contains words of.
    assert _records(tmp_path, "orders") == _records(tmp_path, "positions") == _records(tmp_path, "assets") == []


@responses.activate
@pytest.mark.parametrize(
    "order",
    [
        {
            "symbol": _CALL, "qty": "1", "side": "buy", "position_intent": "buy_to_open", "type": "limit",
            "time_in_force": "day", "limit_price": "1.10", "client_order_id": "bot-a-1",
        },
        {
            "order_class": "mleg", "qty": "1", "type": "limit", "time_in_force": "day", "limit_price": "-0.30",
            "client_order_id": "bot-a-2",
            "legs": [
                {"symbol": _CALL, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                {"symbol": _PUT, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            ],
        },
    ],
    ids=["one bought option", "a structure of several legs"],
)
def test_an_option_order_submit_keeps_the_body_that_was_sent_and_the_answer_beside_it(
    tmp_path: Path, order: dict
) -> None:
    session = Session()
    install_capture_hook(session, _journal(tmp_path))
    answered = json.dumps({"id": "11111111-1111-1111-1111-111111111111", "status": "accepted"}).encode()
    responses.add(responses.POST, f"{_BASE}/v2/orders", body=answered, status=200)

    session.post(f"{_BASE}/v2/orders", json=order)

    [record] = _records(tmp_path, "orders")
    assert json.loads(record["request_body"]) == order
    assert record["raw_body"].encode("utf-8") == answered
    assert record["method"] == "POST"


@responses.activate
def test_a_stock_order_submit_keeps_no_body(tmp_path: Path) -> None:
    session = Session()
    install_capture_hook(session, _journal(tmp_path))
    responses.add(responses.POST, f"{_BASE}/v2/orders", body=b'{"status":"accepted"}', status=200)

    session.post(
        f"{_BASE}/v2/orders",
        json={"symbol": "SPY", "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"},
    )

    [record] = _records(tmp_path, "orders")
    assert "request_body" not in record


@responses.activate
@pytest.mark.parametrize(
    "sent",
    [
        # JSON of an option order in UTF-16: ``json.loads`` reads it, so it reaches the journal as an order's body.
        json.dumps({"symbol": _CALL, "qty": "1", "api_key": "sk-live-123456"}).encode("utf-16"),
        # Form text that is not JSON at all, on the same path.
        f"symbol={_CALL}&qty=1&api_key=sk-live-123456".encode(),
    ],
    ids=["utf-16 json", "form text"],
)
def test_an_order_body_the_journal_cannot_redact_never_reaches_it_and_the_answer_still_does(
    tmp_path: Path, sent: bytes
) -> None:
    session = Session()
    install_capture_hook(session, _journal(tmp_path))
    responses.add(responses.POST, f"{_BASE}/v2/orders", body=b'{"status":"accepted"}', status=200)

    session.post(f"{_BASE}/v2/orders", data=sent)

    [record] = _records(tmp_path, "orders")
    assert record["raw_body"] == '{"status":"accepted"}'
    assert "request_body" not in record
    [written] = [path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.jsonl")]
    assert "sk-live-123456" not in written
    assert base64.b64encode(sent).decode("ascii") not in written


@responses.activate
def test_a_cancel_and_a_read_of_the_orders_path_keep_no_body(tmp_path: Path) -> None:
    session = Session()
    install_capture_hook(session, _journal(tmp_path))
    responses.add(responses.DELETE, f"{_BASE}/v2/orders/abc", body=b"", status=204)
    responses.add(responses.GET, f"{_BASE}/v2/orders/abc", body=b"{}", status=200)

    session.delete(f"{_BASE}/v2/orders/abc")
    session.get(f"{_BASE}/v2/orders/abc")

    records = _records(tmp_path, "orders")
    assert len(records) == 2
    assert all("request_body" not in record for record in records)


@responses.activate
def test_no_header_is_ever_written_to_the_journal(tmp_path: Path) -> None:
    session = Session()
    session.headers.update({"APCA-API-KEY-ID": "PKTHEKEYIDOFTHEOWNER", "APCA-API-SECRET-KEY": "theownerssecretvalue"})
    install_capture_hook(session, _journal(tmp_path))
    responses.add(responses.POST, f"{_BASE}/v2/orders", body=b'{"status":"accepted"}', status=200)
    responses.add(responses.GET, f"{_BASE}/v2/options/contracts/{_CALL}", body=b"{}", status=200)

    session.post(f"{_BASE}/v2/orders", json={"symbol": _CALL, "qty": "1", "side": "buy", "type": "market"})
    session.get(f"{_BASE}/v2/options/contracts/{_CALL}")

    written = [path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.jsonl")]
    assert len(written) == 2
    for text in written:
        assert "PKTHEKEYIDOFTHEOWNER" not in text
        assert "theownerssecretvalue" not in text
        assert "apca" not in text.lower()
        assert "headers" not in json.loads(text)

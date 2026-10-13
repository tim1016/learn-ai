"""A recording is sanitised before it is committed as a fixture: no account number, no key (#3102).

The rules are the ones ``scripts/hitl_alpaca_capture.py`` has always applied;
they now live in ``app.broker.capture.sanitize`` for the qualification kit's
recordings as well.
"""

from __future__ import annotations

import json

import pytest

from app.broker.capture.journal import REDACTED
from app.broker.capture.sanitize import (
    ACCOUNT_NUMBER_SENTINEL,
    ORDER_IDENTIFIER_SENTINEL,
    UUID_SENTINEL_BASE,
    sanitize_payload,
    sanitize_recording,
)

_ORDER = "61e69015-8549-4bfd-b9c3-01e75843f47d"
_OTHER_ORDER = "7a1f3c92-0000-4a8e-9d51-5b2f6c9e1a10"
_ACCOUNT = "PA3ABCDEFGH1"


def test_each_uuid_becomes_its_own_sentinel_and_the_same_one_wherever_it_recurs() -> None:
    sanitized = sanitize_payload(
        [{"id": _ORDER, "parent": _OTHER_ORDER}, {"order_id": _ORDER.upper()}],
    )

    assert sanitized == [
        {"id": UUID_SENTINEL_BASE.format(1), "parent": UUID_SENTINEL_BASE.format(2)},
        {"order_id": UUID_SENTINEL_BASE.format(1)},
    ]


def test_an_account_number_and_every_linkable_order_identifier_are_replaced_and_numbers_are_not() -> None:
    sanitized = sanitize_payload(
        {
            "account_number": _ACCOUNT,
            "client_order_id": "alpaca/bot-a/entry-17",
            "legs": [{"order_ref": "alpaca/bot-a/entry-17~1", "symbol": "SPY261120C00450000", "qty": "2"}],
            "filled_avg_price": "1.94",
            "filled_at": "2026-10-14T14:31:02.123456Z",
        }
    )

    assert sanitized["account_number"] == ACCOUNT_NUMBER_SENTINEL
    assert sanitized["client_order_id"] == ORDER_IDENTIFIER_SENTINEL
    assert sanitized["legs"] == [
        {"order_ref": ORDER_IDENTIFIER_SENTINEL, "symbol": "SPY261120C00450000", "qty": "2"}
    ]
    assert (sanitized["filled_avg_price"], sanitized["filled_at"]) == ("1.94", "2026-10-14T14:31:02.123456Z")


def test_a_recording_keeps_its_times_and_status_and_loses_the_account_and_the_keys() -> None:
    line = {
        "broker": "alpaca",
        "endpoint": "orders",
        "method": "POST",
        "params": {"order_id": _ORDER, "api_key": "PKTHEKEY"},
        "status": 200,
        "captured_at_ms": 1_760_450_000_000,
        "began_at_ms": 1_760_449_999_700,
        "ended_at_ms": 1_760_449_999_950,
        "raw_body": json.dumps({"id": _ORDER, "account_number": _ACCOUNT, "status": "accepted"}),
        "request_body": json.dumps(
            {"symbol": "SPY261120C00450000", "client_order_id": "alpaca/bot-a/entry-17", "secret_key": "shh"}
        ),
    }

    recording = sanitize_recording(line, uuid_map={}, account_ids=(_ACCOUNT,))

    assert recording["params"] == {"order_id": UUID_SENTINEL_BASE.format(1), "api_key": REDACTED}
    assert json.loads(recording["raw_body"]) == {
        "id": UUID_SENTINEL_BASE.format(1),
        "account_number": ACCOUNT_NUMBER_SENTINEL,
        "status": "accepted",
    }
    assert json.loads(recording["request_body"]) == {
        "symbol": "SPY261120C00450000",
        "client_order_id": ORDER_IDENTIFIER_SENTINEL,
        "secret_key": REDACTED,
    }
    for field in ("broker", "endpoint", "method", "status", "captured_at_ms", "began_at_ms", "ended_at_ms"):
        assert recording[field] == line[field]
    assert _ACCOUNT not in json.dumps(recording)
    assert "PKTHEKEY" not in json.dumps(recording)
    # The line it came from is untouched.
    assert line["params"]["api_key"] == "PKTHEKEY"


def test_an_account_id_that_is_not_shaped_like_a_paper_number_is_replaced_when_it_is_named() -> None:
    line = {"raw_body": json.dumps({"owner": "318420190", "limit": "3184201900", "note": "acct 318420190."})}

    recording = sanitize_recording(line, uuid_map={}, account_ids=("318420190",))

    body = json.loads(recording["raw_body"])
    # Whole tokens only: a longer number that merely contains it is another number.
    assert body == {"owner": ACCOUNT_NUMBER_SENTINEL, "limit": "3184201900", "note": f"acct {ACCOUNT_NUMBER_SENTINEL}."}


def test_an_authorization_acknowledgement_is_replaced_by_a_structural_placeholder() -> None:
    frame = {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate", "key_id": "PK1"}}

    recording = sanitize_recording({"raw_body": json.dumps(frame)}, uuid_map={})

    assert json.loads(recording["raw_body"]) == {
        "stream": "authorization",
        "data": {"status": "authorized", "action": "authenticate"},
    }


def test_a_body_that_is_not_json_is_scrubbed_as_text() -> None:
    recording = sanitize_recording({"raw_body": f"order {_ORDER} for {_ACCOUNT} not found"}, uuid_map={})

    assert recording["raw_body"] == f"order {UUID_SENTINEL_BASE.format(1)} for {ACCOUNT_NUMBER_SENTINEL} not found"


def test_a_body_that_cannot_be_read_as_text_is_refused_not_passed() -> None:
    with pytest.raises(ValueError, match="cannot be inspected"):
        sanitize_recording({"raw_body": "/+4=", "body_encoding": "base64"}, uuid_map={})


def test_one_map_across_lines_gives_one_order_one_identity() -> None:
    uuid_map: dict[str, str] = {}

    first = sanitize_recording({"raw_body": json.dumps({"id": _ORDER})}, uuid_map=uuid_map)
    second = sanitize_recording({"raw_body": json.dumps({"order_id": _ORDER, "id": _OTHER_ORDER})}, uuid_map=uuid_map)

    assert json.loads(first["raw_body"]) == {"id": UUID_SENTINEL_BASE.format(1)}
    assert json.loads(second["raw_body"]) == {
        "order_id": UUID_SENTINEL_BASE.format(1),
        "id": UUID_SENTINEL_BASE.format(2),
    }

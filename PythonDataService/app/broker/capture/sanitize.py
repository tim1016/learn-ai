"""The one way a recording of an Alpaca account is sanitised before it is committed as a fixture (#3102).

Recordings come from a live Paper account, so before one is committed it must
carry no account number, no key and nothing that links back to an order of the
owner's. These are the rules ``scripts/hitl_alpaca_capture.py`` has always
applied to what it captured, moved here so the qualification kit's recordings
(``scripts/option_qualification_bundle.py``) are sanitised by the same code and
not by a copy of it:

- UUIDs become distinct sentinel UUIDs, the same input always the same
  sentinel within one ``uuid_map`` (hand one map to every line of a set, so
  one order keeps one identity across the set);
- an account number becomes ``PA0SANITIZED00001``, and so does any account id
  the caller names, wherever it stands alone as a token;
- ``client_order_id`` and ``order_ref`` become one fixed, non-linkable token;
- a value under a secret-like key is redacted, with the journal's own rule;
- an authorization acknowledgement is replaced by a structural placeholder,
  never committed: it can echo key material;
- every number, timestamp, status string and symbol is kept verbatim: the
  adapter maps those, and sanitising must not disturb them.

A body this cannot read as JSON is refused, not passed (``sanitize_recording``
raises): a line that cannot be inspected for its keys is not one that has been
made safe, and opaque text with its UUIDs replaced is not sanitised. The caller
leaves such a line out and names it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.broker.capture.journal import redact_value

# Structurally valid UUIDs, so the adapter can parse them, and obviously not real.
UUID_SENTINEL_BASE = "00000000-0000-0000-0000-{:012d}"
ACCOUNT_NUMBER_SENTINEL = "PA0SANITIZED00001"
ORDER_IDENTIFIER_SENTINEL = "manual/hitl-gate/v1:SANITIZED0000000000001"
ORDER_IDENTIFIER_KEYS = frozenset({"client_order_id", "order_ref"})
# What an authorization acknowledgement is written as, in place of what Alpaca sent.
AUTH_ACK_PLACEHOLDER: Mapping[str, Any] = {
    "stream": "authorization",
    "data": {"status": "authorized", "action": "authenticate"},
}

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ACCOUNT_NUMBER_RE = re.compile(r"\bPA[0-9A-Z]{8,15}\b")
# The text fields of a journal line that hold a body.
_BODY_FIELDS = ("raw_body", "request_body")


def sanitize_payload(
    raw: Any, *, uuid_map: dict[str, str] | None = None, account_ids: Sequence[str] = ()
) -> Any:
    """Recursively scrub UUIDs and account numbers from a parsed JSON structure.

    UUIDs are replaced with deterministic sentinel values so tests that assert
    on structural field presence remain valid. The same input UUID always maps
    to the same sentinel within one ``uuid_map`` (stable within a fixture file).
    """
    if uuid_map is None:
        uuid_map = {}

    if isinstance(raw, dict):
        return {
            key: (
                sanitize_order_identifier(value)
                if key in ORDER_IDENTIFIER_KEYS and isinstance(value, str)
                else sanitize_payload(value, uuid_map=uuid_map, account_ids=account_ids)
            )
            for key, value in raw.items()
        }
    if isinstance(raw, list):
        return [sanitize_payload(item, uuid_map=uuid_map, account_ids=account_ids) for item in raw]
    if isinstance(raw, str):
        return sanitize_str(raw, uuid_map, account_ids)
    return raw


def sanitize_str(value: str, uuid_map: dict[str, str], account_ids: Sequence[str] = ()) -> str:
    # Replace account numbers before UUIDs (non-overlapping). An account id the caller names is replaced
    # whole only: inside a longer token it is not that account.
    for account_id in account_ids:
        if account_id:
            value = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(account_id)}(?![A-Za-z0-9])", ACCOUNT_NUMBER_SENTINEL, value
            )
    value = _ACCOUNT_NUMBER_RE.sub(ACCOUNT_NUMBER_SENTINEL, value)

    def _replace_uuid(m: re.Match[str]) -> str:
        orig = m.group(0).lower()
        if orig not in uuid_map:
            idx = len(uuid_map) + 1
            uuid_map[orig] = UUID_SENTINEL_BASE.format(idx)
        return uuid_map[orig]

    return _UUID_RE.sub(_replace_uuid, value)


def sanitize_order_identifier(value: str) -> str:
    """Replace a linkable operator order identifier with a stable fixture token."""
    return ORDER_IDENTIFIER_SENTINEL if value else value


def sanitize_recording(
    line: Mapping[str, Any], *, uuid_map: dict[str, str], account_ids: Sequence[str] = ()
) -> dict[str, Any]:
    """One journal line as a recording fit to commit; the line it came from is untouched.

    The line keeps its shape, its times and its status. Its ``params`` and
    its bodies are scrubbed by the rules above. Raises ``ValueError``, and
    returns nothing of the line, for one whose body cannot be made safe: not
    UTF-8 text, not JSON, or a bare JSON string. Opaque text is never passed
    with only its UUIDs and account ids replaced.
    """
    if line.get("body_encoding"):
        raise ValueError("a body that is not UTF-8 text cannot be inspected, so it is not sanitised")
    recording = dict(line)
    if "params" in recording:
        recording["params"] = sanitize_payload(
            redact_value(recording["params"]), uuid_map=uuid_map, account_ids=account_ids
        )
    for field in _BODY_FIELDS:
        if field in recording:
            recording[field] = _sanitize_body(recording[field], uuid_map, account_ids)
    return recording


def _sanitize_body(text: str, uuid_map: dict[str, str], account_ids: Sequence[str]) -> str:
    if not text.strip():
        # An empty body (a 204's) holds nothing to leak.
        return text
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        # Opaque text: its keys cannot be told, so a secret in it cannot be found. Replacing the account ids and
        # UUIDs in it would pass it off as sanitised while ``api_key=...`` or a client order id went through.
        raise ValueError("a body that is not JSON cannot be sanitised: a secret in its text cannot be told") from exc
    if isinstance(parsed, str):
        raise ValueError("a body that is a bare JSON string cannot be sanitised: a secret in its text cannot be told")
    if isinstance(parsed, dict) and parsed.get("stream") == "authorization":
        parsed = AUTH_ACK_PLACEHOLDER
    scrubbed = sanitize_payload(redact_value(parsed), uuid_map=uuid_map, account_ids=account_ids)
    return json.dumps(scrubbed, ensure_ascii=False)


__all__ = [
    "ACCOUNT_NUMBER_SENTINEL",
    "AUTH_ACK_PLACEHOLDER",
    "ORDER_IDENTIFIER_KEYS",
    "ORDER_IDENTIFIER_SENTINEL",
    "UUID_SENTINEL_BASE",
    "sanitize_order_identifier",
    "sanitize_payload",
    "sanitize_recording",
    "sanitize_str",
]

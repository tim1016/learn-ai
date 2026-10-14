"""Hash-chain row format — byte-level pin in ADR 0035's binding annex §7."""

from __future__ import annotations

import hashlib
import sqlite3

from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.hashchain import (
    GENESIS,
    PAYLOAD_COLUMNS,
    canonical_payload,
    canonicalize,
    compute_row_hash,
    verify_chain,
)
from app.broker.alpaca.clerk.sqlite.writes import _NON_KEY_TRANSITION_COLUMNS

_ROW = {
    "authority_generation": 1,
    "strategy_instance_id": "spy-bot",
    "run_id": None,
    "command_id": None,
    "effect_operation_id": None,
    "order_ref": None,
    "broker_order_id": None,
    "transition_kind": "STRATEGY_INSTANCE_REGISTERED",
    "custody_owner": "ACCOUNT_CLERK",
    "execution_authority": "ACCOUNT_CLERK",
    "operation_state": "succeeded",
    "broker_state": None,
    "proof_reference": None,
    "source_event_at_ms": None,
    "clerk_observed_at_ms": 1000,
    "recorded_at_ms": 1000,
    "summary_code": "STRATEGY_INSTANCE_REGISTERED",
    "facts_schema_version": 1,
    "facts_json": '{"symbol":"SPY"}',
}


def test_canonicalize_is_sorted_key_no_whitespace() -> None:
    assert canonicalize({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_payload_includes_every_column_even_when_none() -> None:
    payload = canonical_payload(_ROW)
    assert '"run_id":null' in payload
    assert '"broker_state":null' in payload


def test_compute_row_hash_is_string_not_byte_concatenation() -> None:
    """A real prev_hash is a 64-char hex *string*; the annex's §7 pins concatenating
    it as text, not decoding it to its 32 raw bytes first. These genuinely differ
    (different byte lengths, not just different bytes) for any real hash —
    unlike GENESIS, which isn't valid hex and so can't discriminate this.
    """
    row2 = dict(_ROW, strategy_instance_id="qqq-bot")
    payload1 = canonical_payload(_ROW)
    prev_hash = hashlib.sha256((GENESIS + payload1).encode("utf-8")).hexdigest()
    payload2 = canonical_payload(row2)

    string_concat = hashlib.sha256((prev_hash + payload2).encode("utf-8")).hexdigest()
    byte_concat = hashlib.sha256(bytes.fromhex(prev_hash) + payload2.encode("utf-8")).hexdigest()
    assert string_concat != byte_concat  # the two interpretations must diverge
    assert compute_row_hash(prev_hash, payload2) == string_concat


def test_verify_chain_accepts_a_valid_two_row_chain() -> None:
    row1 = dict(_ROW, sequence=1, prev_hash=GENESIS)
    payload1 = canonical_payload(row1)
    row1["row_hash"] = compute_row_hash(GENESIS, payload1)

    row2 = dict(_ROW, sequence=2, strategy_instance_id="qqq-bot", prev_hash=row1["row_hash"])
    payload2 = canonical_payload(row2)
    row2["row_hash"] = compute_row_hash(row1["row_hash"], payload2)

    assert verify_chain([row1, row2]) is None


def test_verify_chain_detects_a_tampered_row() -> None:
    row1 = dict(_ROW, sequence=1, prev_hash=GENESIS)
    row1["row_hash"] = compute_row_hash(GENESIS, canonical_payload(row1))
    row1["operation_state"] = "accepted"  # tamper after hashing

    assert verify_chain([row1]) == 1


def test_every_stored_column_but_the_chain_keys_is_hashed() -> None:
    """A column added to the table and the write list but not to ``PAYLOAD_COLUMNS`` would be stored un-hashed."""
    conn = sqlite3.connect(":memory:")
    try:
        schema.apply_schema(conn)
        stored = tuple(row[1] for row in conn.execute("PRAGMA table_info(custody_transitions)"))
    finally:
        conn.close()

    assert tuple(c for c in stored if c not in ("sequence", "prev_hash", "row_hash")) == PAYLOAD_COLUMNS
    assert PAYLOAD_COLUMNS == _NON_KEY_TRANSITION_COLUMNS


def test_a_row_with_non_ascii_text_hashes_to_the_digest_it_always_did() -> None:
    """The literal digest pins ``ensure_ascii=True``: the same row hashed as UTF-8 text would differ.

    The literals were computed once with this module as it stands; they are the format, not a derivation of it.
    """
    row = dict(
        _ROW,
        authority_generation=3,
        run_id="run-1",
        transition_kind="OPERATOR_NOTE",
        summary_code="OPERATOR_NOTE",
        clerk_observed_at_ms=1788883200000,
        recorded_at_ms=1788883200001,
        facts_json='{"note":"caf\u00e9 \u2713"}',
    )

    payload = canonical_payload(row)

    assert payload == (
        r'{"authority_generation":3,"broker_order_id":null,"broker_state":null,"clerk_observed_at_ms":1788883200000,'
        r'"command_id":null,"custody_owner":"ACCOUNT_CLERK","effect_operation_id":null,'
        r'"execution_authority":"ACCOUNT_CLERK","facts_json":"{\"note\":\"caf\u00e9 \u2713\"}",'
        r'"facts_schema_version":1,"operation_state":"succeeded","order_ref":null,"proof_reference":null,'
        r'"recorded_at_ms":1788883200001,"run_id":"run-1","source_event_at_ms":null,'
        r'"strategy_instance_id":"spy-bot","summary_code":"OPERATOR_NOTE","transition_kind":"OPERATOR_NOTE"}'
    )
    assert compute_row_hash(GENESIS, payload) == "16fb0ce616a610c6958a8e6a7a10e345475b8293af4eff7bdfeb88a027c59f4d"

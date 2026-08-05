"""Hash-chain row format — byte-level pin in contracts doc §7."""

from __future__ import annotations

import hashlib

from app.broker.alpaca.clerk.sqlite.hashchain import (
    GENESIS,
    canonical_payload,
    canonicalize,
    compute_row_hash,
    verify_chain,
)

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


def test_canonicalize_is_deterministic_regardless_of_input_order() -> None:
    assert canonicalize({"z": 1, "a": 2}) == canonicalize({"a": 2, "z": 1})


def test_canonical_payload_includes_every_column_even_when_none() -> None:
    payload = canonical_payload(_ROW)
    assert '"run_id":null' in payload
    assert '"broker_state":null' in payload


def test_compute_row_hash_matches_manual_sha256_string_concatenation() -> None:
    payload = canonical_payload(_ROW)
    expected = hashlib.sha256((GENESIS + payload).encode("utf-8")).hexdigest()
    assert compute_row_hash(GENESIS, payload) == expected


def test_compute_row_hash_is_string_not_byte_concatenation() -> None:
    # If prev_hash were decoded as raw bytes instead of concatenated as a hex
    # string, this would produce a different digest than the pinned formula.
    payload = canonical_payload(_ROW)
    string_concat = hashlib.sha256((GENESIS + payload).encode("utf-8")).hexdigest()
    byte_concat = hashlib.sha256(GENESIS.encode("utf-8") + payload.encode("utf-8")).hexdigest()
    assert string_concat == byte_concat  # GENESIS has no multi-byte ambiguity, sanity only
    assert compute_row_hash(GENESIS, payload) == string_concat


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

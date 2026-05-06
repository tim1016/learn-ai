"""Properties of canonical-JSON hashing used by the run ledger."""

from __future__ import annotations

from app.research.runs.hashing import (
    canonical_json,
    hash_payload,
    make_data_snapshot_id,
)


def test_canonical_json_key_order_independent():
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "y": 2, "x": 1}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_no_whitespace():
    s = canonical_json({"a": 1, "b": [2, 3]})
    assert " " not in s
    assert "\n" not in s
    assert s == '{"a":1,"b":[2,3]}'


def test_canonical_json_nested_keys_sorted():
    payload = {"outer": {"z": 1, "a": 2}, "alpha": [{"q": 1, "p": 0}]}
    s = canonical_json(payload)
    assert s == '{"alpha":[{"p":0,"q":1}],"outer":{"a":2,"z":1}}'


def test_canonical_json_preserves_unicode():
    payload = {"name": "résumé"}
    s = canonical_json(payload)
    assert "résumé" in s


def test_hash_payload_is_64_hex_chars():
    h = hash_payload({"a": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_payload_stable_across_calls():
    payload = {"name": "spec", "indicators": [{"id": "ema", "period": 10}]}
    assert hash_payload(payload) == hash_payload(payload)


def test_hash_payload_stable_across_key_order():
    a = {"id": "ema", "period": 10, "source": "close"}
    b = {"source": "close", "period": 10, "id": "ema"}
    assert hash_payload(a) == hash_payload(b)


def test_hash_payload_changes_on_value_change():
    base = {"period": 10}
    mutated = {"period": 11}
    assert hash_payload(base) != hash_payload(mutated)


def test_hash_payload_changes_on_key_change():
    a = {"period": 10}
    b = {"window": 10}
    assert hash_payload(a) != hash_payload(b)


def test_hash_payload_distinguishes_int_and_str():
    assert hash_payload({"x": 10}) != hash_payload({"x": "10"})


def test_make_data_snapshot_id_format():
    out = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc123",
    )
    assert out == "SPY|15|1700000000000|1710000000000|abc123"


def test_make_data_snapshot_id_changes_with_each_field():
    base = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_symbol = make_data_snapshot_id(
        symbol="QQQ",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_resolution = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=30,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_start = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_001,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_end = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_001,
        data_root_revision="abc",
    )
    diff_revision = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="def",
    )
    assert len({base, diff_symbol, diff_resolution, diff_start, diff_end, diff_revision}) == 6

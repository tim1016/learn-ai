"""Determinism + collision tests for app.data_lake.data_contract."""

from __future__ import annotations

import json

import pytest

from app.data_lake.data_contract import data_contract_hash


def _hash(provider: str, provider_params: str) -> str:
    # ``provider_params`` is JSON text so each case builds its own dict, in the written key order.
    return data_contract_hash(
        provider=provider,
        provider_params=json.loads(provider_params),
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )


@pytest.mark.parametrize(
    ("provider", "params_a", "params_b"),
    [
        (
            "polygon",
            '{"adjusted": false, "timespan": "minute", "multiplier": 1}',
            '{"multiplier": 1, "timespan": "minute", "adjusted": false}',
        ),
        (
            "learn_ai_derived",
            '{"source": {"trade_artifact_id": 42, "sha256": "abc"}}',
            '{"source": {"sha256": "abc", "trade_artifact_id": 42}}',
        ),
    ],
    ids=["identical_inputs_produce_identical_hashes", "nested_provider_params_canonicalized"],
)
def test_provider_params_key_order_does_not_change_the_hash(provider: str, params_a: str, params_b: str) -> None:
    a = _hash(provider, params_a)
    b = _hash(provider, params_b)
    assert a == b, "key order in provider_params (nested included) must not matter"
    assert len(a) == 64, "the hash is 64 hex characters"


@pytest.mark.parametrize(
    ("provider_a", "params_a", "provider_b", "params_b"),
    [
        ("polygon", "{}", "learn_ai_derived", "{}"),
        ("polygon", '{"adjusted": false}', "polygon", '{"adjusted": true}'),
    ],
    ids=["different_provider_produces_different_hashes", "different_provider_params_produce_different_hashes"],
)
def test_a_different_provider_or_provider_params_changes_the_hash(
    provider_a: str, params_a: str, provider_b: str, params_b: str
) -> None:
    assert _hash(provider_a, params_a) != _hash(provider_b, params_b), "the hashes must differ"

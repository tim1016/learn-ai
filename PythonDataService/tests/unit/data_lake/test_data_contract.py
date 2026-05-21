"""Determinism + collision tests for app.data_lake.data_contract."""

from __future__ import annotations

from app.data_lake.data_contract import data_contract_hash


def test_identical_inputs_produce_identical_hashes():
    a = data_contract_hash(
        provider="polygon",
        provider_params={"adjusted": False, "timespan": "minute", "multiplier": 1},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    b = data_contract_hash(
        provider="polygon",
        provider_params={"multiplier": 1, "timespan": "minute", "adjusted": False},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    assert a == b  # key order in provider_params must not matter
    assert len(a) == 64


def test_different_provider_produces_different_hashes():
    a = data_contract_hash(
        provider="polygon",
        provider_params={},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    b = data_contract_hash(
        provider="learn_ai_derived",
        provider_params={},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    assert a != b


def test_different_provider_params_produce_different_hashes():
    a = data_contract_hash(
        provider="polygon",
        provider_params={"adjusted": False},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    b = data_contract_hash(
        provider="polygon",
        provider_params={"adjusted": True},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    assert a != b


def test_nested_provider_params_canonicalized():
    a = data_contract_hash(
        provider="learn_ai_derived",
        provider_params={"source": {"trade_artifact_id": 42, "sha256": "abc"}},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    b = data_contract_hash(
        provider="learn_ai_derived",
        provider_params={"source": {"sha256": "abc", "trade_artifact_id": 42}},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    assert a == b  # nested key order must not matter

"""Manifest schema round-trip + sha256 helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.lean_sidecar.parity_matrix.manifest import (
    BrokerSpec,
    CellManifest,
    DataSpec,
    LeanRuntimeSpec,
    PinnedArtifactHashes,
    StateCsvSchema,
    StrategySpec,
    WindowSpec,
    sha256_of_file,
    sha256_of_text,
)


def test_sha256_of_text_stable() -> None:
    h = sha256_of_text("hello")
    assert h == hashlib.sha256(b"hello").hexdigest()
    assert len(h) == 64


def test_sha256_of_file(tmp_path: Path) -> None:
    p = tmp_path / "x.txt"
    p.write_text("hello", encoding="utf-8")
    assert sha256_of_file(p) == hashlib.sha256(b"hello").hexdigest()


def test_manifest_round_trip() -> None:
    m = CellManifest(
        schema_version=1,
        cell_id="SPY_W6mo_2025-11-03_to_2026-04-30",
        ticker="SPY",
        window=WindowSpec(
            label="W6mo",
            start_date="2025-11-03",
            end_date="2026-04-30",
            session="regular",
            trading_days_expected=125,
        ),
        strategy=StrategySpec(
            trusted_sample="ema_crossover",
            trusted_sample_source_sha256="a" * 64,
            parameters_constants={
                "FAST_PERIOD": 5,
                "SLOW_PERIOD": 10,
                "RSI_PERIOD": 14,
                "EXIT_BARS": 5,
                "GAP_MIN": 0.20,
                "RSI_LO": 50,
                "RSI_HI": 70,
            },
            runtime_parameters={
                "bar_minutes": 15,
                "adjustment": "raw",
                "starting_cash": 100000,
            },
        ),
        data=DataSpec(
            lean_data_capture_ref="_lean_data_capture/SPY",
            data_contract_hash="b" * 64,
        ),
        broker=BrokerSpec(
            brokerage_model="InteractiveBrokersBrokerage",
            account_type="Margin",
            fill_model="ImmediateFillModel",
            fee_model="IbkrEquityCommissionModel",
        ),
        lean_runtime=LeanRuntimeSpec(
            container_image_digest="docker.io/quantconnect/lean@sha256:" + "c" * 64,
        ),
        artifacts=PinnedArtifactHashes(
            orders_sha256="d" * 64,
            state_sha256="e" * 64,
            observations_sha256="f" * 64,
            reconciliation_sha256="0" * 64,
        ),
        state_csv_schema=StateCsvSchema(
            columns=["ts_ms_utc", "close", "ema_fast", "ema_slow", "rsi", "cross_state", "signal"],
            column_types={
                "ts_ms_utc": "int64",
                "close": "decimal_string",
                "ema_fast": "decimal_string",
                "ema_slow": "decimal_string",
                "rsi": "decimal_string",
                "cross_state": "string_enum:above|below|equal",
                "signal": "string_enum:HOLD|ENTER|EXIT",
            },
        ),
        timezone="America/New_York",
        timestamp_convention="int64_ms_utc",
        fixture_git_commit="1" * 40,
        python_data_service_commit="1" * 40,
        generator_script_sha256="2" * 64,
        captured_by="Tester",
        captured_at_ms_utc=1779849600000,
    )
    serialized = m.model_dump_json()
    reloaded = CellManifest.model_validate_json(serialized)
    assert reloaded == m


def test_manifest_rejects_unknown_session() -> None:
    with pytest.raises(ValidationError):
        WindowSpec(
            label="W6mo",
            start_date="2025-11-03",
            end_date="2026-04-30",
            session="weird",
            trading_days_expected=125,
        )


def test_state_csv_schema_columns_match_keys() -> None:
    s = StateCsvSchema(
        columns=["a", "b"],
        column_types={"a": "int64", "b": "int64"},
    )
    assert set(s.columns) == set(s.column_types.keys())


def test_state_csv_schema_rejects_mismatched_keys() -> None:
    with pytest.raises(ValidationError):
        StateCsvSchema(
            columns=["a", "b"],
            column_types={"a": "int64"},  # missing "b"
        )


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        (
            WindowSpec,
            dict(
                label="W6mo",
                start_date="2025-11-03",
                end_date="2026-04-30",
                session="regular",
                trading_days_expected=125,
                unknown_field="x",
            ),
        ),
        (
            BrokerSpec,
            dict(
                brokerage_model="InteractiveBrokersBrokerage",
                account_type="Margin",
                fill_model="ImmediateFillModel",
                fee_model="IbkrEquityCommissionModel",
                unknown_field="x",
            ),
        ),
        (
            DataSpec,
            dict(
                lean_data_capture_ref="_lean_data_capture/SPY",
                data_contract_hash="b" * 64,
                unknown_field="x",
            ),
        ),
    ],
)
def test_extra_fields_rejected(cls: type, kwargs: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValidationError):
        cls(**kwargs)

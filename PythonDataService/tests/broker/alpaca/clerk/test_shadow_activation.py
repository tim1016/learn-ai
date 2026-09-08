"""The shadow world's append-only activation fence (ADR 0059 D2)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_activation import (
    SHADOW_ACTIVATION_FILENAME,
    ShadowActivationConflict,
    ShadowActivationInvalid,
    ShadowActivationRecord,
    ShadowActivationStore,
)
from app.broker.alpaca.clerk.synthetic_activation import (
    IsolatedActivationInvalid,
    SyntheticActivationRecord,
)


def _record(generation: int = 1) -> ShadowActivationRecord:
    return ShadowActivationRecord.create(
        account_id="shadow:9LIVE0001",
        authority_generation=generation,
        db_identity_token="sqlite-identity",
        activated_at_ms=1_700_000_000_000,
    )


def test_record_requires_the_shadow_namespace() -> None:
    with pytest.raises(ValueError, match="shadow: account identity"):
        ShadowActivationRecord.create(
            account_id="sim:ema-1",
            authority_generation=1,
            db_identity_token="t",
            activated_at_ms=1,
        )
    with pytest.raises(ShadowActivationInvalid, match="invalid values"):
        ShadowActivationRecord.from_payload({**asdict(_record()), "account_id": "9LIVE0001"})


def test_record_digest_verifies_and_rejects_tampering() -> None:
    record = _record()
    assert ShadowActivationRecord.from_payload(asdict(record)) == record
    with pytest.raises(ShadowActivationInvalid, match="digest does not verify"):
        ShadowActivationRecord.from_payload({**asdict(record), "activated_at_ms": 2})
    assert isinstance(ShadowActivationInvalid("x"), IsolatedActivationInvalid)


def test_store_lives_under_accounts_shadow_and_is_monotonic(tmp_path: Path) -> None:
    store = ShadowActivationStore(tmp_path)
    assert store.path == tmp_path / "accounts" / "shadow" / SHADOW_ACTIVATION_FILENAME
    assert store.latest("shadow:9LIVE0001") is None

    store.append(_record(1))
    store.append(_record(2))
    assert store.latest("shadow:9LIVE0001") == _record(2)
    with pytest.raises(ShadowActivationConflict, match="generation must increase"):
        store.append(_record(2))


def test_store_refuses_synthetic_rows_and_synthetic_store_refuses_shadow_rows(tmp_path: Path) -> None:
    store = ShadowActivationStore(tmp_path)
    with pytest.raises(ValueError, match="shadow: account identity"):
        store.latest("sim:ema-1")
    synthetic = SyntheticActivationRecord.create(
        account_id="sim:ema-1", authority_generation=1, db_identity_token="t", activated_at_ms=1
    )
    with pytest.raises(ShadowActivationInvalid):
        store.append(synthetic)  # type: ignore[arg-type]

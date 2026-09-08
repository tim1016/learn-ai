"""The sealed per-instance proof that the shadow gate passed (ADR 0059 D2)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_receipt import (
    SHADOW_RECEIPTS_FILENAME,
    ShadowReceipt,
    ShadowReceiptInvalid,
    ShadowReceiptSession,
    ShadowReceiptStore,
)

SIGNAL = "a" * 64


def _receipt(*, sessions: int = 2, signal: str = SIGNAL, written_at_ms: int = 1_700_000_000_000) -> ShadowReceipt:
    return ShadowReceipt.create(
        live_account_id="9LIVE0001",
        strategy_instance_id="ema-shadow-1",
        configured_signal_hash=signal,
        twin_account_id="PA-TEST",
        twin_strategy_instance_id="ema-paper-1",
        required_sessions=2,
        sessions=tuple(
            ShadowReceiptSession(session_open_ms=1_000 + index, shadow_run_id="run-1", reconciliation_sha256="b" * 64)
            for index in range(sessions)
        ),
        written_at_ms=written_at_ms,
    )


def test_receipt_is_sealed_and_verifies() -> None:
    receipt = _receipt()
    assert len(receipt.receipt_sha256) == 64
    assert ShadowReceipt.from_payload(asdict(receipt)) == receipt
    with pytest.raises(ShadowReceiptInvalid, match="digest does not verify"):
        ShadowReceipt.from_payload({**asdict(receipt), "required_sessions": 1})
    with pytest.raises(ShadowReceiptInvalid, match="shadow: account"):
        ShadowReceipt.from_payload({**asdict(receipt), "live_account_id": "shadow:9LIVE0001"})


def test_store_appends_and_answers_current_by_signal_and_count(tmp_path: Path) -> None:
    store = ShadowReceiptStore(tmp_path)
    assert store.path == tmp_path / "accounts" / "shadow" / SHADOW_RECEIPTS_FILENAME
    assert store.latest("ema-shadow-1") is None and store.any_for_account("9LIVE0001") is False

    store.append(_receipt(sessions=1, written_at_ms=1))
    store.append(_receipt(sessions=2, written_at_ms=2))

    assert store.latest("ema-shadow-1") == _receipt(sessions=2, written_at_ms=2)
    assert store.current("ema-shadow-1", configured_signal_hash=SIGNAL, required_sessions=2) == _receipt(
        sessions=2, written_at_ms=2
    )
    assert store.current("ema-shadow-1", configured_signal_hash=SIGNAL, required_sessions=3) is None
    assert store.current("ema-shadow-1", configured_signal_hash="c" * 64, required_sessions=1) is None
    assert store.any_for_account("9LIVE0001") is True
    assert len(store.all_for("ema-shadow-1")) == 2


def test_store_refuses_a_tampered_row(tmp_path: Path) -> None:
    store = ShadowReceiptStore(tmp_path)
    store.append(_receipt())
    store.path.write_text(store.path.read_text().replace('"required_sessions":2', '"required_sessions":1'), encoding="utf-8")
    with pytest.raises(ShadowReceiptInvalid):
        store.latest("ema-shadow-1")

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
from app.utils.session_anchors import MAX_TIMESTAMP_MS

SIGNAL = "a" * 64


def _session(session_open_ms: int) -> ShadowReceiptSession:
    return ShadowReceiptSession(
        session_open_ms=session_open_ms, shadow_run_id="run-1", reconciliation_sha256="b" * 64
    )


def _receipt(
    *,
    sessions: int = 2,
    signal: str = SIGNAL,
    written_at_ms: int = 1_700_000_000_000,
    required_sessions: int | None = None,
    instance: str = "ema-shadow-1",
    account: str = "9LIVE0001",
) -> ShadowReceipt:
    return ShadowReceipt.create(
        live_account_id=account,
        strategy_instance_id=instance,
        configured_signal_hash=signal,
        twin_account_id="PA-TEST",
        twin_strategy_instance_id="ema-paper-1",
        required_sessions=sessions if required_sessions is None else required_sessions,
        sessions=tuple(_session(1_000 + index) for index in range(sessions)),
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

    # A second instance on a second account shares the ledger and answers
    # only for itself: one instance's sealed proof never arms another.
    store.append(_receipt(sessions=1, written_at_ms=3, instance="ema-shadow-2", account="9LIVE0002"))

    assert len(store.all_for("ema-shadow-1")) == 2
    assert store.latest("ema-shadow-1") == _receipt(sessions=2, written_at_ms=2)
    second = store.latest("ema-shadow-2")
    assert second is not None and second.strategy_instance_id == "ema-shadow-2"
    assert store.current("ema-shadow-2", configured_signal_hash=SIGNAL, required_sessions=2) is None
    assert store.any_for_account("9LIVE0002") is True
    assert store.any_for_account("9LIVE0003") is False


def test_store_refuses_a_tampered_row(tmp_path: Path) -> None:
    store = ShadowReceiptStore(tmp_path)
    store.append(_receipt())
    store.path.write_text(store.path.read_text().replace('"required_sessions":2', '"required_sessions":1'), encoding="utf-8")
    with pytest.raises(ShadowReceiptInvalid):
        store.latest("ema-shadow-1")


def test_store_refuses_a_type_confused_row_by_its_own_error(tmp_path: Path) -> None:
    """A hand-edited row whose integer became a string is invalid, not a ``TypeError``."""
    store = ShadowReceiptStore(tmp_path)
    store.append(_receipt(written_at_ms=1_700_000_000_000))
    store.path.write_text(
        store.path.read_text().replace('"written_at_ms":1700000000000', '"written_at_ms":"1700000000000"'),
        encoding="utf-8",
    )

    with pytest.raises(ShadowReceiptInvalid, match="invalid shape"):
        store.latest("ema-shadow-1")


def test_receipt_refuses_a_repeated_session() -> None:
    """N sessions must be N distinct trading days, not one day counted N times."""
    with pytest.raises(ShadowReceiptInvalid, match="repeats a session"):
        ShadowReceipt.create(
            live_account_id="9LIVE0001",
            strategy_instance_id="ema-shadow-1",
            configured_signal_hash=SIGNAL,
            twin_account_id="PA-TEST",
            twin_strategy_instance_id="ema-paper-1",
            required_sessions=2,
            sessions=(_session(1_000), _session(1_000)),
            written_at_ms=1_700_000_000_000,
        )


def test_receipt_bounds_its_timestamps_by_the_domain_ceiling() -> None:
    assert _receipt(written_at_ms=MAX_TIMESTAMP_MS).written_at_ms == MAX_TIMESTAMP_MS
    with pytest.raises(ShadowReceiptInvalid, match="invalid integer facts"):
        _receipt(written_at_ms=MAX_TIMESTAMP_MS + 1)
    with pytest.raises(ShadowReceiptInvalid, match="invalid session facts"):
        ShadowReceipt.create(
            live_account_id="9LIVE0001",
            strategy_instance_id="ema-shadow-1",
            configured_signal_hash=SIGNAL,
            twin_account_id="PA-TEST",
            twin_strategy_instance_id="ema-paper-1",
            required_sessions=1,
            sessions=(_session(MAX_TIMESTAMP_MS + 1),),
            written_at_ms=1_700_000_000_000,
        )


def test_receipt_refuses_to_require_more_sessions_than_it_lists() -> None:
    """The sealed count is self-describing: a receipt cannot claim a day it never listed."""
    with pytest.raises(ShadowReceiptInvalid, match="fewer sessions than it requires"):
        _receipt(sessions=1, required_sessions=2)

"""WAL+skip-log fold for the Sizing card per-trade audit (VCR-0003).

PR A scope: the helper itself, additively added to live_instances.py.
PR B will wire it into ``_sizing_audit_rows``; this PR does not.

The fold helper is the durability bridge: SIZING_RESOLVED events live in
``intent_events.jsonl`` (Phase 8 / ADR 0009 § 11), SIZING_SKIP rows live
in ``sizing_skip.jsonl``, and the Sizing card needs both as a unified
per-trade audit list. Until PR B lands, the helper is unused in
production — these tests are the only callers.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.live.intent_events import IntentEventType, IntentKind
from app.engine.live.intent_wal import IntentWal
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)
from app.routers.live_instances import _fold_wal_sizing_audit

NS = build_bot_order_namespace("VCR0003")


def _write_sizing_resolved(
    wal: IntentWal,
    *,
    symbol: str = "AAPL",
    policy_kind: str = "SetHoldings",
    policy_value: str = "1.0",
    intended_qty: int = 10,
    reference_price: str = "100.00",
    ts_ms: int = 1_780_000_000_000,
    sized_via: str = "policy_set_holdings",
) -> None:
    iid = mint_intent_id()
    wal.append(
        event_type=IntentEventType.SIZING_RESOLVED,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
        intent_kind=IntentKind.STRATEGY,
        order_id=12345,
        symbol=symbol,
        policy_kind=policy_kind,
        policy_value=policy_value,
        intended_qty=intended_qty,
        reference_price=reference_price,
        sizing_provenance_at_resolve_time="live_override",
        sized_via=sized_via,
        ts_ms=ts_ms,
    )


def _append_skip_line(
    skip_log: Path,
    *,
    symbol: str = "MSFT",
    policy_kind: str = "SetHoldings",
    policy_value: str = "1.0",
    target_qty: int = 5,
    current_qty: int = 5,
    reference_price: str = "200.00",
    reason: str = "target_equals_current",
    ts_ms: int = 1_780_000_001_000,
) -> None:
    """Mirrors the on-disk shape written by
    ``live_portfolio._append_sizing_skip_line``."""
    skip_log.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_type": "SIZING_SKIP",
        "ts_ms_utc": ts_ms,
        "symbol": symbol,
        "policy_kind": policy_kind,
        "policy_value": policy_value,
        "target_qty": target_qty,
        "current_qty": current_qty,
        "reference_price": reference_price,
        "reason": reason,
    }
    with skip_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def test_fold_empty_run_dir_returns_empty(tmp_path: Path) -> None:
    assert _fold_wal_sizing_audit(tmp_path) == []


def test_fold_only_sizing_resolved_in_wal(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    _write_sizing_resolved(wal)

    rows = _fold_wal_sizing_audit(tmp_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "AAPL"
    assert row["policy_kind"] == "SetHoldings"
    assert row["policy_value"] == "1.0"
    assert row["intended_qty"] == 10
    assert row["reference_price"] == "100.00"
    assert row["sized_via"] == "policy_set_holdings"
    assert row["ts_ms"] == 1_780_000_000_000
    assert row.get("skipped", False) is False


def test_fold_only_sizing_skip_log(tmp_path: Path) -> None:
    _append_skip_line(tmp_path / "sizing_skip.jsonl")

    rows = _fold_wal_sizing_audit(tmp_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "MSFT"
    assert row["policy_kind"] == "SetHoldings"
    assert row["policy_value"] == "1.0"
    assert row["intended_qty"] == 5
    assert row["reference_price"] == "200.00"
    assert row["skipped"] is True
    assert row["skip_reason"] == "target_equals_current"
    assert row["ts_ms"] == 1_780_000_001_000
    # ``sized_via`` follows the in-memory skip-row convention: the policy
    # path tags as ``policy_set_holdings`` (the sizer wrote the prior row
    # with that tag); we mirror that for fold consistency.
    assert row["sized_via"] == "policy_set_holdings_skip"


def test_fold_merges_both_newest_first(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    _write_sizing_resolved(wal, symbol="AAPL", ts_ms=1_780_000_000_000)
    _append_skip_line(
        tmp_path / "sizing_skip.jsonl",
        symbol="MSFT",
        ts_ms=1_780_000_001_000,
    )

    rows = _fold_wal_sizing_audit(tmp_path)

    assert [r["symbol"] for r in rows] == ["MSFT", "AAPL"]


def test_fold_ignores_non_sizing_resolved_wal_events(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    iid = mint_intent_id()
    wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
    )
    _write_sizing_resolved(wal, symbol="NVDA")

    rows = _fold_wal_sizing_audit(tmp_path)

    assert [r["symbol"] for r in rows] == ["NVDA"]


def test_fold_corrupt_wal_returns_empty(tmp_path: Path) -> None:
    """Fail-open mirrors ``_sizing_audit_rows``'s tolerance for a corrupt
    sidecar. The WAL is the source of truth for safety-critical reads, but
    the Sizing card is a UI surface and should degrade gracefully — not
    block render — when its evidence is unreadable."""
    wal_path = tmp_path / "intent_events.jsonl"
    wal_path.write_text("not-json\n", encoding="utf-8")

    assert _fold_wal_sizing_audit(tmp_path) == []


def test_fold_corrupt_skip_log_returns_empty(tmp_path: Path) -> None:
    skip_log = tmp_path / "sizing_skip.jsonl"
    skip_log.write_text("not-json\n", encoding="utf-8")

    assert _fold_wal_sizing_audit(tmp_path) == []


def test_fold_caps_at_50_rows(tmp_path: Path) -> None:
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    for i in range(60):
        _write_sizing_resolved(wal, ts_ms=1_780_000_000_000 + i)

    rows = _fold_wal_sizing_audit(tmp_path)

    assert len(rows) == 50
    # Newest first — the last-appended event surfaces at index 0.
    assert rows[0]["ts_ms"] == 1_780_000_000_000 + 59
    assert rows[-1]["ts_ms"] == 1_780_000_000_000 + 10


def test_fold_old_sizing_resolved_without_symbol_returns_empty_symbol(
    tmp_path: Path,
) -> None:
    """Backward-compatibility: WAL events written before VCR-0003 PR A's
    symbol additive will fold with ``symbol=''`` rather than raise."""
    wal_path = tmp_path / "intent_events.jsonl"
    wal_path.parent.mkdir(parents=True, exist_ok=True)
    iid = mint_intent_id()
    legacy_event = {
        "seq": 1,
        "event_type": "SIZING_RESOLVED",
        "intent_id": iid,
        "bot_order_namespace": NS,
        "order_ref": build_order_ref(NS, iid),
        "intent_kind": "STRATEGY",
        "reason": None,
        "order_id": 12345,
        "perm_id": None,
        "exec_id": None,
        "order_spec": None,
        "policy_kind": "SetHoldings",
        "policy_value": "1.0",
        "intended_qty": 10,
        "reference_price": "100.00",
        "sizing_provenance_at_resolve_time": "live_override",
        "sized_via": "policy_set_holdings",
        "ts_ms": 1_780_000_000_000,
    }
    wal_path.write_text(json.dumps(legacy_event) + "\n", encoding="utf-8")

    rows = _fold_wal_sizing_audit(tmp_path)

    assert len(rows) == 1
    assert rows[0]["symbol"] == ""

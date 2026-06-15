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

import pytest

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
    # VCR-0003 last-mile — the engine-minted provenance stamp is now
    # surfaced through the fold to the Sizing card's per-trade audit.
    assert row["sizing_provenance_at_resolve_time"] == "live_override"


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
    # VCR-0003 last-mile — sizing_skip.jsonl predates this field, so
    # skip rows surface ``None``. The frontend renders an "unknown"
    # badge for these rows. A future skip-log schema revision can
    # populate the field; until then it's None.
    assert row["sizing_provenance_at_resolve_time"] is None


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


# ---------------------------------------------------------------------------
# Regression tests from the PR #552 max-effort code review (fix-up commit).
# Each test pins a fail-open contract that a verifier flagged as broken.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_line",
    ["null", "42", "[]", '"string"', "true"],
)
def test_fold_non_dict_skip_log_line_does_not_crash(
    tmp_path: Path, bad_line: str
) -> None:
    """A valid-JSON non-dict skip-log line must not crash the fold via
    AttributeError on ``payload.get(...)``. Review finding #2."""
    skip_log = tmp_path / "sizing_skip.jsonl"
    skip_log.write_text(bad_line + "\n", encoding="utf-8")

    # The bad line is silently dropped (per-line fail-open); empty result.
    assert _fold_wal_sizing_audit(tmp_path) == []


def test_fold_non_numeric_skip_log_values_do_not_crash(tmp_path: Path) -> None:
    """``int(payload.get('target_qty') or 0)`` must not crash with
    TypeError when target_qty is a list/dict. Review finding #3."""
    skip_log = tmp_path / "sizing_skip.jsonl"
    bad_payloads = [
        {"ts_ms_utc": 1_780_000_000_000, "target_qty": [5], "symbol": "BAD1"},
        {"ts_ms_utc": [99], "target_qty": 0, "symbol": "BAD2"},
        {"ts_ms_utc": 1_780_000_000_000, "target_qty": {"x": 1}, "symbol": "BAD3"},
    ]
    skip_log.write_text(
        "\n".join(json.dumps(p) for p in bad_payloads) + "\n", encoding="utf-8"
    )

    # Each bad row is dropped per-line; no crash.
    assert _fold_wal_sizing_audit(tmp_path) == []


def test_fold_corrupt_mid_line_in_skip_log_preserves_trailing_valid_lines(
    tmp_path: Path,
) -> None:
    """A corrupt mid-file line in the skip log must drop only itself, not
    all trailing valid lines. Review finding #6 — the prior implementation
    wrapped the for-loop in try/except so a single bad line dropped the
    trailing prefix of valid rows."""
    skip_log = tmp_path / "sizing_skip.jsonl"
    lines = [
        json.dumps(
            {
                "event_type": "SIZING_SKIP",
                "ts_ms_utc": 1_780_000_000_001,
                "symbol": "BEFORE_A",
                "policy_kind": "SetHoldings",
                "policy_value": "1.0",
                "target_qty": 1,
                "current_qty": 1,
                "reference_price": "100.00",
                "reason": "target_equals_current",
            }
        ),
        json.dumps(
            {
                "event_type": "SIZING_SKIP",
                "ts_ms_utc": 1_780_000_000_002,
                "symbol": "BEFORE_B",
                "policy_kind": "SetHoldings",
                "policy_value": "1.0",
                "target_qty": 2,
                "current_qty": 2,
                "reference_price": "200.00",
                "reason": "target_equals_current",
            }
        ),
        "not-json-at-all",  # corrupt mid-file line
        json.dumps(
            {
                "event_type": "SIZING_SKIP",
                "ts_ms_utc": 1_780_000_000_003,
                "symbol": "AFTER_C",
                "policy_kind": "SetHoldings",
                "policy_value": "1.0",
                "target_qty": 3,
                "current_qty": 3,
                "reference_price": "300.00",
                "reason": "target_equals_current",
            }
        ),
    ]
    skip_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = _fold_wal_sizing_audit(tmp_path)

    symbols = [r["symbol"] for r in rows]
    assert "BEFORE_A" in symbols
    assert "BEFORE_B" in symbols
    assert "AFTER_C" in symbols, "trailing valid line must survive a mid-file corrupt line"
    assert len(symbols) == 3


def test_fold_wal_uses_seq_not_ts_ms_for_50_row_cap(tmp_path: Path) -> None:
    """ADR-0008 §3/§5: ``seq`` is the WAL's authoritative chronology, not
    ``ts_ms``. Under a wall-clock step-back, the latest-by-seq event must
    still surface in the top-50 even if its ``ts_ms`` is smaller than 50
    other events' ``ts_ms``. Review finding #1."""
    wal = IntentWal(tmp_path / "intent_events.jsonl")

    # 50 events at increasing ts_ms (the "normal" timeline).
    for i in range(50):
        _write_sizing_resolved(
            wal,
            symbol=f"NORM{i:02d}",
            ts_ms=1_780_000_000_000 + i,
        )
    # One more event with a SMALLER ts_ms — simulates NTP step-back
    # between fsyncs. seq=51 has ts_ms below ALL 50 prior events.
    _write_sizing_resolved(
        wal,
        symbol="STEPBACK",
        ts_ms=1_779_999_999_000,
    )

    rows = _fold_wal_sizing_audit(tmp_path)

    symbols = [r["symbol"] for r in rows]
    assert "STEPBACK" in symbols, (
        "the seq-most-recent SIZING_RESOLVED must not be dropped from the "
        "50-row cap by a wall-clock step-back — ts_ms must not be the cap "
        "boundary per ADR-0008 §3/§5"
    )

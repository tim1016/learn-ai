"""PR B wiring: ``_sizing_audit_rows`` prefers the WAL fold and falls
back to the sidecar projection for legacy runs (VCR-0003).

PR A's tests covered the helper in isolation. These tests cover the
endpoint glue: the WAL fold wins when a current run dir has evidence;
the sidecar projection is consulted only when the WAL fold is empty
(no current run dir, or a run dir with no SIZING_RESOLVED / SIZING_SKIP
evidence yet).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.engine.live.intent_events import IntentEventType, IntentKind
from app.engine.live.intent_wal import IntentWal
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)
from app.routers import live_instances

NS = build_bot_order_namespace("VCR0003WIRE")
SID = "sid-vcr0003-wiring"


def _stub_settings(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    stub = SimpleNamespace(
        live_runs_root=str(root / "live_runs"),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)


def _seed_run_dir(root: Path, sid: str, run_id: str = "run-001") -> Path:
    """Seed a run_ledger.json that ties run_id to sid so
    ``latest_run_dir_for_instance`` finds it."""
    run_dir = root / "live_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / "run_ledger.json"
    ledger_path.write_text(
        json.dumps({"strategy_instance_id": sid, "run_id": run_id}),
        encoding="utf-8",
    )
    return run_dir


def _write_sizing_resolved(
    wal: IntentWal,
    *,
    symbol: str = "AAPL",
    ts_ms: int = 1_780_000_000_000,
    reference_price: str | None = "100.00",
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
        policy_kind="SetHoldings",
        policy_value="1.0",
        intended_qty=10,
        reference_price=reference_price,
        sizing_provenance_at_resolve_time="live_override",
        sized_via="policy_set_holdings",
        ts_ms=ts_ms,
    )


def _write_sidecar(root: Path, sid: str, rows: list[dict]) -> None:
    """Write a real ``LiveStateEnvelope`` to the sidecar path. Schema is
    ``extra="forbid"`` so hand-rolled JSON is rejected — we use the model."""
    from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo

    sidecar_dir = root / "live_state" / sid
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    envelope = LiveStateEnvelope(
        strategy_instance_id=sid,
        run_id="run-001",
        bot_order_namespace=NS,
        ib_client_id=1,
        last_processed_bar_ms=1_780_000_000_000,
        last_artifact_flush_ms=1_780_000_000_000,
        sizing_resolutions=rows,
    )
    LiveStateSidecarRepo(sidecar_dir / "live_state.json").write(envelope)


def test_wal_fold_wins_over_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the run dir has SIZING_RESOLVED events AND the sidecar has
    its own projection, the WAL fold takes precedence — durability over
    in-memory state."""
    _stub_settings(monkeypatch, tmp_path)
    run_dir = _seed_run_dir(tmp_path, SID)
    wal = IntentWal(run_dir / "intent_events.jsonl")
    _write_sizing_resolved(wal, symbol="FROM_WAL")
    _write_sidecar(
        tmp_path,
        SID,
        rows=[
            {
                "ts_ms": 1_780_000_000_000,
                "symbol": "FROM_SIDECAR",
                "policy_kind": "SetHoldings",
                "policy_value": "1.0",
                "intended_qty": 99,
                "reference_price": "999.00",
                "sized_via": "policy_set_holdings",
            }
        ],
    )

    rows = live_instances._sizing_audit_rows(SID)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "FROM_WAL"


def test_wal_fold_preserves_null_reference_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_settings(monkeypatch, tmp_path)
    run_dir = _seed_run_dir(tmp_path, SID)
    wal = IntentWal(run_dir / "intent_events.jsonl")
    _write_sizing_resolved(wal, symbol="NVDA", reference_price=None)

    rows = live_instances._sizing_audit_rows(SID)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["reference_price"] is None


def test_falls_back_to_sidecar_when_wal_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run dir exists (so ``latest_run_dir_for_instance`` returns it)
    but it has no SIZING_RESOLVED / SIZING_SKIP evidence yet. The
    fallback fires and the sidecar projection wins."""
    _stub_settings(monkeypatch, tmp_path)
    _seed_run_dir(tmp_path, SID)  # ledger present, no WAL evidence
    _write_sidecar(
        tmp_path,
        SID,
        rows=[
            {
                "ts_ms": 1_780_000_000_000,
                "symbol": "FROM_SIDECAR",
                "policy_kind": "SetHoldings",
                "policy_value": "1.0",
                "intended_qty": 7,
                "reference_price": "300.00",
                "sized_via": "policy_set_holdings",
            }
        ],
    )

    rows = live_instances._sizing_audit_rows(SID)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "FROM_SIDECAR"


def test_falls_back_to_sidecar_when_no_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy pre-WAL runs have no live_runs/<run_id>/run_ledger.json
    tying back to the instance. The fold helper isn't reachable; the
    sidecar projection is the only surface."""
    _stub_settings(monkeypatch, tmp_path)
    _write_sidecar(
        tmp_path,
        SID,
        rows=[
            {
                "ts_ms": 1_780_000_000_000,
                "symbol": "LEGACY_ONLY",
                "policy_kind": "SetHoldings",
                "policy_value": "1.0",
                "intended_qty": 1,
                "reference_price": "50.00",
                "sized_via": "policy_set_holdings",
            }
        ],
    )

    rows = live_instances._sizing_audit_rows(SID)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "LEGACY_ONLY"


def test_sizing_surface_accepts_sidecar_row_without_reference_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FixedShares can resolve without a reference price. The status/catalog
    path must preserve that audit row instead of failing Pydantic validation."""
    _stub_settings(monkeypatch, tmp_path)
    run_dir = _seed_run_dir(tmp_path, SID)
    ledger = json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))
    ledger["live_config"] = {
        "sizing": {
            "kind": "FixedShares",
            "value": 1,
        }
    }
    (run_dir / "run_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    _write_sidecar(
        tmp_path,
        SID,
        rows=[
            {
                "ts_ms": 1_780_000_000_000,
                "symbol": "SPY",
                "policy_kind": "FixedShares",
                "policy_value": "1",
                "intended_qty": 1,
                "reference_price": None,
                "sized_via": "policy_set_holdings",
            }
        ],
    )

    sizing = live_instances._sizing(
        tmp_path / "live_runs",
        live_binding=None,
        runs=[
            {
                "run_id": "run-001",
                "run_dir": str(run_dir),
                "created_at_ms": 1_780_000_000_000,
            }
        ],
        strategy_instance_id=SID,
    )

    assert sizing is not None
    assert len(sizing.per_trade_audit) == 1
    assert sizing.per_trade_audit[0].reference_price is None


def test_empty_when_neither_source_has_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_settings(monkeypatch, tmp_path)
    _seed_run_dir(tmp_path, SID)  # ledger present, no WAL, no sidecar

    rows = live_instances._sizing_audit_rows(SID)

    assert rows == []


def test_wal_fold_includes_multiple_symbols_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end sanity: a few real-shaped WAL events fold + sort
    newest-first through the wired endpoint."""
    _stub_settings(monkeypatch, tmp_path)
    run_dir = _seed_run_dir(tmp_path, SID)
    wal = IntentWal(run_dir / "intent_events.jsonl")
    _write_sizing_resolved(wal, symbol="AAPL", ts_ms=1_780_000_000_000)
    _write_sizing_resolved(wal, symbol="MSFT", ts_ms=1_780_000_001_000)
    _write_sizing_resolved(wal, symbol="NVDA", ts_ms=1_780_000_002_000)

    rows = live_instances._sizing_audit_rows(SID)

    assert [r["symbol"] for r in rows] == ["NVDA", "MSFT", "AAPL"]

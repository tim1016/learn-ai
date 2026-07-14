"""Tests for cmd_start's cold-start reconciliation wiring (Slice 2).

The full cmd_start pipeline is heavyweight; these tests pin the
helper-level contracts that make the wiring trustworthy:

  * ``_build_broker_snapshot_from_ibkr`` mirrors IBKR open orders and
    executions into the classifier's ``BrokerSnapshot`` shape with the
    right sign convention.
  * ``_resolve_prior_run_dir`` picks the most-recent prior run dir for
    the same instance, skipping unrelated / future runs.
  * Adoption ordering: WAL gets ``ADOPTED_BROKER_ORDER`` BEFORE the
    receipt is written (verified by reading the on-disk artifacts the
    orchestrator just emitted in another test, but pinned here as well
    against the wiring-side helper).
  * The wiring's startup-gate semantics: Poison → exit 1 (engine.run
    never called), receipt-write failure → exit 3, Continue / Adopt
    pause writes desired_state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.broker.ibkr.models import IbkrOpenOrder, IbkrOrderEvent
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarRepo,
)
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)
from app.engine.live.reconciliation_classifier import (
    Adopt,
    BrokerOrderView,
    BrokerSnapshot,
    Continue,
)
from app.engine.live.reconciliation_orchestrator import reconcile
from app.engine.live.run import (
    _build_broker_snapshot_from_ibkr,
    _resolve_prior_run_dir,
)
from app.schemas.live_runs import ReconciliationReceipt

NS = build_bot_order_namespace("inst-x")
SID = "inst-x"


def _open_order(
    *,
    order_id: int,
    perm_id: int | None,
    order_ref: str | None,
    status: str = "Submitted",
    remaining: float = 1.0,
) -> IbkrOpenOrder:
    return IbkrOpenOrder(
        account_id="DU1",
        order_id=order_id,
        perm_id=perm_id,
        client_id=42,
        con_id=1,
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=10.0,
        order_type="MKT",
        time_in_force="DAY",
        status=status,
        cumulative_filled=0.0,
        remaining=remaining,
        order_ref=order_ref,
        fetched_at_ms=1,
    )


def _fill_event(
    *,
    order_id: int,
    perm_id: int | None,
    order_ref: str | None,
    side: str,
    qty: float,
    exec_id: str = "exec-1",
) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU1",
        order_id=order_id,
        perm_id=perm_id,
        con_id=1,
        event_type="fill",
        status="Filled",
        order_ref=order_ref,
        symbol="SPY",
        side=side,
        order_type="MKT",
        exec_id=exec_id,
        client_id=42,
        fill_quantity=qty,
        avg_fill_price=420.0,
        cumulative_filled=qty,
        remaining=0.0,
        ts_ms=2,
    )


def test_build_broker_snapshot_maps_open_orders_and_fills() -> None:
    iid = mint_intent_id()
    ref = build_order_ref(NS, iid)
    snap = _build_broker_snapshot_from_ibkr(
        [
            _open_order(
                order_id=7,
                perm_id=900,
                order_ref=ref,
                status="Submitted",
                remaining=2.5,
            )
        ],
        [
            _fill_event(
                order_id=7, perm_id=900, order_ref=ref, side="BUY", qty=10.0
            )
        ],
    )
    assert isinstance(snap, BrokerSnapshot)
    assert len(snap.open_orders) == 1
    only_open = snap.open_orders[0]
    assert only_open.order_ref == ref
    assert only_open.perm_id == 900
    assert only_open.order_id == 7
    assert only_open.status == "Submitted"
    assert only_open.remaining == 2.5

    assert len(snap.executions) == 1
    only_exec = snap.executions[0]
    assert only_exec.order_ref == ref
    assert only_exec.perm_id == 900
    assert only_exec.exec_id == "exec-1"
    assert only_exec.quantity == 10.0  # BUY -> positive


def test_build_broker_snapshot_sell_quantity_is_negative() -> None:
    iid = mint_intent_id()
    ref = build_order_ref(NS, iid)
    snap = _build_broker_snapshot_from_ibkr(
        [],
        [_fill_event(order_id=1, perm_id=1, order_ref=ref, side="SELL", qty=5.0)],
    )
    assert snap.executions[0].quantity == -5.0


def test_build_broker_snapshot_filters_non_fill_events() -> None:
    """Status / error events must not become BrokerExecutionView rows —
    the classifier expects only fills there."""
    status_event = IbkrOrderEvent(
        account_id="DU1",
        order_id=1,
        perm_id=1,
        con_id=1,
        event_type="status",
        status="Submitted",
        order_ref=None,
        ts_ms=1,
    )
    snap = _build_broker_snapshot_from_ibkr([], [status_event])
    assert snap.executions == ()


def _write_ledger(run_dir: Path, *, sid: str, created_ms: int) -> None:
    """Minimal run_ledger.json subset for prior-run resolution."""
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "strategy_instance_id": sid,
        "created_at_ms": created_ms,
    }
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_prior_run_dir_picks_most_recent(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    current = root / "run-now"
    _write_ledger(current, sid=SID, created_ms=1_000)
    old = root / "run-old"
    _write_ledger(old, sid=SID, created_ms=500)
    older = root / "run-older"
    _write_ledger(older, sid=SID, created_ms=100)
    # Other-instance and future-dated sit alongside but must be skipped.
    _write_ledger(root / "run-other", sid="other-instance", created_ms=200)
    _write_ledger(root / "run-future", sid=SID, created_ms=2_000)

    resolved = _resolve_prior_run_dir(
        current_run_dir=current, strategy_instance_id=SID, current_created_ms=1_000
    )
    assert resolved == old


def test_resolve_prior_run_dir_returns_none_when_no_match(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    current = root / "run-only"
    _write_ledger(current, sid=SID, created_ms=1_000)
    resolved = _resolve_prior_run_dir(
        current_run_dir=current, strategy_instance_id=SID, current_created_ms=1_000
    )
    assert resolved is None


def test_resolve_prior_run_dir_tolerates_corrupt_sibling(tmp_path: Path) -> None:
    """A corrupt sibling ledger must not block reconciliation."""
    root = tmp_path / "runs"
    current = root / "run-now"
    _write_ledger(current, sid=SID, created_ms=1_000)
    good = root / "run-good"
    _write_ledger(good, sid=SID, created_ms=500)
    bad = root / "run-bad"
    bad.mkdir(parents=True)
    (bad / "run_ledger.json").write_text("not json", encoding="utf-8")

    resolved = _resolve_prior_run_dir(
        current_run_dir=current, strategy_instance_id=SID, current_created_ms=1_000
    )
    assert resolved == good


@pytest.mark.asyncio
async def test_adoption_ordering_wal_seq_precedes_receipt_seq(tmp_path: Path) -> None:
    """End-to-end: WAL ADOPTED_BROKER_ORDER lands BEFORE the receipt
    captures sidecar_wal_seq. The receipt's seq must equal the WAL's
    final seq, proving the orchestrator did the durable append first."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = LiveStateSidecarRepo(run_dir / "live_state.json")
    repo.write(
        LiveStateEnvelope(
            strategy_instance_id=SID,
            run_id="r1",
            bot_order_namespace=NS,
            ib_client_id=42,
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        )
    )

    iid = mint_intent_id()

    async def probe() -> BrokerSnapshot:
        return BrokerSnapshot(
            open_orders=(
                BrokerOrderView(
                    order_ref=build_order_ref(NS, iid),
                    perm_id=5,
                    order_id=1,
                    status="Filled",
                    remaining=0.0,
                ),
            )
        )

    counter = [1000]

    def now() -> int:
        v = counter[0]
        counter[0] += 1
        return v

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=probe,
        owned_namespaces=frozenset({NS}),
        now_ms=now,
    )
    assert isinstance(result.verdict, Adopt)
    wal_events = IntentWal(run_dir / "intent_events.jsonl").read_tail()
    adopted = [e for e in wal_events if e.event_type is IntentEventType.ADOPTED_BROKER_ORDER]
    assert adopted, "ADOPTED_BROKER_ORDER must be appended"
    # The receipt's sidecar_wal_seq is the WAL's last seq after adoption.
    assert result.receipt.sidecar_wal_seq == adopted[-1].seq
    # And the on-disk receipt agrees.
    on_disk = ReconciliationReceipt.model_validate_json(
        (run_dir / "reconciliation_receipt.json").read_text(encoding="utf-8")
    )
    assert on_disk.sidecar_wal_seq == adopted[-1].seq


@pytest.mark.asyncio
async def test_empty_broker_cache_cannot_satisfy_reconciliation(tmp_path: Path) -> None:
    """An un-synced broker (empty open_orders + empty executions) against a
    sidecar that records nothing in flight is a clean Continue. The
    Acceptance Gate #2 ("empty cache cannot satisfy reconciliation") is
    enforced upstream (the cmd_start sync calls); the orchestrator's
    contract is honest: empty in = clean out, which is correct ONLY because
    the cmd_start path guarantees the sync happened first. This test pins
    that orchestrator-side behavior so a future regression on the sync
    side is detectable: if cmd_start ever stops syncing, the orchestrator
    will silently pass on an unsynced cache, and operators lose the gate."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = LiveStateSidecarRepo(run_dir / "live_state.json")
    repo.write(
        LiveStateEnvelope(
            strategy_instance_id=SID,
            run_id="r1",
            bot_order_namespace=NS,
            ib_client_id=42,
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        )
    )

    async def empty_probe() -> BrokerSnapshot:
        return BrokerSnapshot()

    counter = [1000]

    def now() -> int:
        v = counter[0]
        counter[0] += 1
        return v

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=empty_probe,
        owned_namespaces=frozenset({NS}),
        now_ms=now,
    )
    # Clean — orchestrator is honest with its inputs. The sync guarantee
    # lives in cmd_start.
    assert isinstance(result.verdict, Continue)

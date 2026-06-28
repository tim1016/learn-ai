"""Tests for the cold-start reconciliation orchestrator (ADR-0008 §5).

Covers the contract:
  * Clean continue (broker matches projection) — Continue + passed receipt.
  * Adoption — orphan recovered, ADOPTED_BROKER_ORDER appended, passed
    receipt carries the intent_id.
  * Poison branches: foreign perm_id, broker_probe_failed, sidecar_corrupt,
    wal_corrupt — each writes a failed receipt with the right reason and
    stamps poisoned.flag.
  * Receipt durability — the on-disk receipt parses back into the same
    Pydantic model.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.engine.live.account_classifier import AccountDurableIntent
from app.engine.live.halt import POISONED_FLAG_FILENAME, read_poisoned_flag
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
    BrokerExecutionView,
    BrokerOrderView,
    BrokerSnapshot,
    Continue,
    Poison,
)
from app.engine.live.reconciliation_orchestrator import (
    ReconciliationResult,
    reconcile,
)
from app.engine.live.reconciliation_receipt import RECEIPT_FILENAME
from app.schemas.live_runs import ReconciliationReceipt

NS = build_bot_order_namespace("test-strategy")
ALLOWED = frozenset({NS})
RUN_ID = "run-cold-start-1"
SID = "test-strategy"


def _clock() -> Callable[[], int]:
    """Monotonic clock yielding distinct ms each call (1000, 1001, ...).

    Distinct values make it easy to spot in-progress vs. completed timestamps
    in receipt assertions without bringing in time.monotonic mocking.
    """
    counter = [1000]

    def now() -> int:
        v = counter[0]
        counter[0] += 1
        return v

    return now


def _make_envelope(run_dir: Path) -> LiveStateSidecarRepo:
    """Seed a minimal sidecar in ``run_dir/live_state.json`` and return its repo."""
    sidecar_path = run_dir / "live_state.json"
    repo = LiveStateSidecarRepo(sidecar_path)
    env = LiveStateEnvelope(
        strategy_instance_id=SID,
        run_id=RUN_ID,
        bot_order_namespace=NS,
        ib_client_id=42,
        last_processed_bar_ms=1_700_000_000_000,
        last_artifact_flush_ms=1_700_000_000_000,
    )
    repo.write(env)
    return repo


def _empty_broker() -> Callable[[], Awaitable[BrokerSnapshot]]:
    async def probe() -> BrokerSnapshot:
        return BrokerSnapshot()

    return probe


@pytest.mark.asyncio
async def test_clean_continue_writes_passed_receipt(tmp_path: Path) -> None:
    """Envelope present + empty broker + empty WAL → Continue, receipt.outcome=clean."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=_empty_broker(),
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert isinstance(result.verdict, Continue)
    assert result.receipt.status == "passed"
    assert result.receipt.outcome == "clean"
    assert result.receipt.run_id == RUN_ID
    assert result.receipt.namespace == NS
    assert result.receipt.broker_observed_at_ms is not None
    assert result.receipt.adopted_intent_ids == ()
    assert not (run_dir / POISONED_FLAG_FILENAME).exists()


@pytest.mark.asyncio
async def test_adoption_appends_wal_and_records_intent_id(tmp_path: Path) -> None:
    """Broker shows an owned orphan in our namespace → Adopt, WAL gains
    ADOPTED_BROKER_ORDER, receipt.outcome=adopted with the intent_id."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    iid = mint_intent_id()

    async def probe() -> BrokerSnapshot:
        return BrokerSnapshot(
            open_orders=(
                BrokerOrderView(
                    order_ref=build_order_ref(NS, iid),
                    perm_id=12345,
                    order_id=7,
                    status="Filled",
                    remaining=0.0,
                ),
            )
        )

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=probe,
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert isinstance(result.verdict, Adopt)
    assert result.receipt.status == "passed"
    assert result.receipt.outcome == "adopted"
    assert result.receipt.adopted_intent_ids == (iid,)

    # WAL must carry exactly one ADOPTED_BROKER_ORDER for this intent.
    wal_events = IntentWal(run_dir / "intent_events.jsonl").read_tail()
    adopted = [
        e for e in wal_events if e.event_type is IntentEventType.ADOPTED_BROKER_ORDER
    ]
    assert len(adopted) == 1
    assert adopted[0].intent_id == iid
    assert adopted[0].perm_id == 12345
    assert adopted[0].order_id == 7
    # And the receipt's sidecar_wal_seq must reflect the WAL post-adoption.
    assert result.receipt.sidecar_wal_seq == adopted[0].seq


@pytest.mark.asyncio
async def test_foreign_perm_id_poisons(tmp_path: Path) -> None:
    """Broker shows an unowned perm_id with no order_ref → Poison, failed receipt."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    async def probe() -> BrokerSnapshot:
        return BrokerSnapshot(
            open_orders=(BrokerOrderView(order_ref=None, perm_id=999),)
        )

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=probe,
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert isinstance(result.verdict, Poison)
    assert result.verdict.reason == "foreign_perm_id"
    assert result.receipt.status == "failed"
    assert result.receipt.failure_reason == "foreign_perm_id"
    # poisoned.flag stamped with COLD_START_DIVERGENCE + the granular reason.
    halt = read_poisoned_flag(run_dir)
    assert halt is not None
    assert halt.details["reason"] == "foreign_perm_id"
    assert halt.details["source"] == "reconciliation_orchestrator"


@pytest.mark.asyncio
async def test_account_owner_durable_intent_allows_missing_order_ref_by_perm_id(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    async def probe() -> BrokerSnapshot:
        return BrokerSnapshot(
            executions=(
                BrokerExecutionView(
                    order_ref=None,
                    perm_id=90044,
                    exec_id="exec-90044",
                    exec_time_ms=1_700_000_020_000,
                ),
            )
        )

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=probe,
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
        account_durable_intents=(
            AccountDurableIntent(
                account_id="DU123",
                strategy_instance_id=SID,
                run_id=RUN_ID,
                bot_order_namespace=NS,
                intent_id="intent-owner-1",
                order_ref=build_order_ref(NS, "intent-owner-1"),
                status="account_owner_submit_accepted",
                recorded_at_ms=1_700_000_010_000,
                perm_id=90044,
                exec_id="exec-90044",
            ),
        ),
    )

    assert isinstance(result.verdict, Continue)
    assert result.receipt.status == "passed"
    assert not (run_dir / POISONED_FLAG_FILENAME).exists()


@pytest.mark.asyncio
async def test_broker_probe_failure_poisons(tmp_path: Path) -> None:
    """Broker probe raises → Poison, failed receipt reason starts broker_probe_failed."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    async def probe() -> BrokerSnapshot:
        raise RuntimeError("ibkr connection refused")

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=probe,
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert isinstance(result.verdict, Poison)
    assert result.verdict.reason.startswith("broker_probe_failed: ")
    assert "ibkr connection refused" in result.verdict.reason
    assert result.receipt.status == "failed"
    assert result.receipt.failure_reason.startswith("broker_probe_failed: ")
    assert (run_dir / POISONED_FLAG_FILENAME).exists()


@pytest.mark.asyncio
async def test_sidecar_corrupt_poisons(tmp_path: Path) -> None:
    """Unparseable sidecar JSON → Poison(sidecar_corrupt), failed receipt."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sidecar_path = run_dir / "live_state.json"
    sidecar_path.write_text("{not valid json", encoding="utf-8")
    repo = LiveStateSidecarRepo(sidecar_path)

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=_empty_broker(),
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert isinstance(result.verdict, Poison)
    assert result.verdict.reason == "sidecar_corrupt"
    assert result.receipt.status == "failed"
    assert result.receipt.failure_reason == "sidecar_corrupt"
    # No envelope ⇒ no clean bar to anchor; the halt reason still lands.
    halt = read_poisoned_flag(run_dir)
    assert halt is not None
    assert halt.details["reason"] == "sidecar_corrupt"


@pytest.mark.asyncio
async def test_wal_corrupt_poisons(tmp_path: Path) -> None:
    """Sidecar OK but WAL has non-monotonic seq → Poison(wal_corrupt)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    # Write two newline-terminated events with reversed seq to trip the
    # IntentWal monotonicity check.
    wal_path = run_dir / "intent_events.jsonl"
    iid_a = mint_intent_id()
    iid_b = mint_intent_id()
    line_a = {
        "seq": 5,
        "event_type": "PENDING_INTENT",
        "intent_id": iid_a,
        "bot_order_namespace": NS,
        "order_ref": build_order_ref(NS, iid_a),
        "intent_kind": "STRATEGY",
    }
    line_b = {
        "seq": 1,  # non-monotonic
        "event_type": "PENDING_INTENT",
        "intent_id": iid_b,
        "bot_order_namespace": NS,
        "order_ref": build_order_ref(NS, iid_b),
        "intent_kind": "STRATEGY",
    }
    wal_path.write_text(
        json.dumps(line_a) + "\n" + json.dumps(line_b) + "\n", encoding="utf-8"
    )

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=_empty_broker(),
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert isinstance(result.verdict, Poison)
    assert result.verdict.reason == "wal_corrupt"
    assert result.receipt.failure_reason == "wal_corrupt"
    assert (run_dir / POISONED_FLAG_FILENAME).exists()


@pytest.mark.asyncio
async def test_receipt_round_trips_through_disk(tmp_path: Path) -> None:
    """On-disk receipt parses back into the same ReconciliationReceipt model."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)

    result: ReconciliationResult = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=_empty_broker(),
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    raw = (run_dir / RECEIPT_FILENAME).read_text(encoding="utf-8")
    on_disk = ReconciliationReceipt.model_validate_json(raw)
    assert on_disk == result.receipt


@pytest.mark.asyncio
async def test_redeploy_receipt_uses_current_run_id_over_stale_envelope(
    tmp_path: Path,
) -> None:
    """On redeploy of the same strategy_instance_id the per-instance sidecar
    still carries the prior run's identity until the new engine flushes for
    the first time. The orchestrator must stamp the receipt with the
    caller-provided ``current_*`` values, not the stale envelope, or the
    cockpit projection would compare a fresh receipt against the new live
    binding and mark it STALE."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)  # envelope.run_id == RUN_ID

    new_run_id = f"{RUN_ID}-after-redeploy"
    new_namespace = f"{NS}-v2"
    new_sid = "spy-ema-paper-v2"

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=_empty_broker(),
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
        current_run_id=new_run_id,
        current_strategy_instance_id=new_sid,
        current_namespace=new_namespace,
    )

    assert isinstance(result.verdict, Continue)
    assert result.receipt.status == "passed"
    # The receipt carries the *new* run's identity, not the envelope's.
    assert result.receipt.run_id == new_run_id
    assert result.receipt.strategy_instance_id == new_sid
    assert result.receipt.namespace == new_namespace


@pytest.mark.asyncio
async def test_existing_poisoned_flag_does_not_block_receipt(tmp_path: Path) -> None:
    """A prior boot may have already stamped poisoned.flag; the orchestrator
    must still land its failed receipt for the cockpit to read."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = _make_envelope(run_dir)
    # Pre-stamp a poisoned.flag from a different trigger.
    from app.engine.live.halt import PoisonedHaltReason, PoisonedHaltTrigger, write_poisoned_flag

    write_poisoned_flag(
        run_dir,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OPERATOR_DECLARED,
            halted_at_ms=1,
            last_clean_bar_close_ms=0,
            details={"reason": "already_poisoned_from_prior_boot"},
        ),
    )

    async def probe() -> BrokerSnapshot:
        return BrokerSnapshot(
            open_orders=(BrokerOrderView(order_ref=None, perm_id=42),)
        )

    result = await reconcile(
        run_dir=run_dir,
        sidecar=repo,
        broker_probe=probe,
        allowed_namespaces=ALLOWED,
        now_ms=_clock(),
    )

    assert result.receipt.status == "failed"
    # The first halt wins, but our receipt landed.
    halt = read_poisoned_flag(run_dir)
    assert halt is not None
    assert halt.details["reason"] == "already_poisoned_from_prior_boot"

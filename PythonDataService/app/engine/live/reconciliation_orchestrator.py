"""Cold-start reconciliation orchestrator (ADR-0008 §5 / PR 1 of the
cold-start reconciliation gate).

This is the canonical caller of ``reconciliation_classifier.classify``: it
loads the live-state sidecar, folds this run's WAL tail, gathers the prior
run's unresolved tail and emergency-flatten audit, probes the broker, runs
the classifier, and writes a durable ``reconciliation_receipt.json`` for
every cold start. Continue / Adopt verdicts return for the caller to wire
into the engine; Poison verdicts also stamp ``poisoned.flag`` so the
familiar fatal-halt machinery reads them.

A receipt is **always** written before this function returns — even on
broker failure or corrupt artifacts — and is written via the
``in_progress`` sentinel pattern so a crash mid-reconcile cannot leave
stale ``passed`` evidence from the previous boot. The startup gate treats
a receipt-write failure as fatal: "no submit without a durable receipt."
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.engine.live.halt import (
    PoisonedHaltReason,
    PoisonedHaltTrigger,
    write_poisoned_flag,
)
from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_ledger import fold, projection_from_envelope
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
)
from app.engine.live.reconciliation_classifier import (
    Adopt,
    BrokerSnapshot,
    Poison,
    ReconcileVerdict,
    classify,
)
from app.engine.live.reconciliation_receipt import write_receipt
from app.schemas.live_runs import ReconciliationReceipt

# Events from the prior run's WAL that may still need resolution against the
# broker on this boot. SUBMIT_UNCERTAIN_HALTED is intentionally excluded — by
# the time it lands the prior run has already halted, so the operator owns
# resolution rather than this orchestrator.
_PRIOR_RUN_UNACKED_TYPES = frozenset(
    {
        IntentEventType.PENDING_INTENT,
        IntentEventType.ACK_FAILED_UNCERTAIN,
    }
)


@dataclass(frozen=True)
class ReconciliationResult:
    """What the orchestrator returns to its caller (the startup gate)."""

    verdict: ReconcileVerdict
    receipt: ReconciliationReceipt


async def reconcile(
    *,
    run_dir: Path,
    sidecar: LiveStateSidecarRepo,
    broker_probe: Callable[[], Awaitable[BrokerSnapshot]],
    allowed_namespaces: frozenset[str],
    now_ms: Callable[[], int],
    prior_run_dir: Path | None = None,
) -> ReconciliationResult:
    """Run the cold-start reconciliation procedure and persist a receipt.

    Steps (in order):
      1. Read sidecar; corruption → Poison(``sidecar_corrupt``).
      2. Read this run's WAL tail; corruption → Poison(``wal_corrupt``).
      3. Fold WAL over the sidecar envelope's projection.
      4. (Optional) Read prior run's WAL tail for unresolved intents.
      5. (Optional) Read prior emergency-flatten audit tail.
      6. Probe the broker; any exception → Poison(``broker_probe_failed: ...``).
      7. Classify.
      8. On Poison: stamp ``poisoned.flag`` (existing flag is tolerated).
      9. On Adopt: append ``ADOPTED_BROKER_ORDER`` to this run's WAL.
     10. Always write a final ``passed`` / ``failed`` receipt that replaces
         the ``in_progress`` sentinel written up front.
    """
    started_at_ms = now_ms()

    # Identity for the receipt — read the sidecar once for both the
    # in-progress sentinel and the projection. A corrupt sidecar means the
    # envelope is unusable; we still need to write a receipt, so fall back
    # to whatever identity we can recover from the run_dir's run_id later.
    envelope = None
    sidecar_corrupt = False
    try:
        envelope = sidecar.read()
    except LiveStateSidecarCorruptError:
        sidecar_corrupt = True

    run_id = envelope.run_id if envelope is not None else _read_run_id_from_dir(run_dir)
    strategy_instance_id = (
        envelope.strategy_instance_id if envelope is not None else ""
    )
    namespace = envelope.bot_order_namespace if envelope is not None else ""

    # Step 10 (durability): write the in-progress sentinel first so a crash
    # between here and the verdict write cannot leave the previous run's
    # stale ``passed`` receipt on disk.
    in_progress = ReconciliationReceipt(
        status="in_progress",
        run_id=run_id,
        strategy_instance_id=strategy_instance_id,
        namespace=namespace,
        started_at_ms=started_at_ms,
    )
    write_receipt(run_dir, in_progress)

    if sidecar_corrupt:
        return _poison_and_record(
            run_dir=run_dir,
            reason="sidecar_corrupt",
            envelope_last_bar_ms=None,
            now_ms=now_ms,
            base_receipt=in_progress,
        )
    assert envelope is not None  # narrowed by sidecar_corrupt branch above

    # Step 2: fold WAL.
    wal = IntentWal(run_dir / "intent_events.jsonl")
    try:
        wal_events = wal.read_tail()
    except IntentWalCorruptError:
        return _poison_and_record(
            run_dir=run_dir,
            reason="wal_corrupt",
            envelope_last_bar_ms=envelope.last_processed_bar_ms,
            now_ms=now_ms,
            base_receipt=in_progress,
        )
    ledger_view = fold(projection_from_envelope(envelope), wal_events)

    # Step 4: prior-run unresolved tail. Corruption here is informational —
    # treat as empty so a single broken prior-run WAL doesn't gate this boot.
    prior_tail: list[IntentEvent] = []
    if prior_run_dir is not None and (prior_run_dir / "intent_events.jsonl").exists():
        prior_wal = IntentWal(prior_run_dir / "intent_events.jsonl")
        try:
            all_prior = prior_wal.read_tail()
        except IntentWalCorruptError:
            all_prior = []
        prior_tail = [e for e in all_prior if e.event_type in _PRIOR_RUN_UNACKED_TYPES]

    # Step 5: prior emergency-flatten audit. Same lenient corruption policy.
    emergency_audit: list[IntentEvent] = []
    if (
        prior_run_dir is not None
        and (prior_run_dir / "emergency_flatten_audit.jsonl").exists()
    ):
        audit_wal = IntentWal(prior_run_dir / "emergency_flatten_audit.jsonl")
        try:
            emergency_audit = audit_wal.read_tail()
        except IntentWalCorruptError:
            emergency_audit = []

    # Step 6: broker probe. ANY exception is a Poison — we cannot
    # distinguish a clean cold start from one with hidden divergence.
    try:
        broker_snapshot = await broker_probe()
    except Exception as exc:
        return _poison_and_record(
            run_dir=run_dir,
            reason=f"broker_probe_failed: {exc}",
            envelope_last_bar_ms=envelope.last_processed_bar_ms,
            now_ms=now_ms,
            base_receipt=in_progress,
        )

    broker_observed_at_ms = now_ms()

    # Step 7: classify.
    verdict = classify(
        projection=ledger_view,
        broker_snapshot=broker_snapshot,
        allowed_namespaces=allowed_namespaces,
        prior_run_unacked_tail=prior_tail,
        emergency_audit=emergency_audit,
    )

    # Step 8: poison branch.
    if isinstance(verdict, Poison):
        return _poison_and_record(
            run_dir=run_dir,
            reason=verdict.reason,
            envelope_last_bar_ms=envelope.last_processed_bar_ms,
            now_ms=now_ms,
            base_receipt=in_progress,
        )

    # Step 9: adopt branch — append ADOPTED_BROKER_ORDER for each orphan
    # BEFORE writing the receipt, so on-disk evidence orders correctly
    # (receipt.sidecar_wal_seq reflects the post-adoption WAL state).
    adopted_intent_ids: tuple[str, ...] = ()
    if isinstance(verdict, Adopt):
        for orphan in verdict.orphans:
            wal.append(
                event_type=IntentEventType.ADOPTED_BROKER_ORDER,
                intent_id=orphan.intent_id,
                bot_order_namespace=envelope.bot_order_namespace,
                order_ref=orphan.order_ref,
                order_id=orphan.order_id,
                perm_id=orphan.perm_id,
                ts_ms=now_ms(),
            )
        adopted_intent_ids = tuple(o.intent_id for o in verdict.orphans)

    outcome = "adopted" if isinstance(verdict, Adopt) else "clean"
    final_seq = ledger_view.last_seq + (
        len(verdict.orphans) if isinstance(verdict, Adopt) else 0
    )
    completed_at_ms = now_ms()
    final_receipt = in_progress.model_copy(
        update={
            "status": "passed",
            "outcome": outcome,
            "completed_at_ms": completed_at_ms,
            "last_reconcile_ms": completed_at_ms,
            "sidecar_wal_seq": final_seq,
            "broker_observed_at_ms": broker_observed_at_ms,
            "adopted_intent_ids": adopted_intent_ids,
        }
    )
    write_receipt(run_dir, final_receipt)
    return ReconciliationResult(verdict=verdict, receipt=final_receipt)


def _poison_and_record(
    *,
    run_dir: Path,
    reason: str,
    envelope_last_bar_ms: int | None,
    now_ms: Callable[[], int],
    base_receipt: ReconciliationReceipt,
) -> ReconciliationResult:
    """Stamp poisoned.flag + write the failed receipt + return.

    ``envelope_last_bar_ms`` is the last clean bar from the sidecar when we
    could read it; ``None`` (or a synthesized 1) when the sidecar itself is
    the source of the poison and there is no clean bar to anchor on.
    ``write_poisoned_flag`` refuses to overwrite an existing flag — a prior
    boot may have already poisoned this run_dir — which we tolerate so the
    receipt still lands.
    """
    halt_reason = PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
        halted_at_ms=now_ms(),
        last_clean_bar_close_ms=envelope_last_bar_ms or 1,
        details={"reason": reason, "source": "reconciliation_orchestrator"},
    )
    # Already poisoned (e.g. by a prior boot or another fatal-halt source)
    # is tolerated: the first halt's reason wins per the flag's contract,
    # but the receipt must still land for the cockpit to read.
    with contextlib.suppress(FileExistsError):
        write_poisoned_flag(run_dir, halt_reason)

    completed_at_ms = now_ms()
    failed = base_receipt.model_copy(
        update={
            "status": "failed",
            "completed_at_ms": completed_at_ms,
            "last_reconcile_ms": completed_at_ms,
            "failure_reason": reason,
        }
    )
    write_receipt(run_dir, failed)
    return ReconciliationResult(verdict=Poison(reason=reason), receipt=failed)


def _read_run_id_from_dir(run_dir: Path) -> str:
    """Best-effort run_id recovery when the sidecar is unreadable.

    The orchestrator's receipt must carry a ``run_id`` even when the
    sidecar's envelope is corrupt. We fall back to the run_dir's basename
    (which is the run_id by convention in this codebase) so the receipt
    parses back; the operator can still correlate it against the dir.
    """
    return run_dir.name


__all__ = ["ReconciliationResult", "reconcile"]

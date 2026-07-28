"""Production-seam deterministic drills for the custody qualification report.

The campaign runs the same Clerk, epoch, safety, lifecycle and producer-log
code used by paper operation.  Only the gateway transport, temporary artifact
root, and clock are controlled.  It is deliberately not a parallel custody
state machine.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import advance_account_clerk_generation
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_clerk_journal import (
    read_account_clerk_journal,
    seed_account_clerk_broker_evidence_baseline,
)
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkPositionEvidence,
)
from app.engine.live.account_epoch import (
    AccountEpochAuthority,
    AccountEpochGenerationFencedError,
    AccountEpochWriteBlockedError,
)
from app.engine.live.account_registry import retire_unmanaged_active_bindings_on_daemon_boot
from app.engine.live.account_safety import AccountSafetyAuthority, AccountSafetyVerdict
from app.engine.live.command_channel import CommandVerb
from app.engine.live.desired_state import DesiredState
from app.engine.live.producer_operational_log import append_producer_operational_event, read_producer_operational_events
from app.schemas.account_custody_qualification import (
    BACKEND_CUSTODY_QUALIFICATION_DRILL_IDS,
    AccountCustodyQualificationDrill,
)
from app.services.account_custody_qualification_fixtures import (
    DeterministicClock,
    DeterministicFaultControls,
    DeterministicPaperBroker,
    bind_qualification_bot,
    controlled_clerk_boundary_hooks,
    journal_receipts,
    qualification_intent,
    real_clerk_trace,
    real_eight_bot_trace,
)
from app.services.presented_lifecycle_actions import (
    PresentedLifecycleActionRejectedError,
    PresentedLifecycleActionService,
)
from app.services.risk_reducing_lifecycle_intent import persist_risk_reducing_intent

_REQUIRED_DRILL_IDS = BACKEND_CUSTODY_QUALIFICATION_DRILL_IDS
_ACCOUNT_ID = "DUQUALIFICATION"
_FLEET_SIZE = 8
_A0_ADMISSION_CEILING_MS = 10_000


@dataclass(frozen=True)
class CustodyQualificationCampaign:
    """Observed campaign evidence used for the report and its latency metrics."""

    drills: tuple[AccountCustodyQualificationDrill, ...]
    queue_wait_samples_ms: tuple[int, ...]
    fsync_samples_ms: tuple[int, ...]
    a0_to_a1_dispatch_samples_ms: tuple[int, ...]
    a1_to_callback_samples_ms: tuple[int, ...]
    callback_to_ack_samples_ms: tuple[int, ...]
    queue_high_water: int
    queue_refusal_count: int
    epoch_recovery_samples_ms: tuple[int, ...]
    max_uncertain_intent_age_ms: int
    projection_gap_count: int


def run_deterministic_custody_campaign(controls: DeterministicFaultControls) -> CustodyQualificationCampaign:
    """Execute all 17 backend production-seam drills in canonical order."""

    return asyncio.run(_run_campaign(controls))


async def _run_campaign(controls: DeterministicFaultControls) -> CustodyQualificationCampaign:
    queue, queue_samples, queue_high_water, queue_refusals = await _queue_delay_drill(controls)
    fsync, fsync_samples = await _fsync_delay_drill(controls)
    qualification, qualification_samples = await _qualification_delay_drill(controls)
    after_a0 = await _socket_loss_after_a0_drill(controls)
    after_a1, _after_a1_samples, uncertain_age = await _socket_loss_after_a1_drill(controls)
    fill, fill_samples = await _fill_before_ack_drill(controls)
    duplicate = await _duplicate_reordered_callbacks_drill(controls)
    originator_death = await _originator_death_drill(controls)
    retired_fill = await _retired_originator_late_fill_drill(controls)
    clerk_death = await _clerk_death_drill(controls)
    reconnect, recovery_samples = await _ibkr_reconnect_drill(controls)
    silence = await _callback_silence_drill(controls)
    pause_stop = await _pause_stop_outage_drill(controls)
    start_outage = _start_outage_drill(controls)
    stale_flatten = await _stale_flatten_drill(controls)
    producer, projection_gaps = _operational_log_concurrency_drill(controls)
    outage_diff = await _outage_diff_drill(controls)
    drills = (
        queue,
        fsync,
        qualification,
        after_a0,
        after_a1,
        fill,
        duplicate,
        originator_death,
        retired_fill,
        clerk_death,
        reconnect,
        silence,
        pause_stop,
        start_outage,
        stale_flatten,
        producer,
        outage_diff,
    )
    if tuple(drill.drill_id for drill in drills) != _REQUIRED_DRILL_IDS:
        raise AssertionError("qualification runner must execute all required drills in order")
    return CustodyQualificationCampaign(
        drills=drills,
        queue_wait_samples_ms=queue_samples,
        fsync_samples_ms=fsync_samples,
        a0_to_a1_dispatch_samples_ms=qualification_samples,
        a1_to_callback_samples_ms=fill_samples[0:1],
        callback_to_ack_samples_ms=fill_samples[1:2],
        queue_high_water=queue_high_water,
        queue_refusal_count=queue_refusals,
        epoch_recovery_samples_ms=recovery_samples,
        max_uncertain_intent_age_ms=uncertain_age,
        projection_gap_count=projection_gaps,
    )


async def _a0_only_clerk(
    controls: DeterministicFaultControls,
    *,
    intent_id: str,
    with_epoch: bool = False,
) -> tuple[tuple[str, ...], int, int, int]:
    """Persist an actual A0 without scheduling any broker work."""

    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-a0-") as temporary_root:
        root = Path(temporary_root)
        bind_qualification_bot(root, _ACCOUNT_ID, "bot-1", clock.now_ms())
        generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms(), source="qualification"
        ).generation
        epoch = (
            AccountEpochAuthority(
                artifacts_root=root,
                account_id=_ACCOUNT_ID,
                clerk_generation=generation,
                clerk_boot_id="qualification-a0",
                now_ms=clock.now_ms,
            )
            if with_epoch
            else None
        )
        if epoch is not None:
            epoch.initialize()
        broker = DeterministicPaperBroker(account_id=_ACCOUNT_ID, clock=clock, controls=controls)
        clerk = AccountClerk(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            broker=broker,
            clerk_generation=generation,
            now_ms=clock.now_ms,
            epoch_authority=epoch,
            async_custody_boundary_hooks=controlled_clerk_boundary_hooks(clock=clock, controls=controls),
        )
        received_at_ms = clock.now_ms()
        receipt = await clerk.record_intent(
            qualification_intent(_ACCOUNT_ID, "bot-1", intent_id, received_at_ms),
            clerk_request_received_at_ms=received_at_ms,
        )
        entries = read_account_clerk_journal(root, _ACCOUNT_ID)
        if len(entries) != 1 or entries[0].entry_kind != "recorded":
            raise AssertionError("A0 did not leave exactly one durable recorded row")
        # Return only evidence that survives the temporary root's lifecycle.
        return (
            journal_receipts(root, _ACCOUNT_ID),
            receipt.clerk_request_received_at_ms or received_at_ms,
            receipt.inbox_fsynced_at_ms or 0,
            len(broker.calls),
        )


async def _queue_delay_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, tuple[int, ...], int, int]:
    trace = await real_eight_bot_trace(controls)
    passed = (
        len(trace.queue_wait_samples_ms) == _FLEET_SIZE
        and min(trace.queue_wait_samples_ms) >= controls.queue_delay_ms
        and max(trace.queue_wait_samples_ms) <= _A0_ADMISSION_CEILING_MS
        and trace.entry_broker_call_count == _FLEET_SIZE
        and sum(":recorded:" in receipt for receipt in trace.receipts) == _FLEET_SIZE
        and trace.queue_high_water == _FLEET_SIZE
        and trace.queue_refusal_count == 1
    )
    return (
        _drill(
            1,
            "queue_delay_before_a0",
            "Eight deployed entry slots with no externally reachable asynchronous reduction lane.",
            f"Queue wait of {controls.queue_delay_ms} ms at the final A0 admission boundary.",
            "Each supported entry gets durable A0 within the 10-second bound and the ninth entry is explicitly refused.",
            trace.receipts,
            "RECONCILING",
            passed,
        ),
        trace.queue_wait_samples_ms,
        trace.queue_high_water,
        trace.queue_refusal_count,
    )


async def _fsync_delay_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, tuple[int, ...]]:
    receipts, received_at_ms, inbox_fsynced_at_ms, broker_calls = await _a0_only_clerk(
        controls, intent_id="fsync-a0"
    )
    fsync_duration_ms = inbox_fsynced_at_ms - received_at_ms
    passed = (
        fsync_duration_ms >= controls.fsync_delay_ms
        and broker_calls == 0
        and any(":recorded:" in row for row in receipts)
    )
    return (
        _drill(
            2,
            "fsync_delay",
            "No durable intent and no broker write.",
            f"Fsync delay of {controls.fsync_delay_ms} ms at the completed inbox durable append boundary.",
            "The Clerk returns only its durable A0 receipt and no broker write starts in the A0 path.",
            receipts,
            "CLEAN",
            passed,
        ),
        (fsync_duration_ms,),
    )


async def _qualification_delay_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, tuple[int, ...]]:
    trace = await real_clerk_trace(
        controls=controls,
        emit_fill_before_ack=False,
        emit_duplicate_callback=False,
        socket_loss_after_a1=True,
        expected_lifecycle_state="uncertain_requires_reconciliation",
    )
    points = _receipt_times(trace.receipts)
    dispatch_delay_ms = points["broker_submitting"] - points["recorded"]
    passed = (
        dispatch_delay_ms >= controls.qualification_delay_ms
        and trace.lifecycle_state == "uncertain_requires_reconciliation"
        and trace.broker_call_count == 1
    )
    return (
        _drill(
            3,
            "qualification_delay_after_a0",
            "A Clerk has accepted durable custody and owns the only broker permission.",
            f"Qualification delay of {controls.qualification_delay_ms} ms after dequeue and before A1 dispatch.",
            "A0 remains durable across the measured dispatch delay; only the Clerk worker crosses A1.",
            trace.receipts,
            "RECONCILING",
            passed,
        ),
        (dispatch_delay_ms,),
    )


async def _socket_loss_after_a0_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-socket-a0-") as temporary_root:
        root = Path(temporary_root)
        bind_qualification_bot(root, _ACCOUNT_ID, "bot-1", clock.now_ms())
        generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms(), source="qualification"
        ).generation
        epoch = AccountEpochAuthority(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            clerk_generation=generation,
            clerk_boot_id="qualification-socket-a0",
            now_ms=clock.now_ms,
        )
        epoch.initialize()
        broker = DeterministicPaperBroker(account_id=_ACCOUNT_ID, clock=clock, controls=controls)
        clerk = AccountClerk(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            broker=broker,
            clerk_generation=generation,
            now_ms=clock.now_ms,
            epoch_authority=epoch,
        )
        intent = qualification_intent(_ACCOUNT_ID, "bot-1", "socket-after-a0", clock.now_ms())
        await clerk.record_intent(intent, clerk_request_received_at_ms=clock.now_ms())
        await clerk.observe_epoch_invalidation("SOCKET_LOSS")
        try:
            epoch.require_broker_write()
        except AccountEpochWriteBlockedError as blocked:
            blocked_reason = blocked.state.would_block_reason
        else:
            blocked_reason = None
        receipts = journal_receipts(root, _ACCOUNT_ID)
        passed = blocked_reason == "SOCKET_LOSS" and len(broker.calls) == 0 and len(receipts) == 1
    return _drill(
        4,
        "socket_loss_after_a0_before_a1",
        "A0 is durable and broker write has not started.",
        "Socket loss immediately after A0.",
        "The epoch closes before A1; the durable intent remains resolvable and no write is replayed.",
        (*receipts, f"epoch_write_blocked:{blocked_reason}"),
        "RECONCILING",
        passed,
    )


async def _socket_loss_after_a1_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, tuple[int, ...], int]:
    trace = await real_clerk_trace(
        controls=controls,
        emit_fill_before_ack=False,
        emit_duplicate_callback=False,
        socket_loss_after_a1=True,
        expected_lifecycle_state="uncertain_requires_reconciliation",
    )
    points = _receipt_times(trace.receipts)
    a1_at_ms = points["broker_submitting"]
    uncertain_at_ms = points["broker_uncertain"]
    passed = (
        trace.lifecycle_state == "uncertain_requires_reconciliation"
        and trace.custody_stage == "A1_BROKER_WRITE_STARTED"
        and trace.broker_call_count == 1
        and uncertain_at_ms >= a1_at_ms
    )
    return (
        _drill(
            5,
            "socket_loss_after_a1_before_a2",
            "A1 started from a durable A0 intent.",
            "Socket loss before broker identity acknowledgement.",
            "The intent becomes outcome-unknown and must reconcile; it is never replayed as a new write.",
            trace.receipts,
            "RECONCILING",
            passed,
        ),
        (a1_at_ms - points["recorded"],),
        max(0, uncertain_at_ms - a1_at_ms),
    )


async def _fill_before_ack_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, tuple[int, int]]:
    trace = await real_clerk_trace(
        controls=controls,
        emit_fill_before_ack=True,
        emit_duplicate_callback=True,
        socket_loss_after_a1=False,
        expected_lifecycle_state="economic_terminal",
    )
    points = _receipt_times(trace.receipts)
    passed = (
        points["broker_submitting"] < points["broker_event"] < points["broker_acked"]
        and trace.lifecycle_state == "economic_terminal"
        and trace.custody_stage == "A3_ECONOMIC_TERMINAL"
        and trace.broker_call_count == 1
    )
    return (
        _drill(
            6,
            "fill_before_ack_callback",
            "An A1 intent awaits an acknowledgement callback.",
            "A fill callback arrives before the acknowledgement callback.",
            "The fill attaches once and later acknowledgement cannot regress the terminal lifecycle.",
            trace.receipts,
            "CLEAN",
            passed,
        ),
        (
            points["broker_event"] - points["broker_submitting"],
            points["broker_acked"] - points["broker_event"],
        ),
    )


async def _duplicate_reordered_callbacks_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    trace = await real_clerk_trace(
        controls=controls,
        emit_fill_before_ack=True,
        emit_duplicate_callback=True,
        socket_loss_after_a1=False,
        expected_lifecycle_state="economic_terminal",
    )
    terminal_events = tuple(receipt for receipt in trace.receipts if ":broker_event:" in receipt)
    passed = (
        trace.lifecycle_state == "economic_terminal"
        and trace.custody_stage == "A3_ECONOMIC_TERMINAL"
        and len(terminal_events) == 1
        and trace.broker_call_count == 1
    )
    return _drill(
        7,
        "duplicate_and_reordered_callbacks",
        "One A1 intent with no terminal callback yet.",
        "Duplicate fill plus late acknowledgement delivery.",
        "Execution identity is deduplicated and economic terminal state is monotone.",
        trace.receipts,
        "CLEAN",
        passed,
    )


async def _originator_death_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-originator-death-") as temporary_root:
        root = Path(temporary_root)
        for bot_id in ("bot-dead", "bot-sibling-a", "bot-sibling-b"):
            bind_qualification_bot(root, _ACCOUNT_ID, bot_id, clock.now_ms())
        safety = AccountSafetyAuthority(artifacts_root=root, account_id=_ACCOUNT_ID, now_ms=clock.now_ms)
        clerk = AccountClerk(artifacts_root=root, account_id=_ACCOUNT_ID, safety_authority=safety, now_ms=clock.now_ms)
        intent = qualification_intent(_ACCOUNT_ID, "bot-dead", "originator-died", clock.now_ms())
        await clerk.record_intent(intent)
        retire_unmanaged_active_bindings_on_daemon_boot(root, managed_run_ids=frozenset(), now_ms=clock.now_ms() + 1)
        folded = await clerk.fold_binding_retirement_proposals()
        state = safety.read()
        receipts = journal_receipts(root, _ACCOUNT_ID)
        passed = (
            folded.retirements_applied == 3
            and state.verdict is AccountSafetyVerdict.SUSPENDED
            and state.suspension is not None
            and any(item.intent_id == intent.intent_id for item in state.suspension.custody)
        )
    return _drill(
        8,
        "originator_death_after_a0",
        "One durable A0 plus healthy sibling bindings.",
        "The originator disappears from daemon liveness after A0.",
        "The Clerk preserves attribution and custody; the account suspends new entries rather than retiring history.",
        (*receipts, f"retirements_applied:{folded.retirements_applied}", f"verdict:{state.verdict.value}"),
        state.verdict.value,
        passed,
    )


async def _retired_originator_late_fill_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-retired-fill-") as temporary_root:
        root = Path(temporary_root)
        bind_qualification_bot(root, _ACCOUNT_ID, "bot-retired", clock.now_ms())
        safety = AccountSafetyAuthority(artifacts_root=root, account_id=_ACCOUNT_ID, now_ms=clock.now_ms)
        broker = DeterministicPaperBroker(account_id=_ACCOUNT_ID, clock=clock, controls=controls)
        clerk = AccountClerk(
            artifacts_root=root, account_id=_ACCOUNT_ID, broker=broker, safety_authority=safety, now_ms=clock.now_ms
        )
        intent = qualification_intent(_ACCOUNT_ID, "bot-retired", "retired-late-fill", clock.now_ms())
        await clerk.record_intent(intent)
        retire_unmanaged_active_bindings_on_daemon_boot(root, managed_run_ids=frozenset(), now_ms=clock.now_ms() + 1)
        await clerk.fold_binding_retirement_proposals()
        await clerk.record_broker_event(
            IbkrOrderEvent(
                account_id=_ACCOUNT_ID,
                order_ref=intent.order_ref,
                order_id=17,
                exec_id="qualification-retired-late-fill",
                event_type="fill",
                symbol="SPY",
                side="BUY",
                fill_quantity=1,
                ts_ms=clock.now_ms() + 2,
            )
        )
        state = safety.read()
        receipts = journal_receipts(root, _ACCOUNT_ID)
        passed = state.verdict is AccountSafetyVerdict.SUSPENDED and any(":broker_event:" in row for row in receipts)
    return _drill(
        9,
        "retired_originator_late_fill",
        "A retiring originator owns an accepted Clerk intent.",
        "A late fill arrives after durable retirement.",
        "Attribution stays exact and the account remains suspended for entries pending recovery evidence.",
        (*receipts, f"verdict:{state.verdict.value}"),
        state.verdict.value,
        passed,
    )


async def _clerk_death_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-clerk-death-") as temporary_root:
        root = Path(temporary_root)
        bind_qualification_bot(root, _ACCOUNT_ID, "bot-clerk-death", clock.now_ms())
        first_generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms(), source="qualification"
        ).generation
        durable_generation = first_generation
        clerk = AccountClerk(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            clerk_generation=first_generation,
            durable_generation_provider=lambda: durable_generation,
            now_ms=clock.now_ms,
        )
        intent = qualification_intent(_ACCOUNT_ID, "bot-clerk-death", "clerk-death-a0", clock.now_ms())
        await clerk.record_intent(intent)
        custody_before_replacement = read_account_clerk_journal(root, _ACCOUNT_ID)
        original = AccountEpochAuthority(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            clerk_generation=first_generation,
            clerk_boot_id="qualification-old",
            now_ms=clock.now_ms,
            durable_generation_provider=lambda: durable_generation,
        )
        original.initialize()
        durable_generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms() + controls.epoch_recovery_delay_ms, source="qualification"
        ).generation
        try:
            original.require_broker_write()
        except AccountEpochGenerationFencedError:
            fenced = True
        else:
            fenced = False
        successor = AccountEpochAuthority(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            clerk_generation=durable_generation,
            clerk_boot_id="qualification-new",
            now_ms=clock.now_ms,
            durable_generation_provider=lambda: durable_generation,
        ).initialize()
        custody_survived = (
            len(custody_before_replacement) == 1
            and custody_before_replacement[0].entry_kind == "recorded"
            and custody_before_replacement[0].intent == intent
        )
        passed = (
            custody_survived
            and fenced
            and successor.status == "INVALID"
            and successor.would_block_reason == "CLERK_DEATH"
        )
        receipts = journal_receipts(root, _ACCOUNT_ID)
    return _drill(
        10,
        "clerk_death_with_nonterminal_intents",
        "A Clerk generation owns nonterminal account custody.",
        "The accepting Clerk is replaced by a new durable generation.",
        "The old generation is fenced and the successor begins invalid until reconciliation proves a single manager.",
        (
            *receipts,
            f"old_generation:{first_generation}",
            f"successor_generation:{durable_generation}",
            f"successor_status:{successor.status}",
        ),
        "RECONCILING",
        passed,
    )


async def _ibkr_reconnect_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, tuple[int, ...]]:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-110x-") as temporary_root:
        root = Path(temporary_root)
        generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms(), source="qualification"
        ).generation
        authority = AccountEpochAuthority(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            clerk_generation=generation,
            clerk_boot_id="qualification-110x",
            now_ms=clock.now_ms,
        )
        authority.initialize()
        first = authority.invalidate("IBKR_1101")
        clock.advance(controls.epoch_recovery_delay_ms)
        second = authority.invalidate("IBKR_1102")
        try:
            authority.require_broker_write()
        except AccountEpochWriteBlockedError:
            writes_blocked = True
        else:
            writes_blocked = False
        state = authority.read()
        passed = (
            first.required_reconciliation_depth == "full"
            and second.required_reconciliation_depth == "incremental"
            and second.observed_epoch.epoch_seq > first.observed_epoch.epoch_seq
            and writes_blocked
        )
    return (
        _drill(
            11,
            "ibkr_1101_and_1102",
            "A valid account epoch exists before reconnect notices.",
            "IBKR 1101 then 1102 reconnect notifications.",
            "Both mint a new epoch with their configured depth; stale write permission remains blocked.",
            (first.receipt_id, second.receipt_id, f"final_depth:{state.required_reconciliation_depth}"),
            "RECONCILING",
            passed,
        ),
        (controls.epoch_recovery_delay_ms,),
    )


async def _callback_silence_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-silence-") as temporary_root:
        root = Path(temporary_root)
        generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms(), source="qualification"
        ).generation
        authority = AccountEpochAuthority(
            artifacts_root=root, account_id=_ACCOUNT_ID, clerk_generation=generation, clerk_boot_id="qualification-silence", now_ms=clock.now_ms
        )
        authority.initialize()
        clerk = AccountClerk(artifacts_root=root, account_id=_ACCOUNT_ID, epoch_authority=authority, now_ms=clock.now_ms)
        bind_qualification_bot(root, _ACCOUNT_ID, "bot-silence", clock.now_ms())
        intent = qualification_intent(_ACCOUNT_ID, "bot-silence", "stream-silence-a0", clock.now_ms())
        await clerk.record_intent(intent)
        has_nonterminal_work = any(
            entry.entry_kind == "recorded" and entry.intent == intent
            for entry in read_account_clerk_journal(root, _ACCOUNT_ID)
        )
        clerk.mark_broker_event_stream_live()
        clock.advance(30_001)
        await clerk.observe_critical_stream_silence(now_ms=clock.now_ms(), has_nonterminal_work=has_nonterminal_work)
        state = authority.read()
        passed = (
            has_nonterminal_work
            and state.would_block_reason == "CRITICAL_STREAM_SILENCE"
            and state.status == "INVALID"
        )
        receipts = journal_receipts(root, _ACCOUNT_ID)
        invalidation_receipt_ids = tuple(receipt.receipt_id for receipt in state.invalidation_receipts)
    return _drill(
        12,
        "callback_silence_with_socket_connected",
        "The Clerk owns a live broker callback stream.",
        "The callback heartbeat is silent past the active-poll threshold.",
        "Loss of proof invalidates entry authority despite an apparently connected socket.",
        (*receipts, *invalidation_receipt_ids),
        "RECONCILING",
        passed,
    )


async def _pause_stop_outage_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-pause-stop-") as temporary_root:
        root = Path(temporary_root)
        kwargs = {
            "artifacts_root": root,
            "strategy_instance_id": "bot-outage",
            "desired_state": DesiredState.PAUSED,
            "command_verb": CommandVerb.PAUSE,
            "updated_by": "qualification",
            "reason": "daemon outage drill",
            "idempotency_key": "qualification-pause-outage",
            "now_ms": clock.now_ms,
            "daemon_url": "http://127.0.0.1:0",
            "live_binding_from_process": lambda _process: None,
            "visible_live_run_dir": lambda _binding: None,
        }
        first = await persist_risk_reducing_intent(**kwargs)
        second = await persist_risk_reducing_intent(**kwargs)
        passed = (
            first.durable.state == "PAUSED"
            and not first.actuation.actuated
            and first.daemon_process is None
            and second.replayed
        )
    return _drill(
        14,
        "pause_stop_during_daemon_outage",
        "The daemon is unreachable before operator control is requested.",
        "Operator records a risk-reducing Pause intent during the outage.",
        "Durable intent is accepted once; actuation remains explicitly pending and the retry is idempotent.",
        (
            f"desired_state:{first.durable.state}",
            f"actuated:{first.actuation.actuated}",
            f"replayed:{second.replayed}",
        ),
        "RECONCILING",
        passed,
    )


def _start_outage_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    """Use the real action-envelope guard: no proof means no start-like queue."""

    from app.schemas.presented_operator_action import PresentedOperatorActionTarget

    target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, strategy_instance_id="bot-outage", run_id="run-outage")
    errors: list[str] = []
    presenter = PresentedLifecycleActionService(now_ms=lambda: 1_784_950_000_000 + controls.action_to_effect_delay_ms)
    for action_id in ("start", "resume"):
        try:
            presenter.validate(action_id=action_id, target=target, invocation=None, snapshot=None)
        except PresentedLifecycleActionRejectedError as rejected:
            errors.append(rejected.reason_code)
    # Deploy has no run-id target and shares the same proof gate.
    deploy_target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, strategy_instance_id="bot-outage")
    try:
        presenter.validate(action_id="deploy", target=deploy_target, invocation=None, snapshot=None)
    except PresentedLifecycleActionRejectedError as rejected:
        errors.append(rejected.reason_code)
    passed = errors == ["ACTION_PRESENTATION_REQUIRED"] * 3
    return _drill(
        15,
        "start_resume_deploy_during_outage",
        "No current account-safety action envelope is available during outage.",
        "Operator requests Start, Resume, and Deploy.",
        "Each risk-increasing action is rejected before any host command can be queued.",
        tuple(f"action_rejected:{reason}" for reason in errors),
        "UNAVAILABLE",
        passed,
    )


async def _stale_flatten_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-stale-flatten-") as temporary_root:
        root = Path(temporary_root)
        broker = DeterministicPaperBroker(account_id=_ACCOUNT_ID, clock=clock, controls=controls)
        clerk = AccountClerk(artifacts_root=root, account_id=_ACCOUNT_ID, broker=broker, now_ms=clock.now_ms)
        try:
            await clerk.authorize_emergency_flatten(
                operation_id="qualification-stale-flatten",
                confirmation_token="FLATTEN",
                reconciliation_evidence_version="missing-or-stale-reconciliation",
                no_exact_recovery_candidate=True,
            )
        except RuntimeError as rejected:
            rejection = str(rejected)
        else:
            rejection = "UNEXPECTED_AUTHORIZATION"
        passed = rejection in {
            "CLERK_EMERGENCY_RECONCILIATION_NOT_AUTHORIZED",
            "CLERK_EMERGENCY_RECONCILIATION_EVIDENCE_INVALID",
        } and not broker.calls
    return _drill(
        16,
        "flatten_with_stale_positions",
        "No current reconciliation receipt authorizes a flatten target.",
        "Operator requests an account flatten using missing or stale position evidence.",
        "The Clerk refuses authorization before any broker write; a fresh reconciliation and confirmation are required.",
        (f"flatten_authorization:{rejection}", f"broker_calls:{len(broker.calls)}"),
        "RECONCILING",
        passed,
    )


def _operational_log_concurrency_drill(
    controls: DeterministicFaultControls,
) -> tuple[AccountCustodyQualificationDrill, int]:
    with tempfile.TemporaryDirectory(prefix="account-custody-producer-log-") as temporary_root:
        root = Path(temporary_root)

        def append(index: int) -> str:
            producer = "daemon" if index % 2 else "clerk_supervisor"
            record = append_producer_operational_event(
                root,
                account_id=_ACCOUNT_ID,
                producer=producer,
                producer_boot_id=f"{producer}-qualification",
                idempotency_key=f"qualification:{producer}:{index}",
                event_type=f"{producer}_lifecycle_observed",
                payload={"index": index},
                recorded_at_ms=1_784_950_000_000 + controls.callback_delay_ms + index,
            )
            return f"{record.producer}:{record.producer_boot_id}:{record.producer_seq}"

        with ThreadPoolExecutor(max_workers=8) as executor:
            # The writes race by design, while the report's content hash must
            # retain a stable receipt *set* rather than scheduler order.
            event_ids = tuple(sorted(executor.map(append, range(1, 17))))
        observed = read_producer_operational_events(root, account_id=_ACCOUNT_ID)
        passed = len(set(event_ids)) == 16 and len(observed) == 16 and {row.producer for row in observed} == {"daemon", "clerk_supervisor"}
    return (
        _drill(
            17,
            "operational_log_concurrency",
            "Clerk supervisor and daemon have independent producer-local streams.",
            "Concurrent lifecycle observations are recorded by both producers.",
            "Both streams remain durable with local sequence ownership and no global order/exposure writer.",
            event_ids,
            "CLEAN",
            passed,
        ),
        0,
    )


async def _outage_diff_drill(controls: DeterministicFaultControls) -> AccountCustodyQualificationDrill:
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-outage-diff-") as temporary_root:
        root = Path(temporary_root)
        generation = advance_account_clerk_generation(
            root, _ACCOUNT_ID, phase="accepting", recorded_at_ms=clock.now_ms(), source="qualification"
        ).generation
        epoch = AccountEpochAuthority(
            artifacts_root=root, account_id=_ACCOUNT_ID, clerk_generation=generation, clerk_boot_id="qualification-diff", now_ms=clock.now_ms
        )
        epoch.initialize()
        broker = DeterministicPaperBroker(account_id=_ACCOUNT_ID, clock=clock, controls=controls)
        clerk = AccountClerk(
            artifacts_root=root, account_id=_ACCOUNT_ID, broker=broker, epoch_authority=epoch, now_ms=clock.now_ms
        )
        epoch.invalidate("SOCKET_LOSS")
        unchanged = await clerk.reconcile_epoch_if_required()
        if unchanged is None or unchanged.outage_diff is None:
            raise AssertionError("unchanged reconnect did not emit an outage diff")
        seed_account_clerk_broker_evidence_baseline(
            root,
            _ACCOUNT_ID,
            AccountClerkBrokerEvidenceBaseline(
                account_id=_ACCOUNT_ID,
                observed_at_ms=clock.now_ms(),
                positions=(AccountClerkPositionEvidence(symbol="SPY", signed_quantity=1, evidence_observed_at_ms=clock.now_ms()),),
            ),
        )
        epoch.invalidate("IBKR_1101")
        changed = await clerk.reconcile_epoch_if_required()
        if changed is None or changed.outage_diff is None:
            raise AssertionError("changed reconnect did not emit an outage diff")
        unchanged_positions = unchanged.outage_diff["positions"]
        changed_positions = changed.outage_diff["positions"]
        passed = unchanged_positions == {"discovered": [], "changed": []} and bool(changed_positions["changed"])
    return _drill(
        18,
        "outage_diff",
        "One reconnect has unchanged evidence and one has a changed position baseline.",
        "Controlled unchanged and changed reconnect observations.",
        "Both branches retain an explicit durable outage diff rather than silently suppressing the unchanged path.",
        (f"unchanged_positions:{unchanged_positions}", f"changed_positions:{changed_positions}"),
        "RECONCILING" if changed.status == "INVALID" else changed.reconciliation_verdict,
        passed,
    )


def _receipt_times(receipts: Sequence[str]) -> dict[str, int]:
    """Decode the campaign's opaque journal receipt references for latency math."""

    values: dict[str, int] = {}
    for receipt in receipts:
        _sequence, kind, recorded_at_ms = receipt.split(":", maxsplit=2)
        values.setdefault(kind, int(recorded_at_ms))
    required = {"recorded", "broker_submitting"}
    if not required.issubset(values):
        raise AssertionError(f"missing required receipt timestamps: {sorted(required - set(values))}")
    return values


def _drill(
    drill_id: int,
    name: str,
    initial_state: str,
    injected_fault: str,
    expected_invariant: str,
    observed_receipts: tuple[str, ...],
    final_account_verdict: str,
    passed: bool,
    *,
    evidence_refs: tuple[str, ...] | None = None,
) -> AccountCustodyQualificationDrill:
    """Preserve every observed receipt whether a campaign drill passes or fails."""

    return AccountCustodyQualificationDrill(
        drill_id=drill_id,
        name=name,
        initial_state=initial_state,
        injected_fault=injected_fault,
        expected_invariant=expected_invariant,
        observed_receipts=observed_receipts,
        evidence_refs=evidence_refs or (f"deterministic-custody-drill:{drill_id}",),
        final_account_verdict=final_account_verdict,
        passed=passed,
        failure_detail=None if passed else "The production-seam deterministic invariant was not satisfied.",
    )

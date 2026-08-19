"""Production-seam deterministic drills for the custody qualification report.

The campaign runs the same Clerk, epoch, safety, lifecycle and producer-log
code used by paper operation.  Only the gateway transport, temporary artifact
root, and clock are controlled.  It is deliberately not a parallel custody
state machine.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable, Sequence
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
from app.engine.live.account_custody_topology import supported_account_custody_config
from app.engine.live.account_epoch import (
    AccountEpochAuthority,
    AccountEpochGenerationFencedError,
    AccountEpochWriteBlockedError,
)
from app.engine.live.account_registry import (
    index_account_instance_bindings,
    read_account_instance_registry,
    retire_unmanaged_active_bindings_on_daemon_boot,
)
from app.engine.live.account_safety import AccountSafetyAuthority, AccountSafetyVerdict
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
    queue_high_water: int | None
    queue_refusal_count: int | None
    epoch_recovery_samples_ms: tuple[int, ...]
    max_uncertain_intent_age_ms: int | None
    projection_gap_count: int | None


def run_deterministic_custody_campaign(controls: DeterministicFaultControls) -> CustodyQualificationCampaign:
    """Execute all 17 backend production-seam drills in canonical order."""

    return asyncio.run(_run_campaign(controls))


async def _run_campaign(controls: DeterministicFaultControls) -> CustodyQualificationCampaign:
    queue, queue_samples, queue_high_water, queue_refusals = await _run_isolated(
        drill_id=1,
        name="queue_delay_before_a0",
        action=lambda: _queue_delay_drill(controls),
        on_failure=lambda drill: (drill, (), None, None),
    )
    fsync, fsync_samples = await _run_isolated(
        drill_id=2,
        name="fsync_delay",
        action=lambda: _fsync_delay_drill(controls),
        on_failure=lambda drill: (drill, ()),
    )
    qualification, qualification_samples = await _run_isolated(
        drill_id=3,
        name="qualification_delay_after_a0",
        action=lambda: _qualification_delay_drill(controls),
        on_failure=lambda drill: (drill, ()),
    )
    after_a0 = await _run_isolated(
        drill_id=4,
        name="socket_loss_after_a0_before_a1",
        action=lambda: _socket_loss_after_a0_drill(controls),
        on_failure=lambda drill: drill,
    )
    after_a1, _after_a1_samples, uncertain_age = await _run_isolated(
        drill_id=5,
        name="socket_loss_after_a1_before_a2",
        action=lambda: _socket_loss_after_a1_drill(controls),
        on_failure=lambda drill: (drill, (), None),
    )
    fill, fill_samples = await _run_isolated(
        drill_id=6,
        name="fill_before_ack_callback",
        action=lambda: _fill_before_ack_drill(controls),
        on_failure=lambda drill: (drill, ()),
    )
    duplicate = await _run_isolated(
        drill_id=7,
        name="duplicate_and_reordered_callbacks",
        action=lambda: _duplicate_reordered_callbacks_drill(controls),
        on_failure=lambda drill: drill,
    )
    originator_death = await _run_isolated(
        drill_id=8,
        name="originator_death_after_a0",
        action=lambda: _originator_death_drill(controls),
        on_failure=lambda drill: drill,
    )
    retired_fill = await _run_isolated(
        drill_id=9,
        name="retired_originator_late_fill",
        action=lambda: _retired_originator_late_fill_drill(controls),
        on_failure=lambda drill: drill,
    )
    clerk_death = await _run_isolated(
        drill_id=10,
        name="clerk_death_with_nonterminal_intents",
        action=lambda: _clerk_death_drill(controls),
        on_failure=lambda drill: drill,
    )
    reconnect, recovery_samples = await _run_isolated(
        drill_id=11,
        name="ibkr_1101_and_1102",
        action=lambda: _ibkr_reconnect_drill(controls),
        on_failure=lambda drill: (drill, ()),
    )
    silence = await _run_isolated(
        drill_id=12,
        name="callback_silence_with_socket_connected",
        action=lambda: _callback_silence_drill(controls),
        on_failure=lambda drill: drill,
    )
    stale_flatten = await _run_isolated(
        drill_id=16,
        name="flatten_with_stale_positions",
        action=lambda: _stale_flatten_drill(controls),
        on_failure=lambda drill: drill,
    )
    producer, projection_gaps = _run_isolated_sync(
        drill_id=17,
        name="operational_log_concurrency",
        action=lambda: _operational_log_concurrency_drill(controls),
        on_failure=lambda drill: (drill, None),
    )
    outage_diff = await _run_isolated(
        drill_id=18,
        name="outage_diff",
        action=lambda: _outage_diff_drill(controls),
        on_failure=lambda drill: drill,
    )
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


async def _run_isolated[DrillResult](
    *,
    drill_id: int,
    name: str,
    action: Callable[[], Awaitable[DrillResult]],
    on_failure: Callable[[AccountCustodyQualificationDrill], DrillResult],
) -> DrillResult:
    """Archive one unexpected drill failure without abandoning later evidence."""

    try:
        return await action()
    except Exception as exc:
        return on_failure(_campaign_failure_drill(drill_id=drill_id, name=name, error=exc))


def _run_isolated_sync[DrillResult](
    *,
    drill_id: int,
    name: str,
    action: Callable[[], DrillResult],
    on_failure: Callable[[AccountCustodyQualificationDrill], DrillResult],
) -> DrillResult:
    """Apply the same failed-campaign receipt policy to synchronous drills."""

    try:
        return action()
    except Exception as exc:
        return on_failure(_campaign_failure_drill(drill_id=drill_id, name=name, error=exc))


def _campaign_failure_drill(
    *,
    drill_id: int,
    name: str,
    error: Exception,
) -> AccountCustodyQualificationDrill:
    """Turn a runner fault into durable-reportable failed qualification evidence."""

    detail = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
    return AccountCustodyQualificationDrill(
        drill_id=drill_id,
        name=name,
        initial_state="Campaign execution did not complete this production-seam drill.",
        injected_fault="Unexpected deterministic campaign execution failure.",
        expected_invariant="A failed drill must remain visible and leave the remaining campaign runnable.",
        observed_receipts=(f"campaign_exception:{type(error).__name__}",),
        evidence_refs=(f"deterministic-custody-drill:{drill_id}",),
        final_account_verdict="UNAVAILABLE",
        passed=False,
        failure_detail=detail[:1_024],
    )


async def _a0_only_clerk(
    controls: DeterministicFaultControls,
    *,
    intent_id: str,
    with_epoch: bool = False,
) -> tuple[tuple[str, ...], int, int, int, int]:
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
            receipt.clerk_intake_admitted_at_ms or 0,
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
        and all(
            queue_wait_ms + fsync_ms <= _A0_ADMISSION_CEILING_MS
            for queue_wait_ms, fsync_ms in zip(trace.queue_wait_samples_ms, trace.fsync_samples_ms, strict=True)
        )
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
            "Each supported entry reaches durable inbox fsync within the 10-second A0 bound and the ninth entry is explicitly refused.",
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
    receipts, _received_at_ms, intake_admitted_at_ms, inbox_fsynced_at_ms, broker_calls = await _a0_only_clerk(
        controls, intent_id="fsync-a0"
    )
    fsync_duration_ms = inbox_fsynced_at_ms - intake_admitted_at_ms
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
    points = _receipt_times(trace.receipts, required=frozenset({"recorded", "broker_submitting"}))
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
        before_a1_reached = asyncio.Event()
        release_before_a1 = asyncio.Event()

        async def hold_before_a1(_intent: object, _lane: object) -> None:
            before_a1_reached.set()
            await release_before_a1.wait()

        clerk = AccountClerk(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            broker=broker,
            clerk_generation=generation,
            now_ms=clock.now_ms,
            epoch_authority=epoch,
            async_custody_config=supported_account_custody_config(),
            async_custody_boundary_hooks=controlled_clerk_boundary_hooks(
                clock=clock,
                controls=controls,
                before_a1_dispatch=hold_before_a1,
            ),
        )
        intent = qualification_intent(_ACCOUNT_ID, "bot-1", "socket-after-a0", clock.now_ms())
        await clerk.start_async_custody()
        try:
            await clerk.submit_async_custody(intent, clerk_request_received_at_ms=clock.now_ms())
            await asyncio.wait_for(before_a1_reached.wait(), timeout=2.0)
            await clerk.observe_epoch_invalidation("SOCKET_LOSS")
            try:
                epoch.require_broker_write()
            except AccountEpochWriteBlockedError as blocked:
                blocked_reason = blocked.state.would_block_reason
            else:
                blocked_reason = None
            release_before_a1.set()
            deadline = asyncio.get_running_loop().time() + 2.0
            status = None
            while asyncio.get_running_loop().time() < deadline:
                status = await clerk.async_custody_status(intent)
                if status is not None and status.lifecycle_state == "submission_hold":
                    break
                await asyncio.sleep(0.001)
            receipts = journal_receipts(root, _ACCOUNT_ID)
            passed = (
                blocked_reason == "SOCKET_LOSS"
                and status is not None
                and status.lifecycle_state == "submission_hold"
                and len(broker.calls) == 0
                and any(":recorded:" in receipt for receipt in receipts)
                and any(":custody_submission_hold:" in receipt for receipt in receipts)
            )
        finally:
            release_before_a1.set()
            await clerk.stop_async_custody()
    return _drill(
        4,
        "socket_loss_after_a0_before_a1",
        "A0 is durable and broker write has not started.",
        "Socket loss immediately after A0.",
        "The epoch closes before A1; the queued durable intent becomes an explicit submission hold and no write is replayed.",
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
    points = _receipt_times(
        trace.receipts,
        required=frozenset({"recorded", "broker_submitting", "broker_uncertain"}),
    )
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
    points = _receipt_times(
        trace.receipts,
        required=frozenset({"broker_submitting", "broker_event", "broker_acked"}),
    )
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
        retire_unmanaged_active_bindings_on_daemon_boot(
            root,
            managed_run_ids=frozenset({"qualification-bot-sibling-a", "qualification-bot-sibling-b"}),
            now_ms=clock.now_ms() + 1,
        )
        folded = await clerk.fold_binding_retirement_proposals()
        state = safety.read()
        receipts = journal_receipts(root, _ACCOUNT_ID)
        latest_bindings = index_account_instance_bindings(
            read_account_instance_registry(root, _ACCOUNT_ID),
            account_id=_ACCOUNT_ID,
        ).latest_by_instance
        passed = (
            folded.retirements_applied == 1
            and state.verdict is AccountSafetyVerdict.SUSPENDED
            and state.suspension is not None
            and any(item.intent_id == intent.intent_id for item in state.suspension.custody)
            and latest_bindings["bot-dead"].lifecycle_state == "RETIRED"
            and latest_bindings["bot-sibling-a"].lifecycle_state == "ACTIVE"
            and latest_bindings["bot-sibling-b"].lifecycle_state == "ACTIVE"
        )
    return _drill(
        8,
        "originator_death_after_a0",
        "One durable A0 plus healthy sibling bindings.",
        "The originator disappears from daemon liveness after A0.",
        "The Clerk preserves attribution and custody, retires only the missing originator, and keeps healthy siblings active.",
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
        broker = DeterministicPaperBroker(account_id=_ACCOUNT_ID, clock=clock, controls=controls)
        clerk = AccountClerk(
            artifacts_root=root,
            account_id=_ACCOUNT_ID,
            broker=broker,
            clerk_generation=generation,
            epoch_authority=authority,
            now_ms=clock.now_ms,
        )
        first = authority.invalidate("IBKR_1101")
        clock.advance(controls.epoch_recovery_delay_ms)
        first_recovered = await clerk.reconcile_epoch_if_required()
        second = authority.invalidate("IBKR_1102")
        clock.advance(controls.epoch_recovery_delay_ms)
        second_recovered = await clerk.reconcile_epoch_if_required()
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
            and first_recovered is not None
            and first_recovered.status == "CLEAN"
            and second_recovered is not None
            and second_recovered.status == "CLEAN"
            and second_recovered.last_reconciliation_id == second.reconciliation_id
            and not writes_blocked
            and state.status == "CLEAN"
        )
        recovery_duration_ms = (second_recovered.updated_at_ms if second_recovered is not None else 0) - second.recorded_at_ms
    return (
        _drill(
            11,
            "ibkr_1101_and_1102",
            "A valid account epoch exists before reconnect notices.",
            "IBKR 1101 then 1102 reconnect notifications.",
            "Both mint a new epoch with their configured depth, then a Clerk-owned reconciliation restores broker-write permission only after fresh proof.",
            (
                first.receipt_id,
                second.receipt_id,
                f"first_recovery:{first_recovered.status if first_recovered is not None else 'MISSING'}",
                f"second_recovery:{second_recovered.status if second_recovered is not None else 'MISSING'}",
                f"final_depth:{state.required_reconciliation_depth}",
            ),
            state.status,
            passed,
        ),
        (recovery_duration_ms,),
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
        sequences_by_producer: dict[str, list[int]] = {}
        for row in observed:
            sequences_by_producer.setdefault(row.producer, []).append(row.producer_seq)
        projection_gap_count = sum(
            _producer_sequence_gap_count(sequences)
            for sequences in sequences_by_producer.values()
        )
        passed = (
            len(set(event_ids)) == 16
            and len(observed) == 16
            and set(sequences_by_producer) == {"daemon", "clerk_supervisor"}
            and projection_gap_count == 0
        )
    return (
        _drill(
            17,
            "operational_log_concurrency",
            "Clerk supervisor and daemon have independent producer-local streams.",
            "Concurrent lifecycle observations are recorded by both producers.",
            "Both streams remain durable with verified gap-free local sequence ownership and no global order/exposure writer.",
            (*event_ids, f"producer_sequence_gaps:{projection_gap_count}"),
            "CLEAN",
            passed,
        ),
        projection_gap_count,
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


def _receipt_times(receipts: Sequence[str], *, required: frozenset[str]) -> dict[str, int]:
    """Decode the campaign's opaque journal receipt references for latency math."""

    values: dict[str, int] = {}
    for receipt in receipts:
        _sequence, kind, recorded_at_ms = receipt.split(":", maxsplit=2)
        values.setdefault(kind, int(recorded_at_ms))
    if not required.issubset(values):
        raise AssertionError(f"missing required receipt timestamps: {sorted(required - set(values))}")
    return values


def _producer_sequence_gap_count(sequences: Sequence[int]) -> int:
    """Count missing or duplicated numbers within one producer-owned stream."""

    if not sequences:
        return 0
    observed = set(sequences)
    expected = set(range(1, max(observed) + 1))
    return len(expected - observed) + (len(sequences) - len(observed))


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

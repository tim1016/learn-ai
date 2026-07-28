"""Deterministic account-epoch persistence and provenance contracts."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.live.account_clerk as account_clerk_module
import app.engine.live.account_epoch as account_epoch_module
import app.engine.live.account_epoch_observer as account_epoch_observer_module
from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import read_account_events
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_clerk_journal import AccountClerkJournal
from app.engine.live.account_clerk_rpc import AccountClerkRpcServer
from app.engine.live.account_epoch import (
    AccountEpoch,
    AccountEpochAuthority,
    AccountEpochGenerationFencedError,
    EpochInvalidationTrigger,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent

ACCOUNT_ID = "DU1234567"


def _authority(root: Path, *, generation: int, boot_id: str, now_ms: int = 1_780_000_000_000) -> AccountEpochAuthority:
    return AccountEpochAuthority(
        artifacts_root=root,
        account_id=ACCOUNT_ID,
        clerk_generation=generation,
        clerk_boot_id=boot_id,
        now_ms=lambda: now_ms,
    )


def _intent() -> AccountOwnerSubmitIntent:
    return AccountOwnerSubmitIntent(
        trace_id="trace-1",
        account_id=ACCOUNT_ID,
        strategy_instance_id="strategy-1",
        run_id="run-1",
        bot_order_namespace="bot:strategy-1",
        intent_id="intent-1",
        order_ref="bot:strategy-1:intent-1",
        intent_kind="ENTRY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=1_780_000_000_000,
    )


def test_first_invalidation_advances_once_and_repeated_trigger_is_one_receipt(tmp_path: Path) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    assert authority.initialize().current_epoch == AccountEpoch(clerk_boot_id="boot-a", epoch_seq=1)

    first = authority.invalidate("IBKR_1100")
    repeated = authority.invalidate("IBKR_1100")

    assert first.observed_epoch == AccountEpoch(clerk_boot_id="boot-a", epoch_seq=2)
    assert repeated.observed_epoch == first.observed_epoch
    assert repeated == first
    state = authority.read()
    assert state.status == "INVALID"
    assert state.would_block_reason == "IBKR_1100"
    assert state.required_reconciliation_depth == "full"
    assert state.invalidation_triggers == ("IBKR_1100",)
    epoch_events = [
        event for event in read_account_events(tmp_path, ACCOUNT_ID) if event["event_type"] == "account_epoch_invalidated"
    ]
    assert len(epoch_events) == 1
    assert epoch_events[0]["receipt_id"] == first.receipt_id


def test_1101_and_1102_remain_distinct_new_epoch_candidates(tmp_path: Path) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()

    recovered_with_lost_data = authority.invalidate("IBKR_1101")
    recovered_with_maintained_data = authority.invalidate("IBKR_1102")

    assert recovered_with_lost_data.observed_epoch.epoch_seq == 2
    assert recovered_with_maintained_data.observed_epoch == recovered_with_lost_data.observed_epoch
    assert recovered_with_lost_data.required_reconciliation_depth == "full"
    assert recovered_with_maintained_data.required_reconciliation_depth == "incremental"
    state = authority.read()
    assert state.required_reconciliation_depth == "full"
    assert state.invalidation_triggers == ("IBKR_1101", "IBKR_1102")


def test_new_generation_preserves_invalid_history_and_stale_generation_cannot_advance(tmp_path: Path) -> None:
    original = _authority(tmp_path, generation=4, boot_id="boot-a")
    original.initialize()
    original.invalidate("SOCKET_LOSS")

    successor = _authority(tmp_path, generation=5, boot_id="boot-b")
    state = successor.initialize()

    assert state.current_epoch == AccountEpoch(clerk_boot_id="boot-b", epoch_seq=3)
    assert state.status == "INVALID"
    assert state.would_block_reason == "CLERK_DEATH"
    with pytest.raises(AccountEpochGenerationFencedError):
        original.invalidate("GENERATION_FENCED")
    assert successor.read().current_epoch == AccountEpoch(clerk_boot_id="boot-b", epoch_seq=3)


def test_durable_generation_fences_old_epoch_writer_before_successor_boot(tmp_path: Path) -> None:
    durable_generation = 4
    original = AccountEpochAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        clerk_generation=4,
        clerk_boot_id="boot-a",
        now_ms=lambda: 1_780_000_000_000,
        durable_generation_provider=lambda: durable_generation,
    )
    original.initialize()

    durable_generation = 5
    with pytest.raises(AccountEpochGenerationFencedError):
        original.invalidate("GENERATION_FENCED")

    successor = AccountEpochAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        clerk_generation=5,
        clerk_boot_id="boot-b",
        now_ms=lambda: 1_780_000_000_001,
        durable_generation_provider=lambda: durable_generation,
    )
    state = successor.initialize()
    assert state.current_epoch == AccountEpoch(clerk_boot_id="boot-b", epoch_seq=2)
    assert state.invalidation_triggers == ("CLERK_DEATH", "GENERATION_FENCED")
    assert [receipt.trigger for receipt in state.invalidation_receipts] == [
        "CLERK_DEATH",
        "GENERATION_FENCED",
    ]


def test_epoch_receipt_replays_after_crash_between_state_and_event_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()

    with monkeypatch.context() as patched:
        patched.setattr(
            account_epoch_module,
            "append_account_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated event-log outage")),
        )
        authority.invalidate("SOCKET_LOSS")
        assert authority.read().receipt_projection_status == "PENDING"

    restarted = _authority(tmp_path, generation=4, boot_id="boot-a")
    state = restarted.read()
    assert [receipt.trigger for receipt in state.invalidation_receipts] == ["SOCKET_LOSS"]
    assert state.receipt_projection_status == "CURRENT"
    events = [
        event for event in read_account_events(tmp_path, ACCOUNT_ID) if event["event_type"] == "account_epoch_invalidated"
    ]
    assert [event["trigger"] for event in events] == ["SOCKET_LOSS"]


@pytest.mark.parametrize("trigger", ["SOCKET_LOSS", "CRITICAL_STREAM_SILENCE", "ACTIVE_POLL_FAILED"])
def test_each_remaining_required_trigger_persists_a_shadow_receipt(
    tmp_path: Path,
    trigger: EpochInvalidationTrigger,
) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()

    receipt = authority.invalidate(trigger)

    assert authority.read().status == "INVALID"
    events = [
        event for event in read_account_events(tmp_path, ACCOUNT_ID) if event["event_type"] == "account_epoch_invalidated"
    ]
    assert len(events) == 1
    assert events[0]["receipt_id"] == receipt.receipt_id
    assert events[0]["trigger"] == trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_trigger"),
    [(504, "SOCKET_LOSS"), (1100, "IBKR_1100"), (1101, "IBKR_1101"), (1102, "IBKR_1102")],
)
async def test_clerk_owned_signal_supervisor_maps_ibkr_codes_to_epoch_receipts(
    code: int,
    expected_trigger: EpochInvalidationTrigger,
) -> None:
    stop = asyncio.Event()

    class _Client:
        last_ibkr_code = code
        connectivity_lost_count = 0

        @staticmethod
        def is_connected() -> bool:
            return True

    class _Clerk:
        _account_id = ACCOUNT_ID

        async def observe_epoch_invalidation(self, trigger: EpochInvalidationTrigger) -> None:
            assert trigger == expected_trigger
            stop.set()

        async def reconciliation_snapshot(self) -> list[object]:
            raise AssertionError("the code receipt should stop before an active-poll scan")

    await account_clerk_module._supervise_account_epoch_signals(
        client=_Client(),
        clerk=_Clerk(),  # type: ignore[arg-type]
        stop=stop,
        signal_poll_s=0.01,
        active_poll_interval_s=60.0,
    )


def test_journal_keeps_origin_and_later_callback_observed_epoch(tmp_path: Path) -> None:
    origin = AccountEpoch(clerk_boot_id="boot-a", epoch_seq=1)
    observed = AccountEpoch(clerk_boot_id="boot-b", epoch_seq=2)
    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT_ID, now_ms=lambda: 1_780_000_000_000)
    intent = _intent()

    journal.record_intent(
        intent,
        validate_intent=lambda _: None,
        origin_epoch=origin,
        observed_epoch=origin,
        reconciliation_id="epoch:DU1234567:boot-a:1",
    )
    journal.append_broker_submitting(
        intent,
        observed_epoch=observed,
        reconciliation_id="epoch:DU1234567:boot-b:2",
    )

    entries = journal.snapshot()
    assert entries[0].origin_epoch == origin
    assert entries[0].observed_epoch == origin
    assert entries[1].origin_epoch == origin
    assert entries[1].observed_epoch == observed
    assert entries[1].reconciliation_id == "epoch:DU1234567:boot-b:2"


def test_broker_ack_remains_nonterminal_for_active_epoch_polling(tmp_path: Path) -> None:
    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT_ID, now_ms=lambda: 1_780_000_000_000)
    intent = _intent()
    journal.record_intent(intent, validate_intent=lambda _: None)
    journal.append_broker_ack(intent, SimpleNamespace(order_id=42, perm_id=43, exec_id=None))

    assert account_epoch_observer_module.has_nonterminal_epoch_work(journal.snapshot())


@pytest.mark.asyncio
async def test_epoch_invalidation_cannot_overtake_an_admitted_callback_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback is labelled by the horizon that held the Clerk intake lock."""

    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        epoch_authority=authority,
    )
    intent = _intent()
    epoch = authority.current_epoch()
    clerk._journal.record_intent(
        intent,
        validate_intent=lambda _: None,
        origin_epoch=epoch,
        observed_epoch=epoch,
    )
    entered = threading.Event()
    release = threading.Event()
    original = clerk._journal.record_broker_event

    def delayed_record(*args: object, **kwargs: object):
        entered.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(clerk._journal, "record_broker_event", delayed_record)
    callback_task = asyncio.create_task(
        clerk.record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT_ID,
                order_id=42,
                event_type="status",
                order_ref=intent.order_ref,
                symbol="AMD",
                side="BUY",
                ts_ms=1_780_000_000_001,
            )
        )
    )
    await asyncio.to_thread(entered.wait)
    invalidation_task = asyncio.create_task(clerk.observe_epoch_invalidation("SOCKET_LOSS"))
    await asyncio.sleep(0)
    assert authority.read().status == "CLEAN"

    release.set()
    await callback_task
    await invalidation_task

    entries = clerk._journal.snapshot()
    assert entries[-1].observed_epoch == AccountEpoch(clerk_boot_id="boot-a", epoch_seq=1)
    assert authority.read().current_epoch == AccountEpoch(clerk_boot_id="boot-a", epoch_seq=2)


@pytest.mark.asyncio
async def test_live_but_silent_stream_invalidates_only_while_nonterminal_work_exists(tmp_path: Path) -> None:
    now_ms = 1_780_000_000_000
    authority = AccountEpochAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        clerk_generation=4,
        clerk_boot_id="boot-a",
        now_ms=lambda: now_ms,
    )
    authority.initialize()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT_ID, epoch_authority=authority, now_ms=lambda: now_ms)
    clerk.mark_broker_event_stream_live()

    now_ms += 31_000
    await clerk.observe_critical_stream_silence(now_ms=now_ms, has_nonterminal_work=False)
    assert authority.read().status == "CLEAN"
    await clerk.observe_critical_stream_silence(now_ms=now_ms, has_nonterminal_work=True)
    assert authority.read().invalidation_triggers == ("CRITICAL_STREAM_SILENCE",)


@pytest.mark.asyncio
async def test_signal_supervisor_records_socket_loss_and_failed_active_poll(tmp_path: Path) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT_ID, epoch_authority=authority)
    stop = asyncio.Event()
    observed: list[EpochInvalidationTrigger] = []
    original_observe = clerk.observe_epoch_invalidation

    async def observe_then_stop(trigger: EpochInvalidationTrigger) -> None:
        await original_observe(trigger)
        observed.append(trigger)
        stop.set()

    clerk.observe_epoch_invalidation = observe_then_stop  # type: ignore[method-assign]

    class _SocketLossClient:
        connection_lost = False

        def __init__(self) -> None:
            self._states = iter((True, False))

        def is_connected(self) -> bool:
            return next(self._states)

    await account_clerk_module._supervise_account_epoch_signals(
        client=_SocketLossClient(),
        clerk=clerk,
        stop=stop,
        signal_poll_s=0.01,
        active_poll_interval_s=60.0,
    )
    assert observed == ["SOCKET_LOSS"]

    authority = _authority(tmp_path / "poll", generation=4, boot_id="boot-a")
    authority.initialize()
    clerk = AccountClerk(artifacts_root=tmp_path / "poll", account_id=ACCOUNT_ID, epoch_authority=authority)
    clerk._journal.record_intent(_intent(), validate_intent=lambda _: None)
    stop = asyncio.Event()
    observed = []
    original_observe = clerk.observe_epoch_invalidation

    async def observe_poll_then_stop(trigger: EpochInvalidationTrigger) -> None:
        await original_observe(trigger)
        observed.append(trigger)
        stop.set()

    clerk.observe_epoch_invalidation = observe_poll_then_stop  # type: ignore[method-assign]

    class _FailingProbeClient:
        connection_lost = False
        last_ibkr_code: int | None = None

        @staticmethod
        def is_connected() -> bool:
            return True

        @staticmethod
        async def probe(*, timeout_s: float) -> None:
            assert timeout_s == 4.0
            raise TimeoutError("simulated active poll failure")

    await account_clerk_module._supervise_account_epoch_signals(
        client=_FailingProbeClient(),
        clerk=clerk,
        stop=stop,
        signal_poll_s=0.01,
        active_poll_interval_s=0.0,
    )
    assert observed == ["ACTIVE_POLL_FAILED"]


@pytest.mark.asyncio
async def test_epoch_health_rpc_returns_clerk_owned_shadow_state(tmp_path: Path) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()
    authority.invalidate("IBKR_1100")
    server = AccountClerkRpcServer(
        AccountClerk(
            artifacts_root=tmp_path,
            account_id=ACCOUNT_ID,
            clerk_generation=4,
            epoch_authority=authority,
        )
    )

    response = await server._dispatch({"operation": "epoch_health_v1"})

    assert response.payload["epoch_health"] == authority.read().model_dump(mode="json")


def test_legacy_journal_epoch_provenance_is_explicitly_unknown(tmp_path: Path) -> None:
    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT_ID, now_ms=lambda: 1_780_000_000_000)
    journal.record_intent(_intent(), validate_intent=lambda _: None)

    entry = journal.snapshot()[0]
    assert entry.origin_epoch is None
    assert entry.observed_epoch is None
    assert entry.reconciliation_id is None

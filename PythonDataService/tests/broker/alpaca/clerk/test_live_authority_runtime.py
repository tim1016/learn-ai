"""The real-money Live Account Authority, composed by the real selector (ADR 0059 D1, D11 — slice 7).

Every test runs ``select_active_clerk_runtime`` against a live broker double:
the wiring under test is composition — which authority a live boot selects
once an activation exists, which ports it binds, which gates it installs, and
whether an armed instance's ENTER reaches the real trade port through all
three admissions. No wall clock: the repository is pinned to ``NOW_MS``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import live_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_shadow_clerk_authority,
    primary_custody_world,
    reset_alpaca_clerk_for_testing,
    select_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_SEAL_CHANGED,
    LIVE_ARMING_UNOBSERVED,
    LIVE_MODE_DISAGREEMENT,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_authority import (
    LIVE_CONTROL_UNAUTHENTICATED,
    select_live_clerk_runtime,
)
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_MISSING
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests.broker.alpaca.clerk.live_arming_fixtures import record_sealed_binding
from tests.broker.alpaca.clerk.live_authority_fixtures import (
    LIVE_SID,
    _RecordingLiveBroker,
    compose_live,
    live_activation,
    pinned_repository,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    TEST_ENVELOPE_VALUES,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import RUN_ID, _binding
from tests.broker.alpaca.clerk.test_active_authority import _ActivationStore
from tests.broker.alpaca.clerk.test_shadow_broker import _retain
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import BAR_CLOSE, DECISION_MINUTE, NOW_MS


@pytest.fixture(autouse=True)
def _no_primary() -> Iterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


@pytest.fixture()
def live_state_root(tmp_path: Path) -> Path:
    return tmp_path / "runner"


@pytest.fixture()
async def live_runtime(
    tmp_path: Path, live_state_root: Path
) -> AsyncIterator[tuple[ActiveClerkRuntime, _RecordingLiveBroker]]:
    broker = _RecordingLiveBroker(now_ms=NOW_MS)
    runtime = await compose_live(tmp_path, broker, now_ms=NOW_MS, live_state_root=live_state_root)
    assert runtime.authority_kind == "sqlite", runtime.startup_failure
    try:
        yield runtime, broker
    finally:
        await runtime.close()


@pytest.fixture()
async def registered_live_bot(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    tmp_path: Path,
    live_state_root: Path,
) -> AsyncIterator[tuple[RetainedSourceBar, str]]:
    """One instance registered with the live Clerk, sealed on disk on the live id, and its decision bar."""
    runtime, _broker = live_runtime
    assert runtime.clerk is not None
    seal = record_sealed_binding(live_state_root, strategy_instance_id=LIVE_SID, sealed_account_id=LIVE_ACCT)
    await runtime.clerk.register_strategy_run(
        _binding(use_rth=True).model_copy(
            update={"strategy_instance_id": LIVE_SID, "sealed_account_id": LIVE_ACCT}
        )
    )
    bars = SourceBarLedger(
        artifacts_root=tmp_path, account_id=live_evidence_account_id_for_strategy(LIVE_SID)
    )
    try:
        yield _retain(bars, minute=DECISION_MINUTE, close=BAR_CLOSE), seal.bot_configuration_hash
    finally:
        bars.close()


def _arm(tmp_path: Path, seal_hash: str, *, configured_signal_hash: str = "b" * 64) -> LiveArmingRecord:
    record = LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=LIVE_SID,
        seal_hash=seal_hash,
        configured_signal_hash=configured_signal_hash,
        shadow_receipt_sha256="e" * 64,
        envelope=TEST_ENVELOPE_VALUES,
        armed_at_ms=NOW_MS - 60_000,
        max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
    )
    LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).append(record)
    return record


async def _enter(runtime: ActiveClerkRuntime, bar: RetainedSourceBar, *, decision_id: str = "d1") -> Any:
    assert runtime.clerk is not None
    binding = _binding(use_rth=True)
    return await runtime.clerk.execute_for_instance(
        strategy_instance_id=LIVE_SID,
        run_id=RUN_ID,
        decision_id=decision_id,
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=1,
        use_rth=True,
        retained_source_bar=bar,
    )


async def test_a_live_account_with_no_activation_still_boots_the_shadow_authority(tmp_path: Path) -> None:
    """R1: absent the cutover's record, every live boot is exactly what slice 4 built."""
    await activate_shadow_clerk_authority(live_account_id=LIVE_ACCT, artifacts_root=tmp_path)
    broker = _LiveBroker(now_ms=NOW_MS)
    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        repository_opener=pinned_repository(NOW_MS),
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )
    try:
        assert runtime.authority_kind == "shadow", runtime.startup_failure
    finally:
        await runtime.close()


async def test_an_activated_live_account_boots_the_real_live_authority_with_both_gates(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
) -> None:
    """R1, R4: the same sqlite Clerk over a real account, in the real_live world, with the real trade port."""
    runtime, _broker = live_runtime
    assert runtime.selected_account_authority_kind == "real_live"
    assert runtime.selected_account_id == LIVE_ACCT
    assert runtime.clerk is not None and runtime.envelope_sync is not None
    assert runtime.clerk.account_mode == "live"
    assert runtime.clerk.live_envelope is runtime.envelope_sync.envelope
    assert runtime.clerk.live_envelope.custody_is_simulated is False
    assert runtime.clerk.live_arming is not None
    assert isinstance(runtime.evidence_sink, SqliteTradeUpdateEvidenceSink)
    set_active_clerk_runtime(runtime)
    assert primary_custody_world() == "real_live"


async def test_an_open_control_plane_installs_no_live_authority(
    tmp_path: Path, live_state_root: Path
) -> None:
    """R14: DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=true and a live account never meet."""
    runtime = await compose_live(
        tmp_path,
        _LiveBroker(now_ms=NOW_MS),
        now_ms=NOW_MS,
        live_state_root=live_state_root,
        control_unauthenticated=True,
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == LIVE_CONTROL_UNAUTHENTICATED
    assert "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL" in runtime.startup_failure.recovery


async def test_a_live_authority_without_envelope_values_is_unavailable(
    tmp_path: Path, live_state_root: Path
) -> None:
    runtime = await compose_live(
        tmp_path,
        _LiveBroker(now_ms=NOW_MS),
        now_ms=NOW_MS,
        live_state_root=live_state_root,
        live_envelope_values=None,
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == LIVE_ENVELOPE_MISSING


async def test_an_activation_naming_another_account_is_a_mode_disagreement(tmp_path: Path) -> None:
    """R2: the activation leg is looked up by the observed id; it can never grant a different one."""
    broker = _LiveBroker(now_ms=NOW_MS)
    activation = live_activation(account_id="9OTHER0002")
    runtime = await select_live_clerk_runtime(
        account=await broker.get_account(),
        activation=activation,
        activation_store=_ActivationStore(activation),
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        repository_opener=pinned_repository(NOW_MS),
        startup_recovery_timeout_s=5.0,
        execution_lease_wait_timeout_s=0.0,
        execution_lease_retry_interval_s=0.1,
        stream_health_gate=None,
        roster_symbols=None,
        live_envelope_values=TEST_ENVELOPE_VALUES,
        live_state_root=None,
        control_unauthenticated=False,
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == LIVE_MODE_DISAGREEMENT


async def test_a_paper_mode_account_reaching_the_live_selector_is_a_mode_disagreement(
    tmp_path: Path,
) -> None:
    """R2: the live selector's first mode-agreement leg refuses a non-live broker-observed account."""
    broker = _LiveBroker(now_ms=NOW_MS)
    account = (await broker.get_account()).model_copy(update={"account_mode": "paper"})
    activation = live_activation()
    runtime = await select_live_clerk_runtime(
        account=account,
        activation=activation,
        activation_store=_ActivationStore(activation),
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        repository_opener=pinned_repository(NOW_MS),
        startup_recovery_timeout_s=5.0,
        execution_lease_wait_timeout_s=0.0,
        execution_lease_retry_interval_s=0.1,
        stream_health_gate=None,
        roster_symbols=None,
        live_envelope_values=TEST_ENVELOPE_VALUES,
        live_state_root=None,
        control_unauthenticated=False,
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == LIVE_MODE_DISAGREEMENT


async def test_an_unreadable_activation_record_refuses_the_live_boot(
    tmp_path: Path, live_state_root: Path
) -> None:
    """Finding 1: the live fork's refusal for a tampered cutover record, through the real selector."""
    broker = _RecordingLiveBroker(now_ms=NOW_MS)
    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(None, invalid=True),
        repository_opener=pinned_repository(NOW_MS),
        live_envelope_values=TEST_ENVELOPE_VALUES,
        live_state_root=lambda: live_state_root,
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "ACTIVATION_RECORD_INVALID"
    assert broker.submissions == []


async def test_before_the_first_tick_every_live_enter_is_unobserved(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    registered_live_bot: tuple[RetainedSourceBar, str],
) -> None:
    runtime, broker = live_runtime
    bar, _seal = registered_live_bot
    receipt = await _enter(runtime, bar)
    assert receipt.state == "rejected"
    assert receipt.explanation.startswith(f"{LIVE_ARMING_UNOBSERVED}:"), receipt.explanation
    assert broker.submissions == []


async def test_an_unarmed_live_instance_is_refused_required_after_the_tick(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    registered_live_bot: tuple[RetainedSourceBar, str],
) -> None:
    runtime, broker = live_runtime
    bar, _seal = registered_live_bot
    assert runtime.envelope_sync is not None
    await runtime.envelope_sync.tick()
    receipt = await _enter(runtime, bar)
    assert receipt.state == "rejected"
    assert receipt.explanation.startswith(f"{LIVE_ARMING_REQUIRED}:"), receipt.explanation
    assert broker.submissions == []


async def test_an_armed_live_instances_enter_passes_all_three_gates_and_reaches_the_real_trade_port(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    registered_live_bot: tuple[RetainedSourceBar, str],
    tmp_path: Path,
) -> None:
    """Consequence 7: the slice after which a real order is possible — and this is that order."""
    runtime, broker = live_runtime
    bar, seal_hash = registered_live_bot
    _arm(tmp_path, seal_hash)
    assert runtime.envelope_sync is not None and runtime.sqlite_repository is not None
    await runtime.envelope_sync.tick()

    receipt = await _enter(runtime, bar)

    assert receipt.state != "rejected", receipt.explanation
    assert runtime.sqlite_repository.reserved_cash_usd(observed_at_ms=NOW_MS) > 0
    assert len(broker.submissions) == 1


async def test_a_changed_seal_disarms_the_live_instance_at_admission(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    registered_live_bot: tuple[RetainedSourceBar, str],
    tmp_path: Path,
) -> None:
    runtime, broker = live_runtime
    bar, _seal = registered_live_bot
    _arm(tmp_path, "f" * 64)
    assert runtime.envelope_sync is not None
    await runtime.envelope_sync.tick()
    receipt = await _enter(runtime, bar)
    assert receipt.state == "rejected"
    assert receipt.explanation.startswith(f"{LIVE_ARMING_SEAL_CHANGED}:"), receipt.explanation
    assert broker.submissions == []

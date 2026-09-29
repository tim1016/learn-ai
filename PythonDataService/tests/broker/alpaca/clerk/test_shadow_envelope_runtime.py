"""The composed shadow authority rides the live risk envelope (ADR 0059 D4).

Every test here composes the *real* selector -- ``activate_shadow_clerk_authority``
then ``select_active_clerk_runtime`` against a live broker whose ``submit`` and
``cancel`` raise -- exactly as ``tests/broker/v2panel/test_shadow_operator_surfaces``
does, because the wiring under test is composition: which authority builds an
envelope, which sync it hands the envelope to, and whether the facade passes it
into ``accept_enter``.

No wall clock: the shadow repository is opened on a clock pinned to the
decision bar's close, so the observation stamp, the freshness question and the
ET day the P&L spans are all the same fixed instant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import (
    shadow_evidence_account_id_for_strategy,
)
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_shadow_clerk_authority,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests.broker.alpaca.clerk.activation_fixtures import _ActivationStore
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    SHADOW_ACCT,
    TEST_ENVELOPE_VALUES,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import RUN_ID, SID, _binding
from tests.broker.alpaca.clerk.test_active_authority import _activation, _Broker
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, _retain

DECISION_MINUTE = 600  # 10:00 ET on a full NYSE session
BAR_CLOSE = "100"
# The Clerk stamps every fact from its repository clock; pinning it to the
# decision bar's close is what makes the observation, its freshness and the
# ET day window deterministic.
NOW_MS = et_minute_of_day_ms(DAY, DECISION_MINUTE) + 60_000


def _pinned_repository(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.open(
        account_id=account_id,
        artifacts_root=artifacts_root,
        clock=_TestClock(NOW_MS),
    )


async def _compose_shadow(
    tmp_path: Path, broker: _LiveBroker
) -> ActiveClerkRuntime:
    await activate_shadow_clerk_authority(
        live_account_id=LIVE_ACCT, artifacts_root=tmp_path
    )
    return await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        repository_opener=_pinned_repository,
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )


@pytest.fixture()
async def shadow_runtime(
    tmp_path: Path,
) -> AsyncIterator[tuple[ActiveClerkRuntime, _LiveBroker]]:
    """One composed shadow authority, envelope included."""
    broker = _LiveBroker(now_ms=NOW_MS)
    runtime = await _compose_shadow(tmp_path, broker)
    assert runtime.authority_kind == "shadow", runtime.startup_failure
    try:
        yield runtime, broker
    finally:
        await runtime.close()


@pytest.fixture()
async def registered_running_bot(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker], tmp_path: Path
) -> AsyncIterator[RetainedSourceBar]:
    """One registered, running instance and the decision bar its ENTER is priced at."""
    runtime, _broker = shadow_runtime
    assert runtime.clerk is not None
    await runtime.clerk.register_strategy_run(
        _binding(use_rth=True).model_copy(update={"sealed_account_id": SHADOW_ACCT})
    )
    bars = SourceBarLedger(
        artifacts_root=tmp_path,
        account_id=shadow_evidence_account_id_for_strategy(SID),
    )
    try:
        yield _retain(bars, minute=DECISION_MINUTE, close=BAR_CLOSE, provider="ibkr")
    finally:
        bars.close()


async def _enter(
    runtime: ActiveClerkRuntime,
    bar: RetainedSourceBar,
    *,
    quantity: int,
    decision_id: str = "d1",
) -> Any:
    """Drive one ENTER through the composed facade, exactly as a bot does."""
    assert runtime.clerk is not None
    binding = _binding(use_rth=True)
    return await runtime.clerk.execute_for_instance(
        strategy_instance_id=SID,
        run_id=RUN_ID,
        decision_id=decision_id,
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=quantity,
        use_rth=True,
        retained_source_bar=bar,
    )


async def _exit(
    runtime: ActiveClerkRuntime,
    bar: RetainedSourceBar,
    *,
    quantity: int,
    decision_id: str = "exit-1",
) -> Any:
    """Drive one EXIT through the same composed Shadow facade."""
    assert runtime.clerk is not None
    binding = _binding(use_rth=True)
    return await runtime.clerk.execute_for_instance(
        strategy_instance_id=SID,
        run_id=RUN_ID,
        decision_id=decision_id,
        purpose=EffectPurpose.EXIT,
        action_plan=binding.action_plan,
        quantity=quantity,
        use_rth=True,
        retained_source_bar=bar,
    )


async def test_a_shadow_authority_without_envelope_values_is_unavailable(
    tmp_path: Path,
) -> None:
    """Fail closed: no ALPACA_LIVE_* values, no authority on a real-money account."""
    await activate_shadow_clerk_authority(
        live_account_id=LIVE_ACCT, artifacts_root=tmp_path
    )
    broker = _LiveBroker(now_ms=NOW_MS)

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        repository_opener=_pinned_repository,
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.clerk is None
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "LIVE_ENVELOPE_MISSING"
    assert runtime.startup_failure.account_id == SHADOW_ACCT
    assert "ALPACA_LIVE_*" in runtime.startup_failure.recovery


async def test_the_composed_shadow_runtime_carries_a_simulated_custody_envelope_and_its_sync(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    """One envelope object, shared by the sync that observes it and the facade that admits against it."""
    runtime, _broker = shadow_runtime

    assert runtime.envelope_sync is not None
    assert runtime.clerk is not None
    assert runtime.clerk.live_envelope is runtime.envelope_sync.envelope
    # Shadow custody is simulated: the broker's cash never moves, so the
    # envelope subtracts what this Clerk's own fills would have spent.
    assert runtime.envelope_sync.envelope.custody_is_simulated is True
    assert runtime.envelope_sync.envelope.values == TEST_ENVELOPE_VALUES


async def test_an_unobserved_envelope_refuses_the_enter_as_a_rejected_receipt_not_an_exception(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
) -> None:
    """Withdrawing activation's observation prevents any new entry."""
    runtime, _broker = shadow_runtime
    runtime.envelope_sync.discard_observation()

    receipt = await _enter(runtime, registered_running_bot, quantity=1)

    assert receipt.state.value == "rejected"
    assert receipt.explanation.startswith("LIVE_ENVELOPE_UNOBSERVED:")


async def test_after_one_tick_the_cash_bound_admits_what_cash_covers_and_refuses_what_it_does_not(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
) -> None:
    """The decision bar's close is the price the envelope bounds the ENTER at."""
    runtime, broker = shadow_runtime
    # 100 shares at the bar's close is the whole account; a program ENTER's
    # quantity is capped at 100, so the bound is proved by moving the cash:
    # exactly 50 shares' notional plus their one-cent fee provision.
    broker.cash = 5_000.01
    assert runtime.envelope_sync is not None

    assert await runtime.envelope_sync.tick() == "observed"

    refused = await _enter(runtime, registered_running_bot, quantity=51)
    assert refused.state.value == "rejected"
    assert refused.explanation.startswith("LIVE_ENVELOPE_CASH_EXCEEDED:")
    # 51 × 100.00 plus the one-cent fee provision: the notional is priced at
    # the *decision bar's* close, which is the whole point of the
    # ``reference_price`` the facade now passes. A market ENTER with no price
    # is refused UNOBSERVED, so a wrong wiring here cannot hide behind a
    # plausible-looking refusal.
    assert "ENTER needs 5100.01 USD" in refused.explanation, refused.explanation

    admitted = await _enter(
        runtime, registered_running_bot, quantity=50, decision_id="d2"
    )
    assert admitted.state.value == "submitted", admitted.explanation


async def test_an_unjudgeable_tick_refuses_the_next_enter_end_to_end(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
) -> None:
    """R3 and R5, joined: unknown at the sync ⇒ UNOBSERVED at the ENTER seam.

    Both halves are pinned on their own -- the sync answers ``unknown`` and
    withdraws, and an unobserved gate refuses -- but the chain between them is
    the gate's contract, and a regression that broke the join would leave both
    halves green. This drives one composed runtime through the whole of it.
    """
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    # A judgeable tick first, so the refusal below cannot pass merely because
    # nothing was ever observed.
    assert await runtime.envelope_sync.tick() == "observed"
    admitted = await _enter(runtime, registered_running_bot, quantity=1)
    assert admitted.state.value == "submitted", admitted.explanation
    flattened = await _exit(runtime, registered_running_bot, quantity=1)
    assert flattened.state.value == "flat", flattened.explanation

    # Invalid reference cash withdraws the old observation immediately.
    broker.cash = float("nan")
    assert await runtime.envelope_sync.tick() == "read_failed"

    refused = await _enter(runtime, registered_running_bot, quantity=1, decision_id="d2")
    assert refused.state.value == "rejected"
    assert refused.explanation.startswith("LIVE_ENVELOPE_UNOBSERVED:"), refused.explanation


async def test_paper_installs_the_same_account_risk_observation_gate(
    tmp_path: Path,
) -> None:
    """#2543: Paper and Live share the account loss policy authority."""
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST", artifacts_root=tmp_path
    )
    runtime: ActiveClerkRuntime | None = None
    try:
        runtime = await select_active_clerk_runtime(
            read=broker,
            trade=broker,
            artifacts_root=tmp_path,
            activation_store=_ActivationStore(_activation()),
            repository_opener=lambda _account_id, _root: repo,
            live_envelope_values=TEST_ENVELOPE_VALUES,
        )

        assert runtime.authority_kind == "sqlite"
        assert runtime.envelope_sync is not None
        assert runtime.clerk is not None
        assert runtime.clerk.live_envelope is runtime.envelope_sync.envelope
    finally:
        # A composed runtime owns this handle and closes it; if the selector
        # never returned one, the repository was opened before it and its
        # execution lease would otherwise outlive the test.
        if runtime is None:
            repo.close()
        else:
            await runtime.close()


async def test_a_second_enter_inside_one_sync_interval_is_refused_by_attributed_exposure(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
) -> None:
    """The deterministic Shadow fill is attributed before another ENTER can be admitted."""
    runtime, broker = shadow_runtime
    broker.cash = 5_000.01  # 50 shares at 100.00 and their one-cent fee provision
    assert runtime.envelope_sync is not None
    assert await runtime.envelope_sync.tick() == "observed"

    first = await _enter(runtime, registered_running_bot, quantity=50)
    assert first.state.value == "submitted", first.explanation

    second = await _enter(runtime, registered_running_bot, quantity=50, decision_id="d2")
    assert second.state.value == "rejected"
    assert second.explanation.startswith("ATTRIBUTED_EXPOSURE_EXISTS:")


async def test_real_account_positions_cannot_create_a_shadow_hold(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    """PRD #2540 supersedes mixed-world P&L: real holdings are foreign."""
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    assert runtime.sqlite_repository is not None

    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.latest_observation().unrealized_pl_usd == 0.0

    broker.unrealized = -5_000.0
    broker.last_equity_known = False
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.latest_observation().unrealized_pl_usd == 0.0
    hold = runtime.sqlite_repository.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        strategy_instance_id=None,
    )
    assert hold is None



async def test_shadow_risk_ignores_retired_arming_bytes_and_never_records_sessions(shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker], tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger

    runtime, _ = shadow_runtime
    path = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("corrupt retired grant\n")
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.sealed is None
    assert path.read_text() == "corrupt retired grant\n"
    assert not tuple(tmp_path.rglob("shadow_sessions.jsonl"))


async def test_shadow_rollover_clearance_ignores_foreign_live_positions(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar, tmp_path: Path,
) -> None:
    from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy

    runtime, broker = shadow_runtime
    repo, sync = runtime.sqlite_repository, runtime.envelope_sync
    append_risk_policy(repo, policy=AccountRiskPolicy(1, .05, 50, "profile", 1, "owner", NOW_MS), expected_revision=0)
    assert await sync.tick() == "observed"
    entered = await _enter(runtime, registered_running_bot, quantity=1)
    assert entered.state.value == "submitted", entered.explanation
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=shadow_evidence_account_id_for_strategy(SID))
    try:
        loss_bar = _retain(bars, minute=DECISION_MINUTE + 1, close="40", provider="ibkr")
    finally:
        bars.close()
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    clock.advance(loss_bar.end_ms - clock())
    repo.revive_execution_lease()
    broker.now_ms = loss_bar.end_ms
    exited = await _exit(runtime, loss_bar, quantity=1)
    assert exited.state.value == "flat", exited.explanation
    assert await sync.tick() == "hold_raised"
    original = sync.risk_snapshot().hold
    assert original is not None and original.day_pnl_usd < -60
    clock.advance(NOW_MS + 86_400_000 - clock())
    repo.revive_execution_lease()
    broker.now_ms = repo.clock()
    broker.unrealized = -10_000  # A real position persists; Shadow is flat.
    assert await broker.list_positions()
    reading, quiet = await sync.observe_loss_clearance()
    assert reading.day_pnl.total_usd == pytest.approx(0, abs=1e-9, rel=0)
    assert quiet is not None and quiet.account_flat and quiet.broker_work_ended and quiet.intents_resolved
    assert sync.clear_observed_loss_hold(reading, quiet=quiet)[0] == "cleared"

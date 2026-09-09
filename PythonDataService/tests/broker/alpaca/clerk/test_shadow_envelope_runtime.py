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

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
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
from app.broker.alpaca.clerk.live_arming import LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    SHADOW_ACCT,
    TEST_ENVELOPE_VALUES,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import RUN_ID, SID, _binding
from tests.broker.alpaca.clerk.test_active_authority import (
    _activation,
    _ActivationStore,
    _Broker,
)
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
        clock=lambda: NOW_MS,
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
        yield _retain(bars, minute=DECISION_MINUTE, close=BAR_CLOSE)
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
    """Before the first tick nothing is observed, and an unobserved envelope admits nothing."""
    runtime, _broker = shadow_runtime

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
    # quantity is capped at 100, so the bound is proved by moving the cash.
    broker.cash = 5_000.0
    assert runtime.envelope_sync is not None

    assert await runtime.envelope_sync.tick() == "observed"

    refused = await _enter(runtime, registered_running_bot, quantity=51)
    assert refused.state.value == "rejected"
    assert refused.explanation.startswith("LIVE_ENVELOPE_CASH_EXCEEDED:")
    # 51 × 100.00: the notional is priced at the *decision bar's* close, which
    # is the whole point of the ``reference_price`` the facade now passes. A
    # market ENTER with no price is refused UNOBSERVED, so a wrong wiring here
    # cannot hide behind a plausible-looking refusal.
    assert "ENTER needs 5100.00 USD" in refused.explanation, refused.explanation

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

    # No previous-close equity: there is no loss limit to judge against, so
    # the account is unjudgeable and the observation is withdrawn (plan R3).
    broker.last_equity_known = False
    assert await runtime.envelope_sync.tick() == "unknown"

    refused = await _enter(runtime, registered_running_bot, quantity=1, decision_id="d2")
    assert refused.state.value == "rejected"
    assert refused.explanation.startswith("LIVE_ENVELOPE_UNOBSERVED:"), refused.explanation


async def test_a_paper_authority_carries_no_envelope_even_when_values_are_offered(
    tmp_path: Path,
) -> None:
    """The envelope is the live world's; a paper authority never composes one."""
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
        assert runtime.envelope_sync is None
        assert runtime.clerk is not None
        assert runtime.clerk.live_envelope is None
    finally:
        # A composed runtime owns this handle and closes it; if the selector
        # never returned one, the repository was opened before it and its
        # execution lease would otherwise outlive the test.
        if runtime is None:
            repo.close()
        else:
            await runtime.close()


async def test_a_second_enter_inside_one_sync_interval_is_refused_by_the_first_ones_unrecorded_fill(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
) -> None:
    """The shadow book fills at submit; until the sweep records that fill the reservation must carry it."""
    runtime, broker = shadow_runtime
    broker.cash = 5_000.0
    assert runtime.envelope_sync is not None
    assert await runtime.envelope_sync.tick() == "observed"

    first = await _enter(runtime, registered_running_bot, quantity=50)
    assert first.state.value == "submitted", first.explanation

    second = await _enter(runtime, registered_running_bot, quantity=50, decision_id="d2")
    assert second.state.value == "rejected"
    assert second.explanation.startswith("LIVE_ENVELOPE_CASH_EXCEEDED:")
    assert "5000.00 USD reserved by working entries" in second.explanation, second.explanation


async def test_the_shadow_envelope_observes_the_live_accounts_positions_not_the_synthesized_book(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    """Day P&L under shadow carries the live account's unrealized P&L (plan residual), so a live loss raises the hold."""
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    assert runtime.sqlite_repository is not None

    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.latest_observation().unrealized_pl_usd == 0.0

    broker.unrealized = -5_000.0  # limit = min(0.05 × 100,000, 5,000) = 5,000; P&L −5,000 breaches
    assert await runtime.envelope_sync.tick() == "hold_raised"
    hold = runtime.sqlite_repository.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        strategy_instance_id=None,
    )
    assert hold is not None


def _arm(
    artifacts_root: Path,
    *,
    envelope: object = TEST_ENVELOPE_VALUES,
    armed_at_ms: int = NOW_MS,
) -> LiveArmingRecord:
    """Append one arming record straight to the ledger.

    The ceremony that mints these has its own tests; what is under test here is
    what the *runtime* does with a record that exists.
    """
    record = LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=SID,
        seal_hash="a" * 64,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="c" * 64,
        envelope=envelope,  # type: ignore[arg-type]
        armed_at_ms=armed_at_ms,
        max_sessions=20,
    )
    LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).append(record)
    return record


async def test_the_gate_is_unsealed_until_an_arming_record_exists(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker], tmp_path: Path
) -> None:
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    assert runtime.envelope_sync.envelope.sealed is None
    assert runtime.envelope_sync.envelope.agreement == "unsealed"

    _arm(tmp_path)

    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.sealed == TEST_ENVELOPE_VALUES
    assert runtime.envelope_sync.envelope.agreement == "agreed"


async def test_an_environment_change_after_arming_refuses_every_enter_end_to_end(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
    tmp_path: Path,
) -> None:
    """R10's whole point: a live ENTER is admitted against the *sealed* values.

    Both halves are pinned on their own -- the sync seals from the ledger, and a
    disagreeing gate refuses -- but the chain between them is what an operator
    is trusting, and a regression that broke the join would leave both halves
    green.
    """
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    assert await runtime.envelope_sync.tick() == "observed"

    admitted = await _enter(runtime, registered_running_bot, quantity=1)
    assert admitted.state.value == "submitted", admitted.explanation

    # The operator edited ALPACA_LIVE_LOSS_USD and restarted nothing.
    runtime.envelope_sync.envelope.values = replace(TEST_ENVELOPE_VALUES, loss_usd=4_000.0)
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.agreement == "disagreed"

    refused = await _enter(runtime, registered_running_bot, quantity=1, decision_id="d2")
    assert refused.state.value == "rejected"
    assert refused.explanation.startswith("LIVE_ENVELOPE_DISAGREEMENT:"), refused.explanation
    assert "re-arm" in refused.explanation


async def test_a_re_arm_seals_the_new_environment_and_admits_again(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
    tmp_path: Path,
) -> None:
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    tightened = replace(TEST_ENVELOPE_VALUES, loss_usd=4_000.0)
    runtime.envelope_sync.envelope.values = tightened
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.agreement == "disagreed"

    _arm(tmp_path, envelope=tightened, armed_at_ms=NOW_MS + 1)
    assert await runtime.envelope_sync.tick() == "observed"

    assert runtime.envelope_sync.envelope.sealed == tightened
    assert runtime.envelope_sync.envelope.agreement == "agreed"
    admitted = await _enter(runtime, registered_running_bot, quantity=1)
    assert admitted.state.value == "submitted", admitted.explanation


async def test_an_unreadable_arming_ledger_unseals_the_gate_and_is_logged_once(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail closed and loud: a ledger nobody can read seals nothing."""
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.sealed is not None

    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"max_sessions":20', '"max_sessions":90'),
        encoding="utf-8",
    )
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert await runtime.envelope_sync.tick() == "observed"
        assert await runtime.envelope_sync.tick() == "observed"

    assert runtime.envelope_sync.envelope.sealed is None
    assert runtime.envelope_sync.envelope.agreement == "unsealed"
    invalid = [
        record for record in caplog.records if getattr(record, "action", None) == "live_arming_ledger_invalid"
    ]
    assert len(invalid) == 1, "the fault is logged once per transition, not four times a minute"
    assert invalid[0].exc_info is not None, "the traceback names which row will not verify"


async def test_the_seal_transition_log_names_both_the_custody_and_the_live_account(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Under shadow those are two different accounts, and an operator reads both."""
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert await runtime.envelope_sync.tick() == "observed"

    (sealed,) = [
        record for record in caplog.records if getattr(record, "action", None) == "live_envelope_sealed"
    ]
    assert sealed.account_id == SHADOW_ACCT  # type: ignore[attr-defined]
    assert sealed.live_account_id == LIVE_ACCT  # type: ignore[attr-defined]


async def test_a_repaired_ledger_reseals_and_the_two_dedup_flags_are_independent(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ledger fault and the seal transition dedup on their own questions.

    One flag must not swallow the other: a ledger that goes bad and is then put
    back has to log the fault once *and* both envelope transitions.
    """
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    assert await runtime.envelope_sync.tick() == "observed"
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    readable = ledger.path.read_text(encoding="utf-8")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        ledger.path.write_text(readable.replace('"max_sessions":20', '"max_sessions":90'), encoding="utf-8")
        assert await runtime.envelope_sync.tick() == "observed"
        assert await runtime.envelope_sync.tick() == "observed"
        assert runtime.envelope_sync.envelope.sealed is None
        ledger.path.write_text(readable, encoding="utf-8")
        assert await runtime.envelope_sync.tick() == "observed"

    assert runtime.envelope_sync.envelope.sealed == TEST_ENVELOPE_VALUES
    actions = [getattr(record, "action", None) for record in caplog.records]
    assert actions.count("live_arming_ledger_invalid") == 1
    assert actions.count("live_envelope_unsealed") == 1
    assert actions.count("live_envelope_sealed") == 1
    assert actions.index("live_envelope_unsealed") < actions.index("live_envelope_sealed")

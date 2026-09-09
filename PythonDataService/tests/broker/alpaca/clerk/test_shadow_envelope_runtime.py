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

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
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
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import BrokerAccountSnapshot, BrokerPosition
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import RUN_ID, SID, _binding
from tests.broker.alpaca.clerk.test_active_authority import (
    _activation,
    _ActivationStore,
    _Broker,
)
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, _retain

LIVE_ACCT = "9LIVE0001"
SHADOW_ACCT = "shadow:9LIVE0001"
DECISION_MINUTE = 600  # 10:00 ET on a full NYSE session
BAR_CLOSE = "100"
# The Clerk stamps every fact from its repository clock; pinning it to the
# decision bar's close is what makes the observation, its freshness and the
# ET day window deterministic.
NOW_MS = et_minute_of_day_ms(DAY, DECISION_MINUTE) + 60_000


class _LiveBroker:
    """The live account: real reads the test steers, writes that must never be reached."""

    broker_id = "alpaca"

    def __init__(self, *, cash: float = 100_000.0) -> None:
        self.cash = cash

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=LIVE_ACCT,
            account_mode="live",
            account_status="ACTIVE",
            currency="USD",
            cash=self.cash,
            equity=self.cash,
            buying_power=self.cash,
            portfolio_value=self.cash,
            long_market_value=0.0,
            short_market_value=0.0,
            # A judgeable account: with no last equity the sync answers
            # "unknown" and withdraws the observation instead of publishing.
            last_equity=self.cash,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=NOW_MS - 1_000,
            observed_at_ms=NOW_MS,
        )

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        return []

    async def list_activities(self, **_kwargs: Any) -> list:
        return []

    async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("LIVE TRADE PORT WAS REACHED")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("LIVE CANCEL WAS REACHED")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


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
    broker = _LiveBroker()
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
    broker = _LiveBroker()

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


async def test_a_paper_authority_carries_no_envelope_even_when_values_are_offered(
    tmp_path: Path,
) -> None:
    """The envelope is the live world's; a paper authority never composes one."""
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST", artifacts_root=tmp_path
    )

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=lambda _account_id, _root: repo,
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )

    try:
        assert runtime.authority_kind == "sqlite"
        assert runtime.envelope_sync is None
        assert runtime.clerk is not None
        assert runtime.clerk.live_envelope is None
    finally:
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

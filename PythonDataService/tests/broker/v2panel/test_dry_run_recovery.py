"""A stopped Dry Run bot recovers inside its own simulated world (hurdle H33, #2563).

The owner's crashed Dry Run bot held 1 simulated SPY. "Reconcile now" answered
"No SQLite custody projection exists for bot ...", so Flatten never unlocked:
panel *reads* opened the bot's own ``sim:`` authority while panel *actions* ran
against the account's real Alpaca authority, which has never heard of the bot.
The custody card named the real account for the same reason.

These tests drive the real seams -- the panel read, the panel action executor
and the recovery check route -- against a real synthetic authority, with the
account's real authority wired to an Alpaca double that fails on any call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    close_synthetic_clerk_runtimes,
    get_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.registry import get_broker_registry, reset_broker_registry_for_testing
from app.marketdata.feed import MarketDataBar
from app.routers import alpaca_clerk_sqlite
from app.schemas.alpaca_clerk_sqlite import (
    ExtendedLimitConfirmationRequest,
    RecoveryActionCheckRequest,
    RecoveryActionExecuteRequest,
)
from app.schemas.broker_v2_panel import BotPanelView, PanelAction, PanelActionRequest
from app.schemas.deployment_budget import DeployBudgetConsent, DeploymentBudgetView
from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource, TopOfBookQuote
from app.services import market_liveness
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.services.broker_v2_panel import budget_deploy, panel_data_source, panel_scope
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    reset_idempotency_store_for_testing,
)
from app.services.broker_v2_panel.sqlite_panel_source import SqlitePanelBotNotFound, read_sqlite_panel_evidence
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.v2panel.conftest import account_snapshot
from tests.broker.v2panel.fixtures import ACCT

SID = "live-dry-dv-spy-0928"
SIM_ACCOUNT = f"sim:{SID}"
NEXT_SESSION_NOON = et_minute_of_day_ms(date(2026, 9, 9), 12 * 60)
NEXT_SESSION_PRE_MARKET = et_minute_of_day_ms(date(2026, 9, 9), 8 * 60)
_BAR_CLOSE = Decimal("600.00")


def _publish_quote(at_ms: int, *, bid: float, ask: float) -> None:
    """Publish one fresh IBKR SPY book to the real process store, as the status source does."""
    market_liveness.get_market_liveness_store().apply_status_snapshot(MarketStatusSnapshot(
        source=MarketStatusSource.IBKR, connected=True, observed_at_ms=at_ms, connection_changed_at_ms=at_ms,
        symbol_statuses=(), quotes=(
            TopOfBookQuote(symbol="SPY", bid=bid, ask=ask, source="ibkr.market_data.status", observed_at_ms=at_ms),
        ),
    ), now_ms=at_ms)


class _AlpacaThatMustNeverBeCalled:
    """The account's real Alpaca adapter: any call is a Dry Run leaking into the real world.

    Every call is recorded before it raises, so a caller that swallows the
    error (a reconciliation turning it into ``stale``) still fails the test.
    """

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _refuse(self, method: str) -> AssertionError:
        self.calls.append(method)
        return AssertionError(f"a Dry Run recovery reached the Alpaca adapter: {method}")

    def capabilities(self) -> None:
        raise self._refuse("capabilities")

    async def get_account(self) -> None:
        raise self._refuse("get_account")

    async def get_clock_evidence(self) -> None:
        raise self._refuse("get_clock_evidence")

    async def list_positions(self) -> None:
        raise self._refuse("list_positions")

    async def list_orders(self, **_kwargs: object) -> None:
        raise self._refuse("list_orders")

    async def list_activities(self, **_kwargs: object) -> None:
        raise self._refuse("list_activities")

    async def get_asset(self, _symbol: str) -> None:
        raise self._refuse("get_asset")

    async def submit(self, *_args: object, **_kwargs: object) -> None:
        raise self._refuse("submit")

    async def cancel(self, _order_id: str) -> None:
        raise self._refuse("cancel")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        raise self._refuse("get_order_by_client_order_id")


@dataclass(frozen=True)
class _World:
    alpaca: _AlpacaThatMustNeverBeCalled
    real: SqliteAlpacaClerkFacade
    clock: _TestClock


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="dry_run", quantity=1, action_plan=alpaca_v1_action_plan("SPY"), run_id="run-1", created_at_ms=0,
        sealed_account_id=SIM_ACCOUNT, exit_terms=TERMS, budget_consent=DeployBudgetConsent(
            committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed",
            world="synthetic",
        ),
    )


@pytest.fixture
async def crashed_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_World]:
    """A Dry Run that bought 1 simulated SPY at $600 and then crashed, runtime released."""
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    market_liveness.reset_market_liveness_store_for_testing()
    clock = _TestClock(NOON)
    alpaca = _AlpacaThatMustNeverBeCalled()
    get_broker_registry().register(alpaca)  # type: ignore[arg-type]
    # The route's account identity is the only account fact a panel needs;
    # it is served from the cached snapshot, never a fresh Alpaca call.
    async def _snapshot(_broker: str):
        return account_snapshot()

    monkeypatch.setattr(panel_scope, "resolve_account_snapshot", _snapshot)
    real_repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path / "real", clock=clock)
    real = SqliteAlpacaClerkFacade(repo=real_repo, read=alpaca, trade=alpaca, account_mode="paper")  # type: ignore[arg-type]
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=real, _sqlite_repository=real_repo))
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, now_ms=clock, boot_recovery_required=False)
    set_bot_task_registry(registry)
    binding = _binding()
    try:
        registry._bindings.record_launch(binding, launch_reason="deploy")
        authority = registry._authority_for(binding)
        await authority.ensure_recoverable()
        runtime = get_clerk_runtime(SIM_ACCOUNT)
        assert runtime is not None and runtime.clerk is not None
        _publish_quote(NOON, bid=float(_BAR_CLOSE), ask=float(_BAR_CLOSE))
        await runtime.clerk.register_strategy_run(binding)
        bars = authority.source_bars()
        try:
            decision_bar = bars.append(MarketDataBar(
                feed_id="ibkr", symbol="SPY", start_ms=NOON - 60_000, end_ms=NOON, open=_BAR_CLOSE,
                high=_BAR_CLOSE, low=_BAR_CLOSE, close=_BAR_CLOSE, volume=100, fetched_at_ms=NOON,
                session_phase="RTH",
            ), run_id=binding.run_id)
        finally:
            bars.close()
        clock.value = NOON + 1_000
        bought = await runtime.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=binding.run_id, decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan, quantity=1, retained_source_bar=decision_bar,
        )
        assert bought.child_order_refs, bought.explanation
        # The crash: the run is stopped and the in-process runtime released,
        # exactly as a supervised FEED_DEATH leaves a Dry Run.
        await runtime.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=binding.run_id, reason="crash")
        await authority.release_if_unused()
        assert get_clerk_runtime(SIM_ACCOUNT) is None
        clock.value = NOON + 30 * 60_000
        # IBKR's live SPY book half an hour after the crash.
        _publish_quote(clock.value, bid=601.25, ask=601.30)
        yield _World(alpaca=alpaca, real=real, clock=clock)
    finally:
        await close_synthetic_clerk_runtimes()
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        real_repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()
        market_liveness.reset_market_liveness_store_for_testing()


def _statement(view: DeploymentBudgetView) -> list[tuple[str, str]]:
    return [(line.label, line.amount_usd) for line in view.statement]


async def _panel() -> BotPanelView:
    return await panel_data_source.get_panel("alpaca", ACCT, SID)


def _action(panel: BotPanelView, action_id: str) -> PanelAction:
    return next(action for action in panel.actions if action.action_id == action_id)


async def _run(panel: BotPanelView, action_id: str, key: str):
    action = _action(panel, action_id)
    return await panel_data_source.run_action(
        "alpaca", ACCT, SID,
        PanelActionRequest(action_id=action_id, revision=panel.revision,
                           concurrency_token=action.concurrency_token, idempotency_key=key),
        operator_identity="owner",
    )


async def test_the_crashed_dry_run_reconciles_prepares_and_flattens_releasing_its_money(
    crashed_dry_run: _World,
) -> None:
    """H33, the owner's exact scenario: 1 simulated SPY held by a crashed Dry Run."""
    panel = await _panel()
    assert panel.exposure == {"SPY": 1.0}
    assert not _action(panel, "execute_safe_flatten").enabled

    reconciled = await _run(panel, "reconcile_now", "reconcile-1")

    assert reconciled.applied
    # The simulation is its own truth; the receipt says so rather than
    # implying anything was compared with a broker.
    assert "simulated" in reconciled.message.lower() and "alpaca" in reconciled.message.lower()

    panel = await _panel()
    prepare = _action(panel, "prepare_safe_flatten")
    assert prepare.enabled
    check = await alpaca_clerk_sqlite.check_bot_recovery_action(
        ACCT, SID, RecoveryActionCheckRequest(action_id="prepare_safe_flatten",
                                              concurrency_token=prepare.concurrency_token),
    )
    plan = check.capability.reduction_plan
    assert plan is not None and plan.account_id == SIM_ACCOUNT
    assert [(leg.symbol, leg.side, leg.quantity) for leg in plan.legs] == [("SPY", "sell", 1.0)]

    flattened = await _run(panel, "execute_safe_flatten", "flatten-1")

    assert flattened.applied
    assert "simulated sale" in flattened.message.lower() and "alpaca" in flattened.message.lower()
    panel = await _panel()
    assert panel.exposure == {}
    # Sold at IBKR's live bid when the flatten went out, never at the last bar the bot saw.
    assert [(fill.side, fill.quantity, fill.price) for fill in panel.recent_fills][:1] == [("sell", 1.0, 601.25)]
    money = await budget_deploy.budget_view(ACCT, SID)
    # Flat with nothing claimed, the bot is finished: its whole balance, net of
    # the sale's fees, is released at once and it has no slice left (review A1).
    assert (money.state, money.headline) == ("ready", "Stopped · fully released")
    # $1,000 + the $1.25 gain on the sale - $0.04 of modelled fees.
    assert _statement(money)[-4:] == [
        ("Balance", "1001.21"),
        ("Released at stop", "1001.21"),
        ("Still in shares, at cost", "0.00"),
        ("Still in entry orders", "0.00"),
    ]
    assert money.segment is None
    # The sale's fees settling after the session changes nothing the bot holds.
    crashed_dry_run.clock.value = NEXT_SESSION_NOON
    money = await budget_deploy.budget_view(ACCT, SID)
    assert (money.state, money.headline) == ("ready", "Stopped · fully released")
    assert _statement(money)[-3] == ("Released at stop", "1001.21")
    assert crashed_dry_run.alpaca.calls == []


async def test_a_dry_run_stopped_yesterday_flattens_at_todays_live_bid(
    crashed_dry_run: _World,
) -> None:
    """Review A3: a crash nobody noticed until the next session left the shares
    unsellable forever. The sale is priced from the quote IBKR shows now."""
    crashed_dry_run.clock.value = NEXT_SESSION_NOON
    _publish_quote(NEXT_SESSION_NOON, bid=598.10, ask=598.20)
    panel = await _panel()
    await _run(panel, "reconcile_now", "reconcile-next-day")
    panel = await _panel()

    flattened = await _run(panel, "execute_safe_flatten", "flatten-next-day")

    assert flattened.applied
    panel = await _panel()
    assert panel.exposure == {}
    assert [(fill.side, fill.quantity, fill.price) for fill in panel.recent_fills][:1] == [("sell", 1.0, 598.1)]
    money = await budget_deploy.budget_view(ACCT, SID)
    assert (money.headline, money.segment) == ("Stopped · fully released", None)
    assert crashed_dry_run.alpaca.calls == []


async def test_a_dry_run_with_no_live_quote_refuses_its_flatten_before_recording_an_exit(
    crashed_dry_run: _World,
) -> None:
    """With no IBKR quote nothing can price the simulated sale honestly, so
    both the prepared plan and the execute refuse in plain words -- before any
    EXIT exists that could never be sent, and without a false "the broker
    rejected it". The price is never guessed.
    """
    crashed_dry_run.clock.value = NEXT_SESSION_NOON
    panel = await _panel()
    await _run(panel, "reconcile_now", "reconcile-next-day")
    panel = await _panel()

    check = await alpaca_clerk_sqlite.check_bot_recovery_action(
        ACCT, SID, RecoveryActionCheckRequest(
            action_id="prepare_safe_flatten",
            concurrency_token=_action(panel, "prepare_safe_flatten").concurrency_token,
        ),
    )
    assert check.reduction_pricing is not None
    assert check.reduction_pricing.kind == "refused"
    assert check.reduction_pricing.reason_code == "SIMULATED_RECOVERY_PRICE_UNAVAILABLE"
    with pytest.raises(ActionNotAvailableError) as refused:
        await _run(panel, "execute_safe_flatten", "flatten-next-day")

    assert "no live IBKR price" in str(refused.value) and "broker" not in str(refused.value)
    panel = await _panel()
    assert panel.exposure == {"SPY": 1.0}
    assert not _action(panel, "discharge_attributed_residue").enabled
    assert crashed_dry_run.alpaca.calls == []


async def _execute_through_the_recovery_route(
    confirmed: ExtendedLimitConfirmationRequest | None = None,
):
    panel = await _panel()
    await _run(panel, "reconcile_now", f"reconcile-route-{confirmed is not None}")
    panel = await _panel()
    return await alpaca_clerk_sqlite.execute_bot_recovery_action(
        ACCT, SID, RecoveryActionExecuteRequest(
            action_id="execute_safe_flatten",
            concurrency_token=_action(panel, "execute_safe_flatten").concurrency_token,
            extended_limit=confirmed,
        ),
    )


async def test_the_bot_recovery_route_sells_a_dry_run_at_market_without_touching_alpaca(
    crashed_dry_run: _World,
) -> None:
    executed = await _execute_through_the_recovery_route()

    assert executed.applied
    panel = await _panel()
    assert panel.exposure == {}
    assert [(fill.side, fill.price) for fill in panel.recent_fills][:1] == [("sell", 601.25)]
    assert crashed_dry_run.alpaca.calls == []


async def test_the_bot_recovery_route_sells_a_dry_run_pre_market_at_its_confirmed_limit(
    crashed_dry_run: _World,
) -> None:
    crashed_dry_run.clock.value = NEXT_SESSION_PRE_MARKET
    _publish_quote(NEXT_SESSION_PRE_MARKET, bid=598.10, ask=598.40)

    executed = await _execute_through_the_recovery_route(ExtendedLimitConfirmationRequest(
        limit_price=598.10, quote_observed_at_ms=NEXT_SESSION_PRE_MARKET,
    ))

    assert executed.applied
    panel = await _panel()
    assert panel.exposure == {}
    assert [(fill.side, fill.price) for fill in panel.recent_fills][:1] == [("sell", 598.1)]
    assert crashed_dry_run.alpaca.calls == []


async def test_dry_run_recovery_never_touches_the_alpaca_adapter(
    crashed_dry_run: _World,
) -> None:
    """The safety invariant: a ``sim:`` bot's actions never reach Alpaca, not even a read."""
    panel = await _panel()
    await _run(panel, "reconcile_now", "reconcile-safety")
    panel = await _panel()
    await _run(panel, "execute_safe_flatten", "flatten-safety")

    assert crashed_dry_run.alpaca.calls == []


async def test_the_dry_run_custody_card_names_its_own_simulated_account(
    crashed_dry_run: _World,
) -> None:
    panel = await _panel()

    assert panel.clerk.account_id == SIM_ACCOUNT
    # The page still addresses the bot through the account it is listed under.
    assert panel.account_id == ACCT


async def test_a_bot_without_custody_is_refused_in_owner_words(
    crashed_dry_run: _World,
) -> None:
    """The words the owner saw on H33 named the storage engine, not the problem."""
    with pytest.raises(SqlitePanelBotNotFound) as refused:
        await read_sqlite_panel_evidence("alpaca", ACCT, SID, now_ms=NOON, facade=crashed_dry_run.real)

    assert "sqlite" not in str(refused.value).lower()
    assert SID in str(refused.value)

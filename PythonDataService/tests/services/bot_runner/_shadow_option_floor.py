"""One Clerk process with an option bot on a Live Shadow, for the tests that drive it through the real runner.

The floor of ``_option_floor`` with a Live Shadow in place of the Dry Run: the
same IBKR supervisor on a stand-in connection, the same resolver, search
lane, scripted bars and single clock. What changes:

- the runner is ``run_trade_bot``, which is what a Live Shadow's bot runs;
- the account's Clerk is the real composition of a Shadow
  (``activate_shadow_clerk_authority`` then ``select_active_clerk_runtime``):
  the shadow order book and its two ports, the account reading with its
  simulated projection, the deadline keeper, and the books the Clerk's
  keeping follows for what its bots hold;
- the live Alpaca account is a double that answers the reads a Shadow makes
  (the account, its orders for the namespace check, the contract listing)
  and **fails the test on any trade call**. Nothing is sent to Alpaca, and no
  key is used.

The keeper is not started as a task: a test runs each of its rounds, and
each reconciliation pass, when it says so.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import shadow_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_shadow_clerk_authority,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.options import BrokerOptionContract
from app.options.contract import OptionContract
from app.schemas.action_plan import ActionPlan
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, SHADOW_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import documented_option_listing

from ._option_floor import CALL_585, Clock, Floor, Ran, ScriptedFeed, Step, one_call

SHADOW_SID = "shadow-buys-a-call"
SHADOW_RUN = "run-shadow"


def shadow_binding_for(
    plan: ActionPlan, *, listens_to: str = "SPY", budget_cents: int = 100_000, contracts: int = 1,
    run_id: str = SHADOW_RUN,
) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SHADOW_SID, strategy_key="deployment_validation", broker="alpaca", symbol=listens_to,
        mode="trade", quantity=contracts, action_plan=plan, run_id=run_id, created_at_ms=0,
        sealed_account_id=SHADOW_ACCT, exit_terms=DEPLOY_EXIT_TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=budget_cents, risk_revision=1, actor="owner", request_fingerprint="reviewed",
            world="shadow",
        ),
    )


class LiveAccount(_LiveBroker):
    """The live Alpaca account a Shadow reads, and nothing more: every trade call fails the test.

    The reads are stamped from the floor's clock. ``listed`` is what
    Alpaca's contract reference lists, by OCC symbol; ``listing_error`` is
    raised by a read of it instead.
    """

    def __init__(self, clock: Clock) -> None:
        super().__init__(now_ms=clock.value)
        self._clock = clock
        self.listed: dict[str, BrokerOptionContract] = {CALL_585.occ_symbol: documented_option_listing(CALL_585)}
        self.listing_error: Exception | None = None
        self.listing_reads: list[str] = []
        # Every call that could send, cancel or look up an order at Alpaca. A Shadow makes none.
        self.trade_calls: list[str] = []

    @property
    def now_ms(self) -> int:
        return self._clock.value

    @now_ms.setter
    def now_ms(self, _value: int) -> None:
        """The stamp is the floor's clock, whatever the fixture it is built from was handed."""

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        self.listing_reads.append(contract.occ_symbol)
        if self.listing_error is not None:
            raise self.listing_error
        return self.listed.get(contract.occ_symbol)

    def _never(self, call: str) -> None:
        self.trade_calls.append(call)
        pytest.fail(f"A Live Shadow reached the live account's trade port: {call}")

    async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        self._never("submit")

    async def cancel(self, _order_id: str) -> None:
        self._never("cancel")

    async def submit_option_order(self, *_args: Any, **_kwargs: Any) -> Any:
        self._never("submit_option_order")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        self._never("get_order_by_client_order_id")

    async def get_order_by_broker_order_id(self, _order_id: str) -> None:
        self._never("get_order_by_broker_order_id")

    async def get_option_order_by_client_order_id(self, _client_order_id: str) -> None:
        self._never("get_option_order_by_client_order_id")

    async def get_option_order_by_broker_order_id(self, _order_id: str) -> None:
        self._never("get_option_order_by_broker_order_id")


class ShadowFloor(Floor):
    """The Clerk's process: its IBKR connection and supervisor, one Live Shadow with its Clerk, and the clock."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        plan: ActionPlan | None = None,
        contracts: int = 1,
        budget_cents: int = 100_000,
        **floor: Any,
    ) -> None:
        super().__init__(monkeypatch, tmp_path, plan=plan, budget_cents=budget_cents, **floor)
        self.binding = shadow_binding_for(
            plan or one_call(), listens_to=self.binding.symbol, budget_cents=budget_cents, contracts=contracts
        )
        self.root = tmp_path / "shadow"
        self.live = LiveAccount(self.clock)
        self._runtime: ActiveClerkRuntime | None = None
        self._ledger: SourceBarLedger | None = None
        # How many times the Clerk paused between two reads of an entry's outcome.
        self.outcome_reads_paused = 0

    # ---- the account ------------------------------------------------------------

    async def open(self, *, register: bool = True) -> ShadowFloor:
        """Activate and compose the live account's Shadow, switch it to budgets, and deploy the bot on it."""
        await activate_shadow_clerk_authority(live_account_id=LIVE_ACCT, artifacts_root=self.root)
        await self._compose()
        repo, sync = self.repo, self.runtime.envelope_sync
        assert sync is not None
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token="reviewed", stop_receipt="empty")
        sync.apply_risk_policy(
            AccountRiskPolicy(
                1, TEST_ENVELOPE_VALUES.loss_fraction, TEST_ENVELOPE_VALUES.loss_usd, "profile", 1, "owner",
                self.clock.value,
            ),
            expected_revision=0,
        )
        assert await sync.tick() == "observed"
        if register:
            await self.clerk.register_strategy_run(self.binding)
        return self

    async def _compose(self) -> None:
        def opened(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
            return ClerkSqliteRepository.open(account_id=account_id, artifacts_root=artifacts_root, clock=self.clock)

        runtime = await select_active_clerk_runtime(
            read=self.live, trade=self.live, artifacts_root=self.root, repository_opener=opened,
            live_envelope_values=TEST_ENVELOPE_VALUES,
        )
        assert runtime.authority_kind == "shadow", runtime.startup_failure
        self._runtime = runtime

        async def no_rest(_seconds: float) -> None:
            self.outcome_reads_paused += 1
            pytest.fail("a Shadow's entry is proven as it is sent: the Clerk never pauses for it")

        runtime.clerk._outcome_pause = no_rest  # type: ignore[union-attr]
        set_alpaca_clerk(runtime.clerk)
        self._note_what_the_clerk_is_asked()

    async def close(self) -> None:
        set_alpaca_clerk(None)
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None

    async def the_clerk_restarts(self) -> None:
        """The Clerk's process dies and a new one opens the same books: the Shadow is composed again."""
        assert self._runtime is not None
        await self._runtime.close()
        await self._compose()

    @property
    def runtime(self) -> ActiveClerkRuntime:
        assert self._runtime is not None, "the Shadow's Clerk is not composed"
        return self._runtime

    @property
    def clerk(self) -> Any:
        assert self.runtime.clerk is not None
        return self.runtime.clerk

    @property
    def repo(self) -> Any:
        assert self.runtime.sqlite_repository is not None
        return self.runtime.sqlite_repository

    @property
    def keeper(self) -> DeadlineKeeper:
        """The Shadow's own keeper, as its composition built it. A test asks it for each round."""
        keeper = self.runtime.deadline_keeper
        assert isinstance(keeper, DeadlineKeeper), "the Shadow was composed with no deadline keeper"
        return keeper

    @property
    def sid(self) -> str:
        return self.binding.strategy_instance_id

    # ---- what the Clerk does by itself ----------------------------------------------

    async def a_pass(self) -> AccountReconciliationResult:
        """One reconciliation pass of the Shadow, as its sweep runs it."""
        return await reconcile_account(
            self.repo, read=self.clerk._read, trade=self.clerk._trade, intake=self.clerk.intake,
            pricing=self.clerk.recovery_pricing,
        )

    async def stop_is_pressed(self) -> None:
        """The owner's Stop: the run is ended on the Clerk's books."""
        self.running = False
        await self.clerk.stop_strategy_run(strategy_instance_id=self.sid, run_id=self.binding.run_id)

    # ---- a run ------------------------------------------------------------------

    def ledger(self) -> SourceBarLedger:
        """The bars this bot's own instance retains: a Shadow's evidence ledger."""
        if self._ledger is None:
            self._ledger = SourceBarLedger(
                artifacts_root=self.root, account_id=shadow_evidence_account_id_for_strategy(self.sid)
            )
        return self._ledger

    async def run(
        self, script: list[Step], *, pricing_bar_wait_ms: int = 20, the_process_own_market: bool = False
    ) -> Ran:
        """Run the bot over ``script`` to the end of its Signal stream, through ``run_trade_bot``."""
        del the_process_own_market
        feed = self.feed = ScriptedFeed(self, script, signal=self.binding.symbol)
        await asyncio.wait_for(
            bot_trade_strategy.run_trade_bot(
                self.binding, feed, source_bars=self.ledger(), pricing_feed=feed,
                pricing_bar_wait_ms=pricing_bar_wait_ms, option_market=self.market(),
            ),
            timeout=120,
        )
        await self.clerk.drain_effects()
        return Ran(feed=feed, journal=[], receipts=self.receipts())

    def receipts(self) -> list[tuple[str, dict[str, Any]]]:
        oldest_first = self.repo.decision_receipt_tail(strategy_instance_id=self.sid, limit=50)
        return [(receipt.outcome, json.loads(receipt.facts_json)) for receipt in oldest_first]

    # ---- what the books and the shadow order book say ---------------------------------

    def holds(self) -> dict[str, float]:
        return {
            symbol: quantity
            for symbol, quantity in self.repo.attributed_positions_for_strategy(self.sid).items()
            if quantity
        }

    def fills(self) -> list[tuple[str, str, float, float, int | None, Any]]:
        """Every fill on the books: its symbol, side, contracts, premium, multiplier and the cash it moved."""
        with self.repo._write_lock:
            records = effective_fill_records(self.repo._conn, account_id=SHADOW_ACCT)
        return [
            (fill.symbol, fill.side.value, fill.quantity, fill.fill_price, fill.multiplier, fill.exact_notional)
            for fill in records
        ]

    def kept_quotes(self) -> KeptOptionQuotes:
        return KeptOptionQuotes.of_account(artifacts_root=self.root, account_id=SHADOW_ACCT)

    def watched_as_held(self) -> tuple[OptionContract, ...]:
        """The contracts the bot's own run asks to have quoted as held lines, now."""
        return tuple(contract for request in self.watch.requests() for contract in request.held)

"""One Clerk process with an option bot on a Paper account, for the tests that drive it through the real runner.

The floor of ``_option_floor`` with a Paper account in place of the Dry Run:
the same IBKR supervisor on a stand-in connection, the same resolver, search
lane, scripted bars and single clock. What changes:

- the runner is ``run_trade_bot``;
- the account's Clerk is the real SQLite Clerk of a Paper account, with its
  account reading and its deadline keeper;
- Alpaca is the double the Clerk's own Paper tests use
  (``conftest._FakeTradePort``): an option order sent to it is kept as
  Alpaca's documentation shows one and rests until the test fills or ends
  it. No order goes to Alpaca, and no key is used.

**While an entry rests** the runner waits inside the Clerk
(``await_entry_outcome``), which reads its books every 200 ms. The floor is
that pause: each one moves the clock, renews the Clerk's hold on its account,
lets the venue do what the test said it does at that moment, and runs a round
of the deadline keeper once a second. So a test says what Alpaca does while
the order rests, in milliseconds from the first read of its outcome.

**The stream is asked for one bar at a time.** A run has one bar in flight,
so while the runner waits for an entry nothing asks the feed for the next
one. The feed here notes what the runner had done each time it was asked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca import adapter as alpaca_adapter
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryOutcome, SettledEntry, entry_outcome, recorded_settlement
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrderEvent
from app.marketdata.feed import ContinuityPolicy, MarketDataBar
from app.options.contract import OptionContract
from app.schemas.action_plan import ActionPlan
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_trade_strategy_warmup import CapturedDecisions, captured_decision_outcomes
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeTradePort, documented_option_listing
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import PaperAccountRead

from ._option_floor import CALL_585, Floor, Ran, ScriptedFeed, Step, one_call

PAPER_ACCOUNT = "PAPER-OPTIONS"
PAPER_SID = "paper-buys-a-call"
PAPER_RUN = "run-paper"
# How often the keeper's round is run while an entry rests: it asks once a second past a deadline.
KEEPER_EVERY_MS = 1_000


def paper_binding_for(
    plan: ActionPlan, *, listens_to: str = "SPY", budget_cents: int = 100_000, contracts: int = 1, run_id: str = PAPER_RUN
) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=PAPER_SID, strategy_key="deployment_validation", broker="alpaca", symbol=listens_to,
        mode="trade", quantity=contracts, action_plan=plan, run_id=run_id, created_at_ms=0,
        sealed_account_id=PAPER_ACCOUNT, exit_terms=DEPLOY_EXIT_TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=budget_cents, risk_revision=1, actor="owner", request_fingerprint="reviewed",
            world="real_paper",
        ),
    )


class RestingFeed(ScriptedFeed):
    """The scripted feed, which also notes what the runner had done each time it was asked for a bar."""

    async def stream_bars(
        self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None
    ) -> AsyncIterator[MarketDataBar]:
        floor = self._floor
        assert isinstance(floor, PaperFloor)
        async for bar in super().stream_bars(symbol, use_rth=use_rth, continuity=continuity):
            yield bar
            if symbol == self._signal:
                # The run has come back for the bar after this one.
                floor.asked_for_the_bar_after[bar.end_ms] = floor.what_the_runner_has_done()


class _AccountReading:
    """The account's own reading cadence, as the floor ticks it: Alpaca's answer is stamped now, and the fee
    evidence is as fresh as its own cadence keeps it, with no fee row posted."""

    def __init__(self, floor: PaperFloor) -> None:
        self._floor = floor

    async def tick(self) -> str:
        floor = self._floor
        floor.account.account_observed_at_ms = floor.clock.value
        record_fee_evidence(floor.repo, [], checked_at_ms=floor.clock.value, history_complete=True)
        return await floor.sync.tick()


class PaperFloor(Floor):
    """The Clerk's process: its IBKR connection and supervisor, one Paper account with its Clerk, and the clock."""

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
        self.binding = paper_binding_for(
            plan or one_call(), listens_to=self.binding.symbol, budget_cents=budget_cents, contracts=contracts
        )
        self.venue = _FakeTradePort()
        self.venue.venue_clock = self.clock
        self.account = PaperAccountRead(self.venue)
        # What Alpaca lists: the call the scene's rules come to, as an active standard contract.
        self.account.listed = {CALL_585.occ_symbol: documented_option_listing(CALL_585)}
        self._repo: ClerkSqliteRepository | None = None
        self._clerk: SqliteAlpacaClerkFacade | None = None
        self.sync: LiveEnvelopeSync
        self.keeper: DeadlineKeeper
        # What Alpaca does while each entry rests, by which of the bot's entries it is: (milliseconds from
        # the first read of its outcome, what).
        self._while_resting: dict[int, list[tuple[int, Callable[[], Awaitable[None] | None]]]] = {}
        self._rest_began_ms: int | None = None
        self._keeper_round_due_ms = 0
        # The limit each wait for an entry's outcome was asked with, in the order the runner asked.
        self.waits_asked_until: list[int] = []
        # How many times the Clerk paused between two reads of an entry's outcome.
        self.outcome_reads_paused = 0
        # By the close of each bar of the signal: what the runner had done when it asked its stream
        # for the bar after it.
        self.asked_for_the_bar_after: dict[int, dict[str, Any]] = {}
        self._ledger: SourceBarLedger | None = None

    # ---- the account ------------------------------------------------------------

    async def open(self) -> PaperFloor:
        """Open the Paper account's books, compose its Clerk, and deploy the bot with its budget."""
        repo = ClerkSqliteRepository.initialize(
            account_id=PAPER_ACCOUNT, artifacts_root=self.tmp_path / "paper", clock=self.clock
        )
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
        append_risk_policy(
            repo, policy=AccountRiskPolicy(1, 0.1, 100, "profile", 1, "owner", self.clock.value), expected_revision=0
        )
        self._repo = repo
        self._compose()
        assert await self.account_reading.tick() == "observed"
        await self.clerk.register_strategy_run(self.binding)
        self._note_what_the_clerk_is_asked()
        return self

    def _compose(self) -> None:
        """A Paper account's Clerk over the books: its account reading, its deadline keeper, its facade."""
        repo = self.repo

        async def reading_pause(seconds: float) -> None:
            self.clock.value += round(seconds * 1000)

        gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
        self.sync = LiveEnvelopeSync(repo=repo, read=self.account, envelope=gate, sleep=reading_pause)
        self.keeper = DeadlineKeeper(repo=repo, trade=self.venue, off_loop=run_inline)
        clerk = SqliteAlpacaClerkFacade(
            repo=repo, read=self.account, trade=self.venue, account_mode="paper", live_envelope=gate,
            entry_reading=self.sync.read_for_entry, entry_deadline_nudge=self.keeper.nudge,
        )
        clerk._outcome_pause = self._an_entry_rests
        ask = clerk.await_entry_outcome

        async def asked(**wait: Any) -> Any:
            self._rest_began_ms = None
            self.waits_asked_until.append(wait["until_ms"])
            return await ask(**wait)

        self._monkeypatch.setattr(clerk, "await_entry_outcome", asked)
        self._clerk = clerk
        self.account_reading = _AccountReading(self)
        set_alpaca_clerk(clerk)

    async def close(self) -> None:
        set_alpaca_clerk(None)
        if self._clerk is not None:
            await self.keeper.stop()
            await self.sync.stop()
            await self._clerk.drain_effects()
        if self._ledger is not None:
            self._ledger.close()
        if self._repo is not None:
            self._repo.close()

    @property
    def runtime(self) -> Any:
        """What the floor's own passing of time asks of a Clerk's runtime: its books and its account reading."""
        if self._repo is None:
            return None
        return SimpleNamespace(sqlite_repository=self._repo, envelope_sync=self.account_reading, clerk=self._clerk)

    @property
    def clerk(self) -> Any:
        assert self._clerk is not None, "the Paper account's Clerk is not composed"
        return self._clerk

    @property
    def repo(self) -> Any:
        assert self._repo is not None, "the Paper account's books are not open"
        return self._repo

    @property
    def sid(self) -> str:
        return self.binding.strategy_instance_id

    # ---- while an entry rests -----------------------------------------------------

    def while_it_rests(self, after_ms: int, what: Callable[[], Awaitable[None] | None], *, entry: int = 0) -> None:
        """Say what happens ``after_ms`` into an entry's rest, counted from the first read of its outcome.
        ``entry`` is which of the bot's entries: the first unless told."""
        due = self._while_resting.setdefault(entry, [])
        due.append((after_ms, what))
        due.sort(key=lambda said: said[0])

    async def _an_entry_rests(self, seconds: float) -> None:
        """The Clerk's pause between two reads of an entry's outcome: time passes, and what was said happens."""
        self.outcome_reads_paused += 1
        if self._rest_began_ms is None:
            self._rest_began_ms = self.clock.value
            self._keeper_round_due_ms = self.clock.value
        until = self.clock.value + round(seconds * 1000)
        due = self._while_resting.get(len(self.waits_asked_until) - 1, [])
        while due and self._rest_began_ms + due[0][0] <= until:
            after_ms, what = due.pop(0)
            self.clock.value = max(self.clock.value, self._rest_began_ms + after_ms)
            done = what()
            if done is not None:
                await done
        self.clock.value = max(self.clock.value, until)
        self.repo.renew_execution_lease()
        if self.clock.value >= self._keeper_round_due_ms:
            self._keeper_round_due_ms = self.clock.value + KEEPER_EVERY_MS
            await self.keeper.tick()
        # Whatever else is ready to run beside the runner's wait gets its turn.
        for _ in range(6):
            await asyncio.sleep(0)

    def what_the_runner_has_done(self) -> dict[str, Any]:
        """A look at the run as it stands: the decisions it has finished with, and each receipt written."""
        return {
            "finished": self._evaluations_finished,
            "outcomes": [outcome for outcome, _ in self.receipts()],
            "asked_of_the_clerk": list(self.asked_of_the_clerk),
            "settled": self.transitions("ENTRY_SETTLED"),
        }

    # ---- what Alpaca does ----------------------------------------------------------

    def order_ref(self, which: int = -1) -> str:
        """The client order id of one option order the venue was sent, the newest unless told."""
        return self.venue.option_submit_calls[which]

    async def alpaca_fills(self, contracts: int, *, in_all: int | None = None, price: str = "4.12") -> None:
        """The venue fills ``contracts`` more of the newest order, and Alpaca's stream delivers that execution."""
        order_ref = self.order_ref()
        payload = self.venue.option_orders[order_ref]
        filled_in_all = int(payload["filled_qty"]) + contracts if in_all is None else in_all
        self.venue.option_order_fills(order_ref, contracts=filled_in_all, average_price=price)
        frame = alpaca_adapter.from_alpaca_order(self.venue.option_orders[order_ref])
        sink = SqliteTradeUpdateEvidenceSink(repo=self.repo, intake=self.clerk.intake, reconciler=self.clerk)
        await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=BrokerOrderEvent(
                event_type="fill" if frame.status == "filled" else "partial_fill", occurred_at_ms=self.clock.value,
                price=float(price), quantity=contracts, execution_id=f"x-{order_ref[-6:]}-{filled_in_all}",
            ),
            event_key=f"execution:{order_ref}:{filled_in_all}", order=frame, recovery_source=None,
            recovery_window_limit=None,
        )

    async def a_pass(self) -> AccountReconciliationResult:
        """One reconciliation pass of the account, as its sweep runs it."""
        return await reconcile_account(
            self.repo, read=self.account, trade=self.venue, intake=self.clerk.intake,
            pricing=self.clerk.recovery_pricing,
        )

    async def the_clerk_restarts(self) -> None:
        """The Clerk's process dies and a new one opens the same books: its startup recovery, then ready."""
        await self.keeper.stop()
        await self.sync.stop()
        await self.clerk.drain_effects()
        self.repo.close()
        self._repo = ClerkSqliteRepository.open(
            account_id=PAPER_ACCOUNT, artifacts_root=self.tmp_path / "paper", clock=self.clock
        )
        self._rest_began_ms = None
        self._compose()
        await self.clerk.recover()

    async def stop_is_pressed(self) -> None:
        """The owner's Stop: the run is ended on the Clerk's books."""
        self.running = False
        await self.clerk.stop_strategy_run(strategy_instance_id=self.sid, run_id=self.binding.run_id)

    # ---- a run ------------------------------------------------------------------

    def ledger(self) -> SourceBarLedger:
        if self._ledger is None:
            self._ledger = SourceBarLedger(artifacts_root=self.tmp_path / "ledger", account_id=PAPER_ACCOUNT)
        return self._ledger

    def running_the_bot(self, script: list[Step], *, pricing_bar_wait_ms: int = 20) -> Awaitable[None]:
        """The runner's own coroutine over ``script``, for a test that must cancel it."""
        feed = self.feed = RestingFeed(self, script, signal=self.binding.symbol)
        return bot_trade_strategy.run_trade_bot(
            self.binding, feed, source_bars=self.ledger(), pricing_feed=feed,
            pricing_bar_wait_ms=pricing_bar_wait_ms, option_market=self.market(),
        )

    async def run(
        self, script: list[Step], *, pricing_bar_wait_ms: int = 20, the_process_own_market: bool = False
    ) -> Ran:
        """Run the bot over ``script`` to the end of its Signal stream."""
        del the_process_own_market
        await asyncio.wait_for(self.running_the_bot(script, pricing_bar_wait_ms=pricing_bar_wait_ms), timeout=120)
        await self.clerk.drain_effects()
        assert self.feed is not None
        return Ran(feed=self.feed, journal=[], receipts=self.receipts())

    def receipts(self) -> list[tuple[str, dict[str, Any]]]:
        import json

        oldest_first = self.repo.decision_receipt_tail(strategy_instance_id=self.sid, limit=50)
        return [(receipt.outcome, json.loads(receipt.facts_json)) for receipt in oldest_first]

    # ---- what the books say --------------------------------------------------------

    def holds(self) -> dict[str, float]:
        return {
            symbol: quantity
            for symbol, quantity in self.repo.attributed_positions_for_strategy(self.sid).items()
            if quantity
        }

    def fills(self) -> list[tuple[str, str, float, float, int | None, Any]]:
        """Every fill on the books: its symbol, side, contracts, premium, multiplier and the cash it moved."""
        with self.repo._write_lock:
            records = effective_fill_records(self.repo._conn, account_id=PAPER_ACCOUNT)
        return [
            (fill.symbol, fill.side.value, fill.quantity, fill.fill_price, fill.multiplier, fill.exact_notional)
            for fill in records
        ]

    def entry(self, which: int = 0) -> EntryOutcome:
        """Where one of the bot's entries stands on the books, the first unless told."""
        return entry_outcome(self.repo, self.venue_entry_refs()[which])

    def venue_entry_refs(self) -> list[str]:
        with self.repo._write_lock:
            rows = self.repo._conn.execute("SELECT order_ref FROM orders WHERE role = 'ENTRY' ORDER BY rowid").fetchall()
        return [row[0] for row in rows]

    def settled(self, which: int = 0) -> SettledEntry | None:
        """The answer the Clerk recorded for one of the bot's entries, or None when it recorded none."""
        return recorded_settlement(self.repo, self.venue_entry_refs()[which])

    def sent(self) -> list[tuple[str, str, int, str, str | None]]:
        """Every option order sent to Alpaca: its contract, side, contracts, kind and limit."""
        return [
            (
                order.contract.occ_symbol, order.side.value, order.quantity, order.order_type.value,
                None if order.limit_price is None else format(order.limit_price, "f"),
            )
            for order in self.venue.submitted_option_orders
        ]

    def replayed_dispositions(self, *, run_id: str = "run-after", option_entries: bool = True) -> CapturedDecisions:
        """What a later run's warm-up would read of this bot's decisions, as a Start after this one reads it."""
        receipts = SqliteDecisionReceipts(self.repo, strategy_instance_id=self.sid)
        return captured_decision_outcomes(receipts, run_id=run_id, option_entries=option_entries)

    def watched_as_held(self) -> tuple[OptionContract, ...]:
        """The contracts the bot's own run asks to have quoted as held lines, now."""
        return tuple(contract for request in self.watch.requests() for contract in request.held)

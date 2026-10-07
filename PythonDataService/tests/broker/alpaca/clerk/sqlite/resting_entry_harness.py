"""A Paper account whose bots trade one bought option, with a venue that rests the order (#2913).

The real SQLite Clerk of a Paper account, on real books, with two doubles at
its edges:

- the trade port is ``conftest._FakeTradePort``: an option order sent to it
  is kept as Alpaca's documentation shows one, and stays ``new`` until the
  test fills or ends it. Every answer is read by the adapter's own reader;
- the read port lists the venue's working orders and the positions its
  fills made, and lists the option contracts a test says Alpaca lists.

Nothing here was sent to Alpaca or recorded from it. The prices are one SPY
call, bid $1.90 and offered at $1.94, a hundred shares a contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca import adapter as alpaca_adapter
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence, OptionExitEvidence
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper, Nudge
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryOutcome, entry_outcome
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult, reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerPosition
from app.broker.contract.options import BrokerOptionAccountSnapshot, BrokerOptionContract
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.schemas.action_plan import ActionPlan
from app.services import market_liveness
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import (
    NOON,
    _FakeTradePort,
    _TestClock,
    documented_option_listing,
)
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import _Account

GENERATION = 4
CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="call", strike=585)
SYMBOL = CALL.occ_symbol
LEG = "long_call"
WAIT_MS = CloseTerms().entry_wait_ms
BUYS_ONE_CALL = ActionPlan.model_validate({
    "on_enter": [{
        "leg_id": LEG, "instrument": {"kind": "option", "underlying": "SPY"}, "position": "long", "qty_ratio": 1,
        "right": "call", "strike": {"selector": "atm"}, "expiry": {"selector": "nearest_weekly"},
    }],
    "on_exit": [{"kind": "close_leg", "entry_leg_id": LEG}],
})
_WORKING = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel"})


class PaperAccountRead(_Account):
    """What Alpaca would show of the account: the venue's working orders, its positions, the contracts it lists."""

    def __init__(self, venue: _FakeTradePort) -> None:
        super().__init__()
        self._venue = venue
        self.listed: dict[str, BrokerOptionContract] = {SYMBOL: documented_option_listing(CALL)}
        self.listing_error: Exception | None = None
        self.listing_reads: list[str] = []
        # What the account states of its option buying power, in dollars; a test may change it.
        self.buying_power: Decimal | None = Decimal(100_000)
        self.option_account_reads = 0

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        self.option_account_reads += 1
        return BrokerOptionAccountSnapshot(
            broker="alpaca", account_id="PA-OPTIONS", account_mode="paper", options_buying_power=self.buying_power,
            options_approved_level=3, options_trading_level=3, observed_at_ms=NOON,
        )

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        self.listing_reads.append(contract.occ_symbol)
        if self.listing_error is not None:
            raise self.listing_error
        return self.listed.get(contract.occ_symbol)

    async def list_orders(self, **_narrowed: Any) -> list[BrokerOrder]:
        # The account's open orders, as the stock reader reads any order in a list.
        return [
            alpaca_adapter.from_alpaca_order(payload)
            for payload in self._venue.option_orders.values()
            if payload["status"] in _WORKING
        ]

    async def list_positions(self) -> list[BrokerPosition]:
        held: dict[str, float] = {}
        for payload in self._venue.option_orders.values():
            signed = float(payload["filled_qty"]) * (1 if payload["side"] == "buy" else -1)
            held[payload["symbol"]] = held.get(payload["symbol"], 0.0) + signed
        return [
            BrokerPosition(
                broker="alpaca", symbol=symbol, asset_id=None, asset_class="us_option", quantity=abs(quantity),
                side="long" if quantity > 0 else "short", average_entry_price=1.94, market_value=194.0 * abs(quantity),
                cost_basis=194.0 * abs(quantity), current_price=1.94, unrealized_pl=0.0, unrealized_plpc=0.0,
                observed_at_ms=NOON,
            )
            for symbol, quantity in held.items()
            if quantity
        ]


@dataclass
class PaperOptions:
    """One Paper account, its real Clerk, a clock the test moves, and two deployed bots, ``a`` and ``b``."""

    repo: ClerkSqliteRepository
    clock: _TestClock
    clerk: SqliteAlpacaClerkFacade
    venue: _FakeTradePort
    account: PaperAccountRead
    keeper: DeadlineKeeper
    sync: LiveEnvelopeSync
    artifacts_root: Path
    decisions: int = 0

    async def restart(self) -> None:
        """The Clerk's process dies and a new one opens the same books: its startup recovery, then ready."""
        await self.keeper.stop()
        await self.sync.stop()
        await self.clerk.drain_effects()
        self.repo.close()
        self.repo = ClerkSqliteRepository.open(
            account_id=self.repo.account_id, artifacts_root=self.artifacts_root, clock=self.clock
        )
        self.sync, self.keeper, self.clerk = _compose(self.repo, self.clock, self.venue, self.account)
        await self.clerk.recover()

    def evidence(self, bid: str = "1.90", ask: str = "1.94", **changes: Any) -> OptionEntryEvidence:
        """What a runner hands over: the pick, its multiplier, and a quote received 300 ms ago."""
        received = self.clock() - 300
        quote = HeldQuote(
            bid=QuoteSide(Decimal(bid), received), ask=QuoteSide(Decimal(ask), received),
            data_type=QuoteDataType.LIVE, generation=GENERATION,
        )
        return replace(
            OptionEntryEvidence(
                picked={LEG: CALL}, multipliers={LEG: 100}, quotes={LEG: quote}, generation=GENERATION,
                rechecked_at_ms=self.clock() - 100, leg_gap_limit_percent=Decimal(8), cost_limit_percent=Decimal(10),
            ),
            **changes,
        )

    async def enter(
        self, sid: str = "a", *, units: int = 1, decision: str | None = None,
        evidence: OptionEntryEvidence | None = None,
    ) -> EffectOperationReceipt:
        self.decisions += 1
        return await self.clerk.execute_for_instance(
            strategy_instance_id=sid, run_id=f"run-{sid}", decision_id=decision or f"enter-{self.decisions}",
            purpose=EffectPurpose.ENTER, action_plan=BUYS_ONE_CALL, quantity=units,
            option_entry=evidence if evidence is not None else self.evidence(),
        )

    async def exit(self, sid: str = "a", *, quotes: OptionExitEvidence | None = None) -> EffectOperationReceipt:
        self.decisions += 1
        return await self.clerk.execute_for_instance(
            strategy_instance_id=sid, run_id=f"run-{sid}", decision_id=f"exit-{self.decisions}",
            purpose=EffectPurpose.EXIT, action_plan=BUYS_ONE_CALL, quantity=1, option_exit=quotes,
        )

    async def resting(self, sid: str = "a", *, units: int = 1, decision: str | None = None) -> str:
        """An entry sent and resting at the venue; its order reference."""
        receipt = await self.enter(sid, units=units, decision=decision)
        assert len(receipt.child_order_refs) == 1, receipt.explanation
        return receipt.child_order_refs[0]

    def wait(self, ms: int) -> None:
        """Let time pass, the Clerk's hold on its account kept alive as its heartbeat keeps it."""
        while ms > 0:
            step = min(ms, 10_000)
            self.clock.advance(step)
            self.repo.renew_execution_lease()
            ms -= step

    async def round(self) -> Any:
        """One round of the deadline keeper."""
        return await self.keeper.tick()

    def another_keeper(self, **changes: Any) -> DeadlineKeeper:
        """A keeper of the test's own over the same books, handed the lock and the pricing the account's own has."""
        return DeadlineKeeper(**{
            "repo": self.repo, "trade": self.venue, "intake": self.clerk.intake,
            "pricing": self.clerk.recovery_pricing, "off_loop": run_inline, **changes,
        })

    async def pass_(self) -> AccountReconciliationResult:
        """One reconciliation pass of the account, as its sweep runs it."""
        return await reconcile_account(
            self.repo, read=self.account, trade=self.venue, intake=self.clerk.intake,
            pricing=self.clerk.recovery_pricing,
        )

    async def stream_fill(self, order_ref: str, *, contracts: int, filled_in_all: int, price: str, execution_id: str) -> None:
        """The venue fills ``contracts`` more at ``price`` and Alpaca's stream delivers that one execution."""
        self.venue.option_order_fills(order_ref, contracts=filled_in_all, average_price=price)
        await self.stream_execution(order_ref, contracts=contracts, price=price, execution_id=execution_id)

    async def stream_execution(self, order_ref: str, *, contracts: int, price: str, execution_id: str) -> Any:
        """Alpaca's stream delivers one execution of an order the venue has already filled that much of."""
        frame = alpaca_adapter.from_alpaca_order(self.venue.option_orders[order_ref])
        sink = SqliteTradeUpdateEvidenceSink(repo=self.repo, intake=self.clerk.intake, reconciler=self.clerk)
        return await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=BrokerOrderEvent(
                event_type="fill" if frame.status == "filled" else "partial_fill", occurred_at_ms=self.clock(),
                price=float(price), quantity=contracts, execution_id=execution_id,
            ),
            event_key=f"execution:{execution_id}", order=frame, recovery_source=None, recovery_window_limit=None,
        )

    def frame_without_its_execution(self, order_ref: str) -> None:
        """The stream says where the order stands and how much has filled; the execution itself was lost."""
        order = self.repo.order(order_ref)
        assert order is not None
        fold_order_acknowledgement(
            self.repo, effect_operation_id=order.effect_operation_id,
            order=alpaca_adapter.from_alpaca_order(self.venue.option_orders[order_ref]),
        )

    def outcome(self, order_ref: str) -> EntryOutcome:
        return entry_outcome(self.repo, order_ref)

    def order_state(self, order_ref: str) -> str | None:
        order = self.repo.order(order_ref)
        assert order is not None
        return order.broker_state

    def enter_state(self, order_ref: str) -> str:
        order = self.repo.order(order_ref)
        assert order is not None
        effect = self.repo.effect_operation(order.effect_operation_id)
        assert effect is not None
        return effect.state

    def held(self, sid: str = "a") -> dict[str, float]:
        return {
            symbol: quantity
            for symbol, quantity in self.repo.attributed_positions_for_strategy(sid).items()
            if quantity
        }

    def recorded(self, order_ref: str, kind: str) -> list[dict[str, Any]]:
        """Every record of ``kind`` written for the order, oldest first."""
        return [row for row in self.repo.transitions_for_order(order_ref) if row["transition_kind"] == kind]

    def cancel_requests(self, order_ref: str) -> list[tuple[str, int]]:
        """Each time the Clerk recorded that it was asking for a cancel: why, and when."""
        return [(row["summary_code"], row["recorded_at_ms"]) for row in self.recorded(order_ref, "ORDER_CANCEL_REQUESTED")]

    def set_aside_cents(self, sid: str = "a") -> int:
        """What the bot's entry orders still claim of its budget: their unfilled part at its limit, with its fees."""
        from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims

        with self.repo.write_fence() as conn:
            claims = [
                claim for claim in entry_cash_claims(conn, seen_before_ms=self.clock() + 1)
                if claim.strategy_instance_id == sid
            ]
        return sum(round((claim.unfilled_cost + claim.unfilled_fee) * 100) for claim in claims)

    def log(self) -> list[tuple[str, str, str]]:
        """The whole custody log as (kind, summary, facts): what a pass or a keeper's round wrote."""
        with self.repo.write_fence() as conn:
            rows = conn.execute(
                "SELECT transition_kind, summary_code, facts_json FROM custody_transitions ORDER BY sequence"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]


def _compose(
    repo: ClerkSqliteRepository, clock: _TestClock, venue: _FakeTradePort, account: PaperAccountRead
) -> tuple[LiveEnvelopeSync, DeadlineKeeper, SqliteAlpacaClerkFacade]:
    """A Paper account's Clerk over ``repo``: its account reading, its deadline keeper, its facade."""

    async def pause(seconds: float) -> None:
        clock.advance(round(seconds * 1000))

    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    sync = LiveEnvelopeSync(repo=repo, read=account, envelope=gate, sleep=pause)
    # As the account's own composition builds them: the nudge, the facade with it, then the keeper whole.
    nudge = Nudge()
    clerk = SqliteAlpacaClerkFacade(
        repo=repo, read=account, trade=venue, account_mode="paper", live_envelope=gate,
        entry_reading=sync.read_for_entry, entry_deadline_nudge=nudge,
    )
    keeper = DeadlineKeeper(
        repo=repo, trade=venue, intake=clerk.intake, pricing=clerk.recovery_pricing, nudge=nudge, off_loop=run_inline
    )
    return sync, keeper, clerk


@pytest.fixture
async def paper(tmp_path: Path) -> AsyncIterator[PaperOptions]:
    """A Paper account at noon on a trading day, with two bots of $500.00 each and nothing held."""
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    repo = _new_budget_repo(tmp_path)
    _deploy(repo, "a", 50_000)
    _deploy(repo, "b", 50_000)
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    venue = _FakeTradePort()
    venue.venue_clock = clock
    account = PaperAccountRead(venue)
    sync, keeper, clerk = _compose(repo, clock, venue, account)
    assert await sync.tick() == "observed"
    clock.advance(1_000)
    opened = PaperOptions(
        repo=repo, clock=clock, clerk=clerk, venue=venue, account=account, keeper=keeper, sync=sync,
        artifacts_root=tmp_path,
    )
    try:
        yield opened
    finally:
        await opened.keeper.stop()
        await opened.sync.stop()
        await opened.clerk.drain_effects()
        opened.repo.close()
        market_liveness.reset_market_liveness_store_for_testing()


def patiently(account: PaperOptions) -> Callable[[float], Awaitable[None]]:
    """A pause that moves the test's clock: what the Clerk's wait for an outcome sleeps on in these tests."""

    async def pause(seconds: float) -> None:
        account.wait(round(seconds * 1000))

    return pause

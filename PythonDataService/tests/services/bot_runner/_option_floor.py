"""One Clerk process with an option bot in a Dry Run, for the tests that drive it through the real runner.

What is real: the runner (``run_dry_run_bot``), the IBKR supervisor as the
Clerk's process builds it (``AlpacaMarketLivenessConsumer.for_alpaca``), the
resolver, the entry search lane, the Clerk's keeping of quotes for what its
bots hold, and the Dry Run's own SQLite Clerk with its simulated broker.

What stands in: the IBKR connection (the ``Gateway`` of the option quote
tests, which lists a chain and delivers the ticks a test sends), the bar feed
(a script), the reader of the rate and dividends, and the search's worker
process (the lane searches here, through the pickling a worker's pipe does;
one test hands in a real worker).

**One clock.** Every part reads ``now_ms_utc``, as in production, and the
floor moves it. Time passes in steps, with the Clerk's hold on its account
renewed at each as its heartbeat renews it. A bar is delivered a moment after
it closes, with the quotes IBKR would have sent by then.

**One decision at a time.** The live stream is read ahead of the strategy, so
the script hands over a bar only once the runner has finished with every
decision before it.

**The keeping is waited for.** The supervisor's pass begins a round of the
Clerk's keeping and goes on without it. The floor waits for each round, so a
test reads the books and the kept quotes as that pass left them. The books
are read after a pass, so what they say is asked for from the next pass on.
"""

from __future__ import annotations

import asyncio
import json
import pickle
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, get_clerk_runtime
from app.broker.alpaca.clerk.held_option_quotes import get_held_option_quotes
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOptionFill, SynthesizedOrderLedger
from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.market_liveness import AlpacaMarketLivenessConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import OrderSide
from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar
from app.options.contract import OptionContract
from app.options.resolver import Resolution
from app.schemas.action_plan import ActionPlan
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import option_search
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_option_run import OptionMarket
from app.services.market_liveness import MarketLivenessStore
from app.services.option_model_inputs import ModelInputsRead
from app.services.option_search import OptionSearchLane, SearchAsk
from app.services.option_watch import OptionWatchList
from app.utils import timestamps
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

# The stand-in Gateway, and the resolver's own scenes: SPY at 585 on a dollar-wide ladder, a Tuesday morning.
from tests.broker.ibkr import test_option_quotes as ibkr_feed
from tests.options import test_resolver as scenes

MINUTE = 60_000
TUESDAY, WEDNESDAY, FRIDAY = scenes._TUESDAY, scenes._WEDNESDAY, scenes._FRIDAY
# The second of two green bars closes at 10:31: the ENTER. The EXIT is decided three bars later.
ENTER_MS = scenes._at(TUESDAY, 10, 31)
FIRST_GREEN_MS = ENTER_MS - MINUTE
EXIT_MS = ENTER_MS + 3 * MINUTE
# How long after its close a bar is delivered, unless a step says otherwise.
DELIVERY_MS = 300

SID = "dry-run-buys-a-call"
RUN = "run-dry"
ACCOUNT = synthetic_account_id_for_strategy(SID)
LEG = "only"
CALL_585 = scenes._contract("call", 585)

KNOWN = ModelInputsRead(
    rate=0.045, rate_source="test", rate_is_default=False, dividends=(), dividends_source="test: none declared",
    dividends_read_ms=ENTER_MS,
)
DIVIDENDS_UNKNOWN = replace(KNOWN, dividends=None, dividends_source="test: the lookup failed")


def bar(end_ms: int, *, open_price: str, close_price: str, symbol: str = "SPY", volume: int = 500) -> MarketDataBar:
    low, high = sorted((Decimal(open_price), Decimal(close_price)))
    return MarketDataBar(
        symbol=symbol, start_ms=end_ms - MINUTE, end_ms=end_ms, open=Decimal(open_price), high=high, low=low,
        close=Decimal(close_price), volume=volume, fetched_at_ms=end_ms + 100, feed_id="fake", session_phase="RTH",
    )


def green(end_ms: int, close: str = "585.00", **more: Any) -> MarketDataBar:
    """A bar that closed above its open: the strategy enters on the second of two."""
    return bar(end_ms, open_price=str(Decimal(close) - Decimal("0.50")), close_price=close, **more)


def red(end_ms: int, close: str = "584.60", **more: Any) -> MarketDataBar:
    return bar(end_ms, open_price=str(Decimal(close) + Decimal("0.50")), close_price=close, **more)


def one_call(strike: dict[str, Any] | None = None, *, underlying: str = "SPY") -> ActionPlan:
    """One bought call expiring on Friday: at the money unless ``strike`` says otherwise."""
    leg = scenes._leg(LEG, "call", strike or {"selector": "atm"})
    leg["instrument"] = {"kind": "option", "underlying": underlying}
    return scenes._plan(leg)


def binding_for(plan: ActionPlan, *, listens_to: str = "SPY", budget_cents: int = 100_000) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID, strategy_key="deployment_validation", broker="alpaca", symbol=listens_to,
        mode="dry_run", quantity=1, action_plan=plan, run_id=RUN, created_at_ms=0, sealed_account_id=ACCOUNT,
        exit_terms=DEPLOY_EXIT_TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=budget_cents, risk_revision=0, actor="owner", request_fingerprint="reviewed",
            world="synthetic",
        ),
    )


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class SearchesHere(Executor):
    """A search worker that runs each search here, at once, through the pickling a worker process's pipe does.

    It keeps what each search was asked, and runs ``during`` while a search
    is under way: what else happens in the time a search takes.
    """

    def __init__(self) -> None:
        self.asked: list[SearchAsk] = []
        self.during: Callable[[], None] | None = None

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        answered: Future[Any] = Future()
        try:
            if fn is option_search.search:
                self.asked.append(args[0])
            called, with_args, with_kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            answer = pickle.loads(pickle.dumps(called(*with_args, **with_kwargs)))
            if fn is option_search.search and self.during is not None:
                self.during()
            answered.set_result(answer)
        except Exception as failed:
            answered.set_exception(failed)
        return answered


class SearchesLate(SearchesHere):
    """A search worker that is busy: it takes each search and answers none until it is let go."""

    def __init__(self) -> None:
        super().__init__()
        self.unanswered: list[Future[Any]] = []

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        if fn is not option_search.search:
            return super().submit(fn, *args, **kwargs)
        self.asked.append(args[0])
        self.unanswered.append(Future())
        return self.unanswered[-1]

    def let_go(self) -> None:
        for search in self.unanswered:
            search.cancel()


class AnswersWith(SearchesHere):
    """A search worker whose every answer is changed on its way back, as a worker of another build's might be."""

    def __init__(self, change: Callable[[Any], Any]) -> None:
        super().__init__()
        self._change = change

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        answered = super().submit(fn, *args, **kwargs)
        if fn is not option_search.search or answered.exception() is not None:
            return answered
        changed: Future[Any] = Future()
        changed.set_result(self._change(answered.result()))
        return changed


@dataclass(frozen=True)
class Step:
    """One scripted delivery: a bar, and what the floor does just before it is handed over."""

    bar: MarketDataBar
    # Takes the place of the usual arrival: time passes to the bar's delivery and IBKR quotes everything afresh.
    before: Callable[[Floor], Awaitable[None]] | None = None
    delay_ms: int = DELIVERY_MS


class ScriptedFeed:
    """Serve the script's bars, each only once the runner has finished with every decision before it."""

    feed_id = "fake"

    def __init__(self, floor: Floor, script: list[Step], *, signal: str) -> None:
        self._floor, self._script, self._signal, self._turn = floor, script, signal, 0
        self.symbols_asked_for: list[str] = []

    async def stream_bars(
        self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None
    ) -> AsyncIterator[MarketDataBar]:
        del use_rth, continuity
        self.symbols_asked_for.append(symbol)
        while any(step.bar.symbol == symbol for step in self._script[self._turn:]):
            step = self._script[self._turn]
            if step.bar.symbol != symbol:
                await asyncio.sleep(0)
                continue
            decided_before = sum(earlier.bar.symbol == self._signal for earlier in self._script[: self._turn])
            await self._floor.runner_has_finished(decided_before)
            if step.before is not None:
                await step.before(self._floor)
            else:
                await self._floor.bar_arrives(step.bar.end_ms + step.delay_ms)
            self._turn += 1
            yield step.bar
        if symbol != self._signal:
            await asyncio.Event().wait()
        if self._floor.a_pass_before_the_stream_ends:
            # The supervisor passes once a second, and a stream does not end in the second of its last
            # decision: the books are read once more while the run is still alive.
            await self._floor.runner_has_finished(sum(step.bar.symbol == self._signal for step in self._script))
            await self._floor.supervisor_passes(1)

    async def recent_closed_bars(self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        return []

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True, stale=False, last_bar_ms=None, reason="", active_subscription_count=0,
            observed_at_ms=timestamps.now_ms_utc(),
        )


@dataclass
class Ran:
    """What one run left behind."""

    feed: ScriptedFeed
    journal: list[DryRunActivity]
    receipts: list[tuple[str, dict[str, Any]]]

    def blocked(self) -> list[tuple[str, str]]:
        """Each refused decision's reason code and its plain words, oldest first."""
        return [
            (facts["reason_code"], facts.get("refusal_reason", ""))
            for outcome, facts in self.receipts
            if outcome == "blocked"
        ]

    def outcomes(self) -> list[tuple[str, int]]:
        """Each decision's outcome and the close of the bar it was decided on, oldest first."""
        return [(outcome, facts["decision_bar_close_ms"]) for outcome, facts in self.receipts]


class Floor:
    """The Clerk's process: its IBKR connection and supervisor, one Dry Run account with its Clerk, and the clock."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        plan: ActionPlan | None = None,
        listens_to: str = "SPY",
        line_budget: int = 40,
        reach: int = 2,
        starts_at_ms: int = FIRST_GREEN_MS - MINUTE,
        watch: OptionWatchList | None = None,
        budget_cents: int = 100_000,
    ) -> None:
        self.tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self.clock = Clock(starts_at_ms)
        # Every part reads the wall clock through ``now_ms_utc``; the lateness gate reads its own import of it.
        monkeypatch.setattr(timestamps, "time", SimpleNamespace(time=lambda: self.clock.value / 1000))
        monkeypatch.setattr(feed_continuity_policy, "now_ms_utc", self.clock)
        self._pace_the_runner(monkeypatch)

        self.ibkr = ibkr_feed.Gateway(monkeypatch)
        monkeypatch.setattr("app.broker.ibkr.client._client", self.ibkr.client)
        monkeypatch.setenv("IBKR_OPTION_LINE_BUDGET", str(line_budget))
        monkeypatch.setenv("IBKR_OPTION_WINDOW_STRIKES_EACH_SIDE", str(reach))
        self.line_budget, self.reach = line_budget, reach
        self.watch = watch or OptionWatchList()
        self.held = get_held_option_quotes()
        # Where the supervisor's stock evidence is published: what a stock bot's entry is admitted on.
        self.store = MarketLivenessStore()
        # The supervisor exactly as a Clerk's process builds it, on the stand-in connection.
        self.consumer = AlpacaMarketLivenessConsumer.for_alpaca(
            read=object(),  # type: ignore[arg-type]
            settings=AlpacaSettings(api_key_id="paper-key", api_secret_key="paper-secret", mode="paper"),
            store=self.store,
            journal=CaptureJournal(capture_dir=tmp_path / "capture"),
            option_watch=self.watch,
            held_option_quotes=self.held,
        )
        self.supervisor = self.consumer.option_quotes
        # None: this process has no IBKR quote source for options.
        self.quote_source: Callable[[], Any] = lambda: self.supervisor

        self.worker = SearchesHere()
        self.lane = OptionSearchLane("entry", executor=lambda: self.worker)
        self.inputs: ModelInputsRead = KNOWN
        # Runs while the rate and dividends are read, and is told the moment they were asked for: what
        # that read costs in time. A read made in the background as a bar closes asks as of the bar's close.
        self.while_inputs_are_read: Callable[[int], None] | None = None
        # The moment each read of the rate and dividends asked as of, in the order they were made.
        self.inputs_read_as_of: list[int] = []
        # A two-cent market around the model's own price for each strike, unless a test sets one: by strike.
        self.markets: dict[int, tuple[str, str]] = {585: ("4.08", "4.12")}
        self.spy_quote: tuple[float, float] = (585.10, 585.12)
        # Whether every open option line ticks afresh at each step of time passing, as a busy contract's does.
        self.option_lines_tick = False
        # IBKR answers a new request for a line with the contract's current prices. False: its lines stay silent.
        self.new_lines_are_answered = True
        self._lines_answered: set[str] = set()
        # False: the Signal stream ends in the very second of its last decision, before any pass.
        self.a_pass_before_the_stream_ends = True

        self.binding = binding_for(plan or one_call(), listens_to=listens_to, budget_cents=budget_cents)
        self.running = True
        self.authority = SyntheticBindingAuthority(
            binding=self.binding, artifacts_root=tmp_path,
            lifecycle_repo_for=lambda _sid: pytest.fail("a Dry Run's entry never reads the runner's lifecycle state"),
            runtime_in_use=lambda _sid: self.running, brokers={}, clock=timestamps.now_ms_utc,
        )
        self._evaluations_finished = 0
        # The feed of the run last started, there for a test whose run is refused.
        self.feed: ScriptedFeed | None = None
        # Each effect the runner asked the Clerk for, by its purpose, in order. A decision the runner
        # itself refused never reaches the Clerk, and is not here.
        self.asked_of_the_clerk: list[str] = []

    # ---- pacing -----------------------------------------------------------------

    def _pace_the_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Count each evaluation the runner has finished with: it asks for the next one only then."""
        real = bot_trade_strategy.strategy_evaluations

        async def counted(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            async for evaluation in real(*args, **kwargs):
                yield evaluation
                self._evaluations_finished += 1

        monkeypatch.setattr(bot_trade_strategy, "strategy_evaluations", counted)

    async def runner_has_finished(self, evaluations: int) -> None:
        waited = 0
        while self._evaluations_finished < evaluations:
            await asyncio.sleep(0.002)
            waited += 1
            assert waited < 10_000, f"the runner never finished decision {evaluations}"

    # ---- the account ------------------------------------------------------------

    async def open(self) -> Floor:
        """Compose the Dry Run's Clerk and start the bot's run on it."""
        await self.authority.ensure_recoverable()
        await self.clerk.register_strategy_run(self.binding)
        self._note_what_the_clerk_is_asked()
        return self

    def _note_what_the_clerk_is_asked(self) -> None:
        clerk, ask = self.clerk, self.clerk.execute_for_instance

        async def noted(**effect: Any) -> Any:
            self.asked_of_the_clerk.append(effect["purpose"].value)
            return await ask(**effect)

        self._monkeypatch.setattr(clerk, "execute_for_instance", noted)

    @property
    def runtime(self) -> ActiveClerkRuntime | None:
        return get_clerk_runtime(ACCOUNT)

    @property
    def clerk(self) -> Any:
        runtime = self.runtime
        assert runtime is not None and runtime.clerk is not None, "the Dry Run's Clerk is not composed"
        return runtime.clerk

    @property
    def repo(self) -> Any:
        return self.clerk.repository

    def market(self, **changes: Any) -> OptionMarket:
        """What the bot's runner reads its market through on this floor."""
        market = OptionMarket(
            quotes=lambda: self.quote_source(),
            watch=self.watch,
            entry_searches=lambda: self.lane,
            model_inputs=self._read_inputs,
            line_budget=self.line_budget,
            strikes_each_side=self.reach,
        )
        return replace(market, **changes)

    def _read_inputs(self, _underlying: str, at_ms: int, _latest_expiry: date) -> ModelInputsRead:
        self.inputs_read_as_of.append(at_ms)
        if self.while_inputs_are_read is not None:
            self.while_inputs_are_read(at_ms)
        return self.inputs

    # ---- time, and what happens as it passes -------------------------------------

    async def time_passes_to(self, at_ms: int) -> None:
        """Move the clock a quarter of a minute at a time. At each step the Clerk's hold on its account is
        renewed as its heartbeat renews it, SPY ticks as it does all day, and the supervisor makes a pass."""
        while self.clock.value < at_ms:
            self.clock.value = min(at_ms, self.clock.value + 15_000)
            runtime = self.runtime
            if runtime is not None and runtime.sqlite_repository is not None:
                runtime.sqlite_repository.renew_execution_lease()
            if "SPY" in self.ibkr.lines:
                self.spy()
            if self.option_lines_tick:
                self.quote_every_line()
            await self.supervisor_passes(1)

    async def hours_pass_to(self, at_ms: int) -> None:
        """Move the clock in one stride over hours nothing is watched through. The Clerk's hold on its
        account is taken up again where it was left, and the supervisor finds its lines as they stand then."""
        assert at_ms >= self.clock.value
        self.clock.value = at_ms
        runtime = self.runtime
        if runtime is not None and runtime.sqlite_repository is not None:
            runtime.sqlite_repository.revive_execution_lease()
        await self.supervisor_passes()
        if "SPY" in self.ibkr.lines:
            self.spy()
        await self.supervisor_passes()

    async def supervisor_passes(self, passes: int = 3) -> None:
        """The supervisor's own pass, as the consumer's loop makes it once a second, with nothing swallowed:
        the lines are brought to what is wanted. The pass begins a round of the Clerk's keeping and does not
        wait for it; the floor does, so that what a test looks at next is there: the books read, and the
        held contracts' quotes kept."""
        for _ in range(passes):
            await self.consumer._status_snapshot_source()  # type: ignore[misc]
            await self.held.settled()
            for _ in range(12):
                await asyncio.sleep(0)
            opened = self.ibkr.open_option_lines()
            if self.new_lines_are_answered:
                for name in sorted(opened - self._lines_answered):
                    self._quote_line(name)
            self._lines_answered = opened

    async def account_is_read(self) -> None:
        """One tick of the account's own reading cadence, which an entry is admitted on."""
        runtime = self.runtime
        if runtime is not None and runtime.envelope_sync is not None:
            await runtime.envelope_sync.tick()

    def spy(self) -> None:
        bid, ask = self.spy_quote
        self.ibkr.now = self.clock.value
        self.ibkr.tick("SPY", bid=bid, ask=ask)

    def quote(self, strike: int, bid: str, ask: str, *, right: str = "call", expiry: date = FRIDAY) -> None:
        """A tick of both sides of one contract's line, now."""
        self.ibkr.now = self.clock.value
        self.ibkr.tick(scenes._contract(right, strike, expiry).occ_symbol, bid=float(bid), ask=float(ask))

    def quote_every_line(self) -> None:
        """A fresh quote on every open option line: the test's own market for a strike, else the scene's."""
        for name in sorted(self.ibkr.open_option_lines()):
            self._quote_line(name)

    def _quote_line(self, name: str) -> None:
        contract = OptionContract.from_occ_symbol(name)
        market = self.markets.get(int(contract.strike)) if contract.right == "call" else None
        if market is None:
            held = _scene_quotes()[name].quote
            market = (str(held.bid.price), str(held.ask.price))
        self.ibkr.now = self.clock.value
        self.ibkr.tick(name, bid=float(market[0]), ask=float(market[1]))

    async def bar_arrives(self, at_ms: int, *, quotes: bool = True) -> None:
        """Everything that is true by the time a bar is delivered at ``at_ms``.

        Time has passed. The supervisor has listed what is wanted, quoted
        the underlying and opened the lines; IBKR has just quoted each of
        them; the Clerk has kept the quotes of what it holds and read its
        account.
        """
        await self.time_passes_to(at_ms)
        await self.supervisor_passes()
        self.spy()
        await self.supervisor_passes()
        if quotes:
            self.quote_every_line()
        await self.supervisor_passes(1)
        await self.account_is_read()

    # ---- a run ------------------------------------------------------------------

    async def run(
        self, script: list[Step], *, pricing_bar_wait_ms: int = 20, the_process_own_market: bool = False
    ) -> Ran:
        """Run the bot over ``script`` to the end of its Signal stream.

        ``the_process_own_market`` hands the runner no market, as production
        does: it reads the process's own supervisor, list, lane and settings.
        """
        feed = self.feed = ScriptedFeed(self, script, signal=self.binding.symbol)
        instance_dir = self.tmp_path / "instance"
        bars = self.authority.source_bars()
        try:
            await asyncio.wait_for(
                bot_trade_strategy.run_dry_run_bot(
                    self.binding, feed, DryRunActivityJournal(instance_dir), source_bars=bars, pricing_feed=feed,
                    pricing_bar_wait_ms=pricing_bar_wait_ms,
                    option_market=None if the_process_own_market else self.market(),
                ),
                timeout=120,
            )
        finally:
            bars.close()
        return Ran(feed=feed, journal=self.journal(), receipts=self.receipts())

    def journal(self) -> list[DryRunActivity]:
        return DryRunActivityJournal(self.tmp_path / "instance").tail(50)

    def receipts(self) -> list[tuple[str, dict[str, Any]]]:
        oldest_first = self.repo.decision_receipt_tail(strategy_instance_id=SID, limit=50)
        return [(receipt.outcome, json.loads(receipt.facts_json)) for receipt in oldest_first]

    # ---- what the books and the simulation say ------------------------------------

    def holds(self) -> dict[str, float]:
        return {
            symbol: quantity for symbol, quantity in self.repo.attributed_positions_for_strategy(SID).items() if quantity
        }

    def fills(self) -> list[tuple[str, str, float, float, int | None, Decimal]]:
        """Every fill on the books: its symbol, side, contracts, premium, multiplier and the cash it moved."""
        with self.repo._write_lock:
            records = effective_fill_records(self.repo._conn, account_id=ACCOUNT)
        return [
            (fill.symbol, fill.side.value, fill.quantity, fill.fill_price, fill.multiplier, fill.exact_notional)
            for fill in records
        ]

    def orders_on_the_books(self) -> int:
        with self.repo._write_lock:
            return self.repo._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def simulated_fills(self) -> list[SynthesizedOptionFill]:
        """How the simulation priced each option order it answered, in the order it answered them."""
        return sorted(self.clerk.synthesized_option_fills().values(), key=lambda fill: fill.filled_at_ms or 0)

    def transitions(self, kind: str) -> int:
        with self.repo._write_lock:
            return self.repo._conn.execute(
                "SELECT COUNT(*) FROM custody_transitions WHERE transition_kind = ?", (kind,)
            ).fetchone()[0]

    def entry_as_the_clerk_kept_it(self) -> Any:
        """The bot's one option entry, as its acceptance recorded what it was admitted on."""
        with self.repo._write_lock:
            (order_ref,) = self.repo._conn.execute("SELECT order_ref FROM orders WHERE role = 'ENTRY'").fetchone()
        return option_entry_of_order(self.repo, order_ref)

    def kept_quotes(self) -> KeptOptionQuotes:
        return KeptOptionQuotes.of_account(artifacts_root=self.tmp_path, account_id=ACCOUNT)

    def sales_the_simulation_made(self) -> list[SynthesizedOptionFill]:
        """Every sale in the simulation's own ledger, read from its file: there with the Clerk's runtime closed."""
        bars = self.authority.source_bars()
        try:
            ledger = SynthesizedOrderLedger.beside_source_bars(account_id=ACCOUNT, source_bars=bars, label="test")
            fills = [fill for fill in ledger.option_fills().values() if fill.request.side is OrderSide.SELL]
        finally:
            bars.close()
        return sorted((fill for fill in fills if fill.fill_price is not None), key=lambda fill: fill.filled_at_ms or 0)

    async def stop_is_pressed(self) -> None:
        """The owner's Stop: the run is ended on the Clerk's books, what it left is closed, and its authority
        is released, as the registry does once the runner's task is gone."""
        self.running = False
        await self.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN)
        await self.authority.release_after_run_end()

    def what_the_resolver_picks(self, search: int = -1) -> Resolution:
        """The resolver's own answer, worked out here, from exactly what one of the run's searches was handed."""
        asked = self.worker.asked[search]
        return scenes.resolve_contracts(
            asked.plan, chain=asked.chain, underlying=asked.underlying, feed=asked.feed, model=asked.model,
            limits=asked.limits, decision_bar_close_ms=asked.decision_bar_close_ms, selection_ms=asked.selection_ms,
        )


_QUOTES: dict[str, Any] = {}


def _scene_quotes() -> dict[str, Any]:
    """The resolver's own scene, worked out once: a two-cent market around the model's price for each strike."""
    if not _QUOTES:
        _QUOTES.update(scenes._scene().chain.watched)
    return _QUOTES

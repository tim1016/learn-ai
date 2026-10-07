"""A bot that trades a different asset prices each decision from that asset's own minute (#2912).

The bot listens to SPY and trades QQQ. Its two bar streams deliver
independently, so every test scripts the order the two symbols' bars arrive in
and proves what the Clerk was handed, or why nothing was sent.

``deployment_validation`` decides on one-minute bars: ENTER on the second of
two green bars, EXIT three bars later.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOrderLedger
from app.marketdata.feed import (
    DELIVERY_ALLOWANCE_MS,
    ContinuityPolicy,
    FeedHealth,
    MarketDataBar,
    MarketDataFeedError,
)
from app.schemas.action_plan import ActionPlan
from app.schemas.deployment_budget import DeployBudgetConsent
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services import market_liveness
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_dry_run import DryRunActivityJournal
from app.services.market_liveness import compose_market_liveness
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeClerk
from tests._helpers.bot_runner.market import patch_wall_clock_to_the_fed_bar
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

from ._support import _SESSION_OPEN_MS, _green_bar, _red_bar

SIGNAL = "SPY"
TRADED = "QQQ"

# 09:59 and 10:00 ET: the second green bar is the ENTER decision. The EXIT is
# decided three bars later, on the 10:03 bar.
_FIRST_GREEN_MS = _SESSION_OPEN_MS + 29 * 60_000
_ENTER_MS = _SESSION_OPEN_MS + 30 * 60_000
_AFTER_ENTER_MS = _ENTER_MS + 60_000
_EXIT_MS = _ENTER_MS + 3 * 60_000


def _qqq(end_ms: int, *, close: str = "40.10", volume: int = 500) -> MarketDataBar:
    price = Decimal(close)
    return MarketDataBar(
        symbol=TRADED, start_ms=end_ms - 60_000, end_ms=end_ms, open=price, high=price, low=price,
        close=price, volume=volume, fetched_at_ms=end_ms, feed_id="fake", session_phase="RTH",
    )


@dataclass(frozen=True)
class _Step:
    """One scripted delivery: a bar, or a feed failure on that bar's symbol."""

    bar: MarketDataBar
    after_s: float = 0.0
    fails_with: MarketDataFeedError | None = None
    # Run as the bar is handed over: what else is true by the time it arrives.
    on_delivery: Callable[[], None] | None = None


class _ScriptedFeed:
    """Serve two symbols' bars in one scripted order.

    Each stream waits for its turn, so a script says which symbol's bar
    arrives first. A step is claimed before its bar is handed over: while one
    consumer is still working on a bar, the next step is already free to
    arrive. The signal's stream ends when its steps run out; the traded
    asset's stays open, as a live stream would.
    """

    feed_id = "fake"

    def __init__(self, script: list[_Step]) -> None:
        self._script = script
        self._turn = 0
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
            if step.after_s:
                await asyncio.sleep(step.after_s)
            self._turn += 1
            if step.fails_with is not None:
                raise step.fails_with
            if step.on_delivery is not None:
                step.on_delivery()
            yield step.bar
        if symbol != SIGNAL:
            await asyncio.Event().wait()

    async def recent_closed_bars(
        self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5
    ) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        return []

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True, stale=False, last_bar_ms=None, reason="", active_subscription_count=0,
            observed_at_ms=now_ms_utc(),
        )


def _binding(*, trades: str = TRADED) -> BrokerBotBinding:
    return BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=_SID, strategy_key="deployment_validation",
        broker="alpaca", symbol=SIGNAL, use_rth=True, mode="trade", quantity=1, carryover_policy="FORBID",
        sealed_account_id="PA-TEST", action_plan=alpaca_v1_action_plan(trades), run_id="run-current",
        created_at_ms=_T0,
    )


@dataclass
class _Ran:
    feed: _ScriptedFeed
    calls: list[dict[str, Any]]
    receipts: list[tuple[str, dict[str, Any]]]
    ledger: SourceBarLedger
    took_s: float

    def blocked(self) -> list[tuple[str, str]]:
        """Each refused decision's reason code and its plain words."""
        return [
            (facts["reason_code"], facts.get("refusal_reason", ""))
            for outcome, facts in self.receipts
            if outcome == "blocked"
        ]


@pytest.fixture
async def run(tmp_path: Path) -> AsyncIterator[Any]:
    """Run the trade bot over a script; the ledger stays open for the test to read."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol=TRADED, config_hash="config-1")
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-TEST")
    clerk = _FakeClerk(repository=repo)
    set_alpaca_clerk(clerk)

    async def _run(
        script: list[_Step],
        *,
        wait_ms: int | None = 20,
        binding: BrokerBotBinding | None = None,
        synthesized: bool = False,
    ) -> _Ran:
        # A Live Shadow's Clerk fills its own orders, and says so.
        clerk.binds_decision_bar = synthesized
        feed = _ScriptedFeed(script)
        started = time.monotonic()
        await bot_trade_strategy.run_trade_bot(
            binding or _binding(), feed, source_bars=ledger, pricing_feed=feed, pricing_bar_wait_ms=wait_ms
        )
        took_s = time.monotonic() - started
        receipts = [
            (receipt.outcome, json.loads(receipt.facts_json))
            for receipt in repo.decision_receipt_tail(strategy_instance_id=_SID, limit=50)
        ]
        return _Ran(feed=feed, calls=clerk.calls, receipts=receipts, ledger=ledger, took_s=took_s)

    try:
        yield _run
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()


async def test_a_price_that_arrived_before_the_decision_bar_prices_the_entry(run: Any) -> None:
    ran = await run([_Step(_green_bar(_FIRST_GREEN_MS)), _Step(_qqq(_ENTER_MS)), _Step(_green_bar(_ENTER_MS))])

    (call,) = ran.calls
    assert call["purpose"] == "ENTER"
    assert (call["retained_source_bar"].symbol, call["retained_source_bar"].end_ms) == (SIGNAL, _ENTER_MS)
    priced_by = call["pricing_bar"]
    assert (priced_by.symbol, priced_by.end_ms, priced_by.close) == (TRADED, _ENTER_MS, Decimal("40.10"))
    assert ran.blocked() == []


async def test_an_entry_waits_for_a_price_that_arrives_inside_the_wait(run: Any) -> None:
    ran = await run(
        [_Step(_green_bar(_FIRST_GREEN_MS)), _Step(_green_bar(_ENTER_MS)), _Step(_qqq(_ENTER_MS), after_s=0.02)],
        wait_ms=5_000,
    )

    (call,) = ran.calls
    assert (call["pricing_bar"].symbol, call["pricing_bar"].end_ms) == (TRADED, _ENTER_MS)


async def test_a_price_that_arrives_after_the_wait_is_kept_and_never_used(run: Any) -> None:
    ran = await run(
        [
            _Step(_green_bar(_FIRST_GREEN_MS)),
            _Step(_green_bar(_ENTER_MS)),
            _Step(_qqq(_ENTER_MS), after_s=0.05),
            _Step(_red_bar(_AFTER_ENTER_MS)),
        ],
        # No wait at all: the entry is refused before anything else can run,
        # so the price is late however loaded the machine is.
        wait_ms=0,
    )

    assert ran.calls == [], "the entry was dropped; the late price must not revive it"
    [(reason_code, why)] = ran.blocked()
    assert reason_code == "PRICING_BAR_LATE"
    assert TRADED in why
    assert ran.ledger.find_by_closed_end(provider="fake", symbol=TRADED, end_ms=_ENTER_MS) is not None


@pytest.mark.parametrize(
    ("prices_delivered", "reason_code"),
    [
        pytest.param([_Step(_qqq(_ENTER_MS, volume=0))], "PRICING_BAR_NO_TRADE", id="no-trade-in-that-minute"),
        pytest.param([], "PRICING_BAR_LATE", id="no-bar-at-all"),
        pytest.param(
            [_Step(_qqq(_ENTER_MS), fails_with=MarketDataFeedError("minute incomplete", reason="MINUTE_INCOMPLETE"))],
            "PRICING_FEED_UNHEALTHY",
            id="feed-unhealthy",
        ),
        pytest.param(
            [_Step(_qqq(_ENTER_MS, close="40.10")), _Step(_qqq(_ENTER_MS, close="41.00"))],
            "PRICING_BAR_CONFLICT",
            id="two-prices-for-one-minute",
        ),
    ],
)
async def test_each_reason_an_entry_has_no_price_is_recorded_as_itself(
    run: Any, prices_delivered: list[_Step], reason_code: str
) -> None:
    """A quiet minute, a slow feed, a broken feed and a contradicting feed are four different facts."""
    ran = await run([_Step(_green_bar(_FIRST_GREEN_MS)), *prices_delivered, _Step(_green_bar(_ENTER_MS))])

    assert ran.calls == []
    [(recorded, why)] = ran.blocked()
    assert recorded == reason_code
    assert TRADED in why and "No order was sent" in why


async def test_an_entry_never_waits_for_a_price_past_its_own_time_limit(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is on its last on-time instant. A minute's wait is cut to nothing, and the entry is dropped.

    Nothing else stops an entry going out after its time limit: once it has
    its price the Clerk does not look at the clock again.
    """
    patch_wall_clock_to_the_fed_bar(monkeypatch, decision_delay_ms=DELIVERY_ALLOWANCE_MS)

    ran = await run([_Step(_green_bar(_FIRST_GREEN_MS)), _Step(_green_bar(_ENTER_MS))], wait_ms=60_000)

    assert ran.took_s < 10, "the entry waited past its own time limit"
    assert ran.calls == []
    [(reason_code, _why)] = ran.blocked()
    assert reason_code == "PRICING_BAR_LATE"


async def test_the_wait_is_the_configured_one_when_a_run_is_given_none(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production wiring passes no wait: the setting decides it."""
    monkeypatch.setattr(bot_trade_strategy.settings, "PRICING_BAR_WAIT_MS", 0)

    ran = await run([_Step(_green_bar(_FIRST_GREEN_MS)), _Step(_green_bar(_ENTER_MS))], wait_ms=None)

    assert ran.took_s < 4, "the entry waited though the configured wait is zero"
    [(reason_code, _why)] = ran.blocked()
    assert reason_code == "PRICING_BAR_LATE"


async def test_an_entry_whose_price_arrives_as_its_time_runs_out_is_not_sent(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The price came inside the wait, and the wait ended a moment past the decision's time limit.

    Lateness is judged again once the price is in hand. Nothing after that
    looks at the clock before the order goes out.
    """
    time_is_up = False

    def now() -> int:
        return _ENTER_MS + (DELIVERY_ALLOWANCE_MS + 1 if time_is_up else 0)

    def run_out_the_clock() -> None:
        nonlocal time_is_up
        time_is_up = True

    monkeypatch.setattr(feed_continuity_policy, "now_ms_utc", now)

    ran = await run(
        [
            _Step(_green_bar(_FIRST_GREEN_MS)),
            _Step(_green_bar(_ENTER_MS)),
            _Step(_qqq(_ENTER_MS), after_s=0.02, on_delivery=run_out_the_clock),
        ],
        wait_ms=5_000,
    )

    assert ran.calls == []
    [(reason_code, _why)] = ran.blocked()
    assert reason_code == "DECISION_LATE"


def _halted(halted_symbol: str) -> Any:
    """Every symbol tradable but one, which is halted."""

    def fact(symbol: str, observed_at_ms: int) -> Any:
        return compose_market_liveness(
            symbol,
            now_ms=observed_at_ms,
            market_clock=MarketClockLivenessEvidence(
                state="OPEN", source="test.clock", observed_at_ms=observed_at_ms, vendor_timestamp_ms=observed_at_ms
            ),
            connected=True,
            connection_changed_at_ms=observed_at_ms,
            symbol_status=SymbolTradingStatusEvidence(
                symbol=symbol,
                state="HALTED" if symbol.upper() == halted_symbol else "TRADABLE",
                source="test.symbol-status",
                observed_at_ms=observed_at_ms,
                source_timestamp_ms=observed_at_ms,
            ),
        )

    return fact


async def test_an_entry_is_refused_while_the_traded_asset_is_halted_though_the_signal_is_live(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry needs both sides live: SPY to decide on, QQQ to send the order into."""
    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", _halted(TRADED))

    ran = await run([_Step(_green_bar(_FIRST_GREEN_MS)), _Step(_qqq(_ENTER_MS)), _Step(_green_bar(_ENTER_MS))])

    assert ran.calls == []
    [facts] = [facts for outcome, facts in ran.receipts if outcome == "blocked"]
    assert facts["market_liveness"]["symbol"] == TRADED
    assert facts["market_liveness"]["state"] == "HALTED"


async def test_trouble_on_the_price_stream_leaves_the_signal_running(run: Any) -> None:
    """The price feed fails; the signal's next bar is still decided, and the run ends on its own terms."""
    ran = await run(
        [
            _Step(_green_bar(_FIRST_GREEN_MS)),
            _Step(_qqq(_ENTER_MS), fails_with=MarketDataFeedError("minute incomplete", reason="MINUTE_INCOMPLETE")),
            _Step(_green_bar(_ENTER_MS)),
            _Step(_red_bar(_AFTER_ENTER_MS)),
        ]
    )

    outcomes = [(outcome, facts["decision_bar_close_ms"]) for outcome, facts in ran.receipts]
    assert ("blocked", _ENTER_MS) in outcomes
    assert ("no_action", _AFTER_ENTER_MS) in outcomes
    assert not any(outcome == "decision_bar_quarantined" for outcome, _ in outcomes)


@pytest.mark.parametrize("price_in_hand", [True, False], ids=["price-in-hand", "no-price"])
async def test_a_sell_never_waits_for_a_price_and_is_never_held_back(run: Any, price_in_hand: bool) -> None:
    ran = await run(
        [
            _Step(_green_bar(_FIRST_GREEN_MS)),
            _Step(_qqq(_ENTER_MS)),
            _Step(_green_bar(_ENTER_MS)),
            _Step(_red_bar(_ENTER_MS + 60_000)),
            _Step(_red_bar(_ENTER_MS + 120_000)),
            *([_Step(_qqq(_EXIT_MS, close="40.60"))] if price_in_hand else []),
            _Step(_red_bar(_EXIT_MS)),
        ],
        # A sell that waited would spend this whole budget before going out.
        wait_ms=60_000,
    )

    assert ran.took_s < 10, "a sell waited for a price"
    enter, sell = ran.calls
    assert (enter["purpose"], sell["purpose"]) == ("ENTER", "EXIT")
    assert sell["retained_source_bar"].end_ms == _EXIT_MS
    if price_in_hand:
        assert (sell["pricing_bar"].symbol, sell["pricing_bar"].close) == (TRADED, Decimal("40.60"))
    else:
        assert sell["pricing_bar"] is None


def _round_trip(*sell_minute_prices: _Step) -> list[_Step]:
    """ENTER at 10:00 with QQQ at 40.10, a QQQ trade at 10:01, EXIT decided on the 10:03 bar."""
    return [
        _Step(_green_bar(_FIRST_GREEN_MS)),
        _Step(_qqq(_ENTER_MS, close="40.10")),
        _Step(_green_bar(_ENTER_MS)),
        _Step(_qqq(_ENTER_MS + 60_000, close="40.30")),
        _Step(_red_bar(_ENTER_MS + 60_000)),
        _Step(_red_bar(_ENTER_MS + 120_000)),
        _Step(_red_bar(_EXIT_MS)),
        *sell_minute_prices,
    ]


async def test_a_simulated_sell_waits_for_its_own_minutes_price(run: Any) -> None:
    """A Live Shadow fills its own sale, so it needs a price: the one for its minute, a moment later."""
    ran = await run(_round_trip(_Step(_qqq(_EXIT_MS, close="40.60"), after_s=0.02)), wait_ms=5_000, synthesized=True)

    _enter, sell = ran.calls
    assert (sell["purpose"], sell["pricing_bar"].end_ms, sell["pricing_bar"].close) == (
        "EXIT", _EXIT_MS, Decimal("40.60"),
    )


async def test_a_simulated_sell_with_no_price_of_its_minute_is_handed_the_newest_earlier_trade(run: Any) -> None:
    """Never held back, never priced from the signal: QQQ's 10:01 trade, with its own time on it."""
    ran = await run(_round_trip(), wait_ms=20, synthesized=True)

    _enter, sell = ran.calls
    assert sell["purpose"] == "EXIT"
    assert (sell["pricing_bar"].symbol, sell["pricing_bar"].end_ms, sell["pricing_bar"].close) == (
        TRADED, _ENTER_MS + 60_000, Decimal("40.30"),
    )
    assert sell["retained_source_bar"].end_ms == _EXIT_MS


async def test_a_bot_that_trades_what_it_listens_to_opens_no_second_stream(run: Any) -> None:
    ran = await run(
        [_Step(_green_bar(_FIRST_GREEN_MS)), _Step(_green_bar(_ENTER_MS))], binding=_binding(trades=SIGNAL)
    )

    (call,) = ran.calls
    assert call["pricing_bar"] is None
    assert ran.feed.symbols_asked_for == [SIGNAL]


async def test_the_paper_runner_refuses_a_plan_that_trades_options_by_name_and_opens_no_stream(tmp_path: Path) -> None:
    """Only a Dry Run runs an option bot for now. A Paper bot's is refused before any stream is asked for."""
    options_on_the_signal = ActionPlan.model_validate(
        {
            "on_enter": [
                {
                    "leg_id": "call",
                    "instrument": {"kind": "option", "underlying": SIGNAL},
                    "position": "long",
                    "qty_ratio": 1,
                    "right": "call",
                    "strike": {"selector": "atm"},
                    "expiry": {"selector": "nearest_weekly"},
                }
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": "call"}],
        }
    )
    binding = _binding().model_copy(update={"action_plan": options_on_the_signal})
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-TEST")
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    set_alpaca_clerk(_FakeClerk(repository=repo))
    feed = _ScriptedFeed([])
    try:
        with pytest.raises(OptionPlanRefused) as refused:
            await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger, pricing_feed=feed)
        assert refused.value.reason is OptionEffectRefusal.ACCOUNT_NOT_OPEN
        assert feed.symbols_asked_for == []
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()


async def test_a_bot_that_trades_another_asset_refuses_to_run_without_that_assets_feed(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-TEST")
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    set_alpaca_clerk(_FakeClerk(repository=repo))
    try:
        with pytest.raises(RuntimeError, match="needs that asset's price feed"):
            await bot_trade_strategy.run_trade_bot(_binding(), _ScriptedFeed([]), source_bars=ledger)
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()


_DRY_RUN_SID = "dry-spy-qqq"


async def test_a_dry_run_records_the_asset_it_traded_and_that_assets_price(tmp_path: Path) -> None:
    """The journal is what the bot page lists as fills: QQQ at QQQ's prices, never SPY's.

    QQQ trades at noon and a minute later, then goes quiet. The buy fills at
    noon's close. The sale is decided three minutes on, in a minute with no
    QQQ bar: it fills at the newest earlier trade, and its row says how old
    that price is.
    """
    clock = _TestClock(NOON)
    binding = BrokerBotBinding(
        strategy_instance_id=_DRY_RUN_SID, strategy_key="deployment_validation", broker="alpaca", symbol=SIGNAL,
        mode="dry_run", quantity=1, action_plan=alpaca_v1_action_plan(TRADED), run_id="run-dry", created_at_ms=0,
        sealed_account_id=synthetic_account_id_for_strategy(_DRY_RUN_SID), exit_terms=DEPLOY_EXIT_TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic",
        ),
    )
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=100.0, QQQ=40.0)
    authority = SyntheticBindingAuthority(
        binding=binding, artifacts_root=tmp_path,
        lifecycle_repo_for=lambda _sid: pytest.fail("a Dry Run's entry never reads the runner's lifecycle state"),
        runtime_in_use=lambda _sid: True, brokers={}, clock=clock,
    )
    try:
        await authority.ensure_recoverable()
        runtime = get_clerk_runtime(binding.sealed_account_id or "")
        assert runtime is not None and runtime.clerk is not None
        await runtime.clerk.register_strategy_run(binding)
        clock.value = NOON + 1_000
        minute = 60_000
        feed = _ScriptedFeed(
            [
                _Step(_green_bar(NOON - minute)),
                _Step(_qqq(NOON, close="40.10")),
                _Step(_green_bar(NOON)),
                _Step(_qqq(NOON + minute, close="40.30")),
                _Step(_red_bar(NOON + minute)),
                _Step(_red_bar(NOON + 2 * minute)),
                _Step(_red_bar(NOON + 3 * minute)),
            ]
        )
        journal = DryRunActivityJournal(tmp_path / "instance")
        bars = authority.source_bars()
        try:
            await bot_trade_strategy.run_dry_run_bot(
                binding, feed, journal, source_bars=bars, pricing_feed=feed, pricing_bar_wait_ms=20
            )
            # What the simulation itself recorded, not what the runner says of it.
            simulated = SynthesizedOrderLedger.beside_source_bars(
                account_id=binding.sealed_account_id or "", source_bars=bars, label="test"
            ).latest_records()
        finally:
            bars.close()

        sale = next(record for record in simulated.values() if record.order.side == "sell")
        assert (sale.order.filled_avg_price, sale.order.filled_at_ms) == (40.30, NOON + 3 * minute)
        assert sale.anchor is not None
        assert (sale.anchor.fill_model, sale.anchor.decision_bar_end_ms) == ("older_bar_close", NOON + minute)
        purchase = next(record for record in simulated.values() if record.order.side == "buy")
        assert purchase.anchor is not None and purchase.anchor.fill_model == "decision_bar_close"

        bought, sold = journal.tail(5)
        assert (bought.intent, bought.symbol, bought.fill_price, bought.price_bar_end_ms) == (
            "ENTER", TRADED, 40.10, None,
        )
        assert (sold.intent, sold.symbol, sold.fill_price, sold.price_bar_end_ms) == (
            "EXIT", TRADED, 40.30, NOON + minute,
        )
        assert sold.recorded_at_ms == NOON + 3 * minute
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()

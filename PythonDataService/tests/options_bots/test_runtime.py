"""Options daily duty across actual rule, journal and observed-execution seams."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence
from app.broker.alpaca.clerk.options_legs import OptionsLegSnapshot
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.option_decision_fills import OptionDecisionFill
from app.broker.ibkr.options_session_market import OptionsMarketUnavailable
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options_backtest.market import MinuteBar, Picked
from app.options_bots.execution import cumulative_fill
from app.options_bots.journal import OptionsBotJournal
from app.options_bots.runtime import MINUTE_GRACE_MS, OptionsBotDuty
from app.options_bots.session import CloseLeg, Phase
from app.schemas.action_plan import OptionsSessionActionPlan
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.schemas.options_bot import OPTIONS_BOT_KEY, OptionsBotConfiguration
from app.schemas.signal_program_seal import semantic_payload_hash
from app.services.bot_binding_repository import BrokerBotBinding
from app.utils.session_anchors import et_date_at_ms
from tests.options_backtest._market import D, at, contract, leg, setup

CALL = contract("call", "500")


class Clock:
    def __init__(self, instant: int) -> None:
        self.now = instant

    def __call__(self) -> int:
        return self.now

    def minute(self, minute: int) -> None:
        self.now = at(minute) + MINUTE_GRACE_MS


class Market:
    def __init__(self, window: Any, clock: Clock) -> None:
        self.session_open_ms, self.session_close_ms = window.open_ms, window.close_ms
        self.clock = clock
        self.prices = {7: D("1.40")}
        self.fail = False
        self.entered = False

    async def __aenter__(self) -> Market:
        self.entered = True
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.entered = False

    async def prepare(self, at_ms: int, **_kwargs: Any) -> None:
        if self.fail:
            raise OptionsMarketUnavailable("Trade-minute retrieval failed")

    def watch_contracts(self, *_args: Any, **_kwargs: Any) -> None:
        return

    def underlying_close(self, _at_ms: int) -> Decimal:
        return D(500)

    def underlying_bar(self, close_ms: int) -> MinuteBar:
        return MinuteBar(close_ms, D(500), D(500), D(500), D(500))

    def pick(self, leg: Any, **_kwargs: Any) -> Picked:
        return Picked(
            OptionContract.of(
                root="SPY",
                expiry_date=et_date_at_ms(self.session_open_ms),
                right=leg.right,
                strike=D(505) if leg.leg_id == "cover" else D(500),
            )
        )

    def option_bar(self, _contract: OptionContract, close_ms: int) -> MinuteBar:
        minute = (close_ms - self.session_open_ms) // 60_000
        price = self.prices.get(minute, D(2))
        return MinuteBar(close_ms, price, price, price, price)

    def quote_feed(self) -> None:
        return None

    def quote(self, _contract: OptionContract) -> None:
        return None

    async def entry_evidence(self, decision: Any, terms: Any, **_kwargs: Any) -> OptionEntryEvidence:
        quote = HeldQuote(QuoteSide(D("1.99"), self.clock()), QuoteSide(D("2.00"), self.clock()), QuoteDataType.LIVE, 1)
        return OptionEntryEvidence(
            picked={decision.leg_id: decision.contract},
            multipliers={decision.leg_id: 100},
            quotes={decision.leg_id: quote},
            generation=1,
            rechecked_at_ms=self.clock(),
            leg_gap_limit_percent=terms.gap_limit_percent,
            cost_limit_percent=terms.cost_limit_percent,
        )


def _binding(written: Any, *, created_at_ms: int | None = None) -> BrokerBotBinding:
    digest = semantic_payload_hash(written.model_dump(mode="json"))
    return BrokerBotBinding(
        strategy_instance_id="options-bot",
        strategy_key=OPTIONS_BOT_KEY,
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        action_plan=OptionsSessionActionPlan(kind="options_session", underlying="SPY", setup_sha256=digest),
        options_bot=OptionsBotConfiguration(
            golden_setup_id="golden", setup_sha256=digest, rules_digest="a" * 64, setup=written
        ),
        option_order_terms=SealedOptionOrderTerms(
            gap_limit_percent="10", entry_wait_seconds=20, cost_limit_percent="10", expiry_close_lead_minutes=45
        ),
        sealed_account_id="paper-test",
        run_id="first-run",
        created_at_ms=at(1) if created_at_ms is None else created_at_ms,
    )


class Clerk:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.snapshots: dict[str, OptionsLegSnapshot] = {}
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.enabled = True
        self.fill_quantity: int | None = None
        self.fill_price = 2.0
        self.terminal = True
        self.cancel_proven = True
        self.crash_before_ack = False
        self.crash_before_accept = False
        self.before_send: Any = None
        self.refuse_entries = False
        self.refuse_closes = 0

    def options_entries_enabled(self, _bot: str) -> bool:
        return self.enabled

    def options_leg_snapshot(self, _bot: str, leg_id: str, attempt: str) -> OptionsLegSnapshot:
        return self.snapshots.get(attempt, OptionsLegSnapshot(leg_id, attempt))

    async def execute_options_leg(self, **kwargs: Any) -> Any:
        if self.before_send is not None:
            self.before_send(kwargs)
        self.calls.append(kwargs)
        attempt = kwargs["attempt_id"]
        if self.crash_before_accept:
            self.crash_before_accept = False
            raise ConnectionError("process stopped before acceptance")
        if kwargs["purpose"] is EffectPurpose.ENTER:
            if self.refuse_entries:
                return SimpleNamespace(state=EffectOperationState.REJECTED, explanation="Entry permission was withdrawn.")
            if attempt in self.snapshots:
                return SimpleNamespace(state=EffectOperationState.ACCEPTED)
            evidence = kwargs["option_entry"]
            if evidence is None:
                return SimpleNamespace(state=EffectOperationState.REJECTED, explanation="No current entry evidence.")
            leg_id = kwargs["leg_id"]
            option = evidence.picked[leg_id]
            quantity = self.fill_quantity
            rows = () if quantity == 0 else (_fill(option, quantity or 1, self.fill_price, self.clock()),)
            self.snapshots[attempt] = OptionsLegSnapshot(
                leg_id,
                attempt,
                entry_decision_id=kwargs["decision_id"],
                entry_order_ref="entry",
                standing=EntryStanding.FILLED if self.terminal else EntryStanding.WORKING,
                held_contract=option,
                held_quantity=0 if quantity == 0 else quantity or 1,
                entry_fills=rows,
            )
            if self.crash_before_ack:
                self.crash_before_ack = False
                raise ConnectionError("acknowledgement lost after actual acceptance")
        else:
            if self.refuse_closes:
                self.refuse_closes -= 1
                return SimpleNamespace(state=EffectOperationState.REJECTED, explanation="The close needs a fresh quote.")
            snapshot = self.snapshots[attempt]
            quantity = sum(row.quantity for row in snapshot.entry_fills)
            self.snapshots[attempt] = replace(
                snapshot,
                held_quantity=0,
                close_pending=False,
                close_fills=(_fill(snapshot.held_contract, int(quantity), 1.3, self.clock(), side="sell"),),
            )
        return SimpleNamespace(state=EffectOperationState.ACCEPTED)

    async def cancel_options_leg_entry(self, _bot: str, _run: str, _leg: str, attempt: str) -> OptionsLegSnapshot:
        self.cancelled.append(attempt)
        snapshot = self.snapshots[attempt]
        if self.cancel_proven:
            snapshot = replace(
                snapshot, standing=EntryStanding.PART_FILLED if snapshot.entry_fills else EntryStanding.UNFILLED
            )
            self.snapshots[attempt] = snapshot
        return snapshot


def _fill(
    option: OptionContract, quantity: int, price: float, instant: int, *, side: str = "buy"
) -> OptionDecisionFill:
    return OptionDecisionFill(
        order_ref=f"{side}-order",
        contract=option.occ_symbol,
        multiplier=100,
        side=side,
        quantity=float(quantity),
        price=price,
        filled_at_ms=instant,
    )


class Qualifier:
    def __init__(self, binding: BrokerBotBinding) -> None:
        self.config = binding.options_bot
        self.revoked = False

    async def __call__(self, _identity: str) -> Any:
        if self.revoked:
            raise ValueError("The owner revoked this golden setup")
        return SimpleNamespace(
            id="golden",
            setup_sha256=self.config.setup_sha256,
            setup=self.config.setup,
            status="ready",
            rules_digest="b" * 64,
        )


def _duty(
    path: Path, binding: BrokerBotBinding, clock: Clock, clerk: Clerk
) -> tuple[OptionsBotDuty, OptionsBotJournal, Qualifier]:
    journal = OptionsBotJournal(
        path, strategy_instance_id=binding.strategy_instance_id, setup_sha256=binding.options_bot.setup_sha256
    )
    qualified = Qualifier(binding)
    return (
        OptionsBotDuty(
            binding, clerk, journal, market_factory=lambda window: Market(window, clock), qualify=qualified, clock=clock
        ),
        journal,
        qualified,
    )


async def test_snapshot_and_outbox_are_durable_before_delivery_and_recover_lost_ack(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)

    def assert_durable(kwargs: Any) -> None:
        assert journal.state.legs[0].phase is Phase.ORDERED
        pending = journal.decisions(pending_only=True)
        assert pending[0].decision_id == kwargs["decision_id"] and pending[0].dispatched

    clerk.before_send = assert_durable
    clerk.crash_before_ack = True
    await duty.tick()
    decision = journal.decisions()[0].decision_id
    assert journal.decisions()[0].status == "pending"
    await duty.close()
    journal.close()

    clerk.before_send = None
    resumed, journal, _qualified = _duty(tmp_path, binding.model_copy(update={"run_id": "resumed-run"}), clock, clerk)
    clock.now += 1
    await resumed.tick()
    assert len(clerk.calls) == 1
    assert journal.decisions()[0].decision_id == decision
    assert journal.decisions()[0].status == "submitted"
    assert journal.state.legs[0].position.contracts_held == 1
    await resumed.close()
    journal.close()


async def test_unsent_crash_retries_same_id_and_never_queues_a_new_entry(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    clerk.crash_before_accept = True
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    await duty.close()
    journal.close()
    resumed, journal, _qualified = _duty(tmp_path, binding.model_copy(update={"run_id": "resume"}), clock, clerk)
    clock.now += 1_000
    await resumed.tick()
    assert len(clerk.calls) == 2
    assert clerk.calls[0]["decision_id"] == clerk.calls[1]["decision_id"]
    assert len(journal.decisions()) == 1
    await resumed.close()
    journal.close()


async def test_rejected_entry_settles_without_inventing_a_position_or_waiting_forever(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    clerk.refuse_entries = True
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    try:
        await duty.tick()
        assert journal.decisions()[0].status == "discarded"
        assert journal.decisions()[0].explanation == "Entry permission was withdrawn."
        clock.now += 1
        await duty.tick()
        assert journal.state.legs[0].position is None
        assert journal.state.legs[0].entries_filled == 0
        assert journal.status()["next_entry_ms"] > at(60)
        assert len(clerk.calls) == 1
    finally:
        await duty.close()
        journal.close()


async def test_rejected_close_stays_pending_and_retries_its_exact_decision(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg(stop=("percent", "25")))), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    try:
        await duty.tick()
        clock.now += 1
        await duty.tick()
        clerk.refuse_closes = 1
        clock.minute(7)
        await duty.tick()
        [pending] = journal.decisions(pending_only=True)
        assert isinstance(pending.decision, CloseLeg)
        assert journal.state.legs[0].position.contracts_held == 1
        assert "The close needs a fresh quote" in journal.status()["execution_hold_reason"]
        await duty.close()
        journal.close()
        duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
        assert "The close needs a fresh quote" in journal.status()["execution_hold_reason"]
        clock.now += 5001
        await duty.tick()
        close_calls = [call for call in clerk.calls if call["purpose"] is EffectPurpose.EXIT]
        assert len(close_calls) == 2
        assert close_calls[0]["decision_id"] == close_calls[1]["decision_id"] == pending.decision_id
        assert journal.decisions(pending_only=True) == ()
        assert journal.status()["execution_hold_reason"] is None
    finally:
        await duty.close()
        journal.close()


async def test_partial_confirmed_entry_cancels_remainder_and_stops_at_actual_average(tmp_path: Path) -> None:
    binding = _binding(setup(leg(contracts=3, stop=("percent", "25"))))
    clock, clerk = Clock(at(5) + MINUTE_GRACE_MS), Clerk(Clock(0))
    clerk.clock, clerk.fill_quantity, clerk.fill_price, clerk.terminal = clock, 1, 2.5, False
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    clock.now += 1
    await duty.tick()
    assert len(clerk.cancelled) == 1
    assert journal.state.legs[0].position.stop_level == D("1.875")
    assert journal.state.legs[0].position.contracts_held == 1
    clock.minute(6)
    await duty.tick()
    clock.minute(7)
    await duty.tick()
    assert [call["purpose"] for call in clerk.calls] == [EffectPurpose.ENTER, EffectPurpose.EXIT]
    assert len(clerk.cancelled) == 1
    await duty.close()
    journal.close()


async def test_revoked_approval_and_manage_only_clerk_keep_exit_management(tmp_path: Path) -> None:
    binding = _binding(setup(leg(stop=("percent", "25"), reentry=("at_once", 1))))
    clock = Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    clock.minute(6)
    await duty.tick()
    qualified.revoked = True
    clerk.enabled = False
    clock.minute(7)
    await duty.tick()
    assert clerk.calls[-1]["purpose"] is EffectPurpose.EXIT
    clock.minute(8)
    await duty.tick()
    clock.minute(9)
    await duty.tick()
    assert len(clerk.calls) == 2
    assert journal.status()["entry_hold_reason"]
    await duty.close()
    journal.close()


async def test_revocation_blocks_unsent_covered_entry_in_the_same_minute(tmp_path: Path) -> None:
    binding = _binding(setup(leg("cover"), leg("call", side="sell")))
    clock = Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, qualified = _duty(tmp_path, binding, clock, clerk)
    try:
        await duty.tick()
        assert [call["leg_id"] for call in clerk.calls] == ["cover"]
        [pending] = journal.decisions(pending_only=True)
        assert pending.decision.leg_id == "call" and not pending.dispatched
        qualified.revoked = True
        clock.now += 1_000

        await duty.tick()

        assert [call["leg_id"] for call in clerk.calls] == ["cover"]
        assert journal.status()["entry_hold_reason"]
        assert journal.decisions(pending_only=True) == ()
    finally:
        await duty.close()
        journal.close()


@pytest.mark.parametrize("entries_enabled", [False, True])
@pytest.mark.parametrize("waiting", [False, True])
async def test_unused_candidate_history_cannot_block_held_stop(tmp_path: Path, entries_enabled: bool, waiting: bool) -> None:
    from app.broker.ibkr.options_session_market import IbkrOptionsSessionMarket
    from app.options.resolver import Chain, FeedState, WatchedContract
    from app.services.option_watch import OptionWatchList
    from tests.broker.ibkr.test_options_session_market import _Client, _listed

    held, unused = CALL, contract("call", "505")
    clock = Clock(at(5) + MINUTE_GRACE_MS)
    written = setup(leg(stop=("percent", "25")))
    if waiting:
        data = setup(leg(stop=("percent", "25")), leg("waiting", momentum=("percent_up", "25"))).model_dump(mode="json")
        data["legs"][1]["strike"] = {"kind": "atm_offset", "steps": 1}
        written = type(written).model_validate(data)
    binding = _binding(written)
    clerk, qualified = Clerk(clock), Qualifier(binding)

    def market_factory(window: Any) -> IbkrOptionsSessionMarket:
        quote = HeldQuote(QuoteSide(D("1.99"), clock()), QuoteSide(D(2), clock()), QuoteDataType.LIVE, 1)
        source = SimpleNamespace(
            option_feed=lambda: FeedState(1, None),
            option_chain=lambda _symbol: Chain((), {c.occ_symbol: WatchedContract(quote, None) for c in (held, unused)}),
        )
        market = IbkrOptionsSessionMarket(
            binding.options_bot.setup, session_open_ms=window.open_ms, session_close_ms=window.close_ms,
            client=_Client(), watch=OptionWatchList(clock=clock), quotes=lambda: source, clock=clock,
        )
        market._stock, market._listing_loaded = SimpleNamespace(conId=1), True
        market._listed = {held.occ_symbol: _listed(held, 2), unused.occ_symbol: _listed(unused, 3)}

        async def read(ib_contract: Any, minute: int) -> MinuteBar:
            if ib_contract.conId == 3 and minute >= at(7):
                raise OptionsMarketUnavailable("Unused candidate history is unavailable")
            price = D(505 if minute >= at(7) else 500) if ib_contract.conId == 1 else D(1 if minute >= at(7) else 2)
            return MinuteBar(minute, price, price, price, price)

        market._read = read
        return market

    journal = OptionsBotJournal(tmp_path, strategy_instance_id=binding.strategy_instance_id, setup_sha256=binding.options_bot.setup_sha256)
    duty = OptionsBotDuty(binding, clerk, journal, market_factory=market_factory, qualify=qualified, clock=clock)
    try:
        await duty.tick()
        clock.minute(6)
        await duty.tick()
        qualified.revoked, clerk.enabled = not entries_enabled, entries_enabled
        clock.minute(7)

        await duty.tick()

        assert [call["purpose"] for call in clerk.calls] == [EffectPurpose.ENTER, EffectPurpose.EXIT]
        assert journal.state.last_minute_ms == at(7)
        assert journal.state.legs[0].position.close.reason == "stop"
    finally:
        await duty.close()
        journal.close()


async def test_clock_close_survives_missing_trade_observations(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    clock.minute(6)
    await duty.tick()
    duty._market.fail = True
    clock.minute(7)
    await duty.tick()
    clock.minute(60)
    await duty.tick()
    assert clerk.calls[-1]["purpose"] is EffectPurpose.EXIT
    assert journal.status()["entry_hold_reason"]
    await duty.close()
    journal.close()


@pytest.mark.parametrize("history_failure", [False, True])
async def test_missed_minute_stop_is_managed_before_a_later_price_recovery(tmp_path: Path, history_failure: bool) -> None:
    binding = _binding(setup(leg(stop=("percent", "25"))))
    clock = Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    try:
        await duty.tick()
        clock.minute(6)
        await duty.tick()
        if history_failure:
            duty._market.fail = True
            clock.minute(7)
            await duty.tick()
            duty._market.fail = False
        clock.minute(9)

        await duty.tick()

        assert [call["purpose"] for call in clerk.calls] == [EffectPurpose.ENTER, EffectPurpose.EXIT]
        assert journal.state.legs[0].position.close.decided_at_ms == at(7)
        assert journal.state.last_minute_ms == at(9)
        assert journal.status()["data_gap"]
    finally:
        await duty.close()
        journal.close()


async def test_backfilled_stop_bars_do_not_precede_a_late_confirmed_entry(tmp_path: Path) -> None:
    binding = _binding(setup(leg(stop=("percent", "25"))))
    clock = Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    clerk.fill_quantity, clerk.terminal = 0, False
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    try:
        await duty.tick()
        clock.minute(6)
        await duty.tick()
        [attempt] = clerk.snapshots
        clock.minute(9)
        duty._market.prices[9] = D("1.40")
        clerk.snapshots[attempt] = replace(
            clerk.snapshots[attempt], standing=EntryStanding.FILLED, held_quantity=1,
            entry_fills=(_fill(CALL, 1, 2.0, at(9) + 1),),
        )

        await duty.tick()

        assert journal.state.legs[0].position.close is None
        assert [call["purpose"] for call in clerk.calls] == [EffectPurpose.ENTER]
        clock.minute(10)
        duty._market.prices[10] = D("1.40")
        await duty.tick()
        assert clerk.calls[-1]["purpose"] is EffectPurpose.EXIT
    finally:
        await duty.close()
        journal.close()


async def test_daily_cycle_starts_fresh_counters_only_after_actual_flat_proof(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    clock.minute(6)
    await duty.tick()
    clock.minute(60)
    await duty.tick()
    clock.now += 1
    await duty.tick()
    assert duty.window.open_ms > at(390)
    assert journal.status()["phase"] == "waiting"
    clock.now = duty.window.entry_ms + MINUTE_GRACE_MS
    await duty.tick()
    entries = [call for call in clerk.calls if call["purpose"] is EffectPurpose.ENTER]
    assert len(entries) == 2 and entries[0]["attempt_id"] != entries[1]["attempt_id"]
    assert journal.state.legs[0].entries_filled == 0
    await duty.close()
    journal.close()


async def test_new_late_deployment_waits_next_session_and_unknown_order_blocks_day_reset(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg()), created_at_ms=at(5) + 1), Clock(at(6) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    assert clerk.calls == []
    assert duty.window.open_ms > at(390)
    await duty.close()
    journal.close()

    binding = _binding(setup(leg()))
    clock.minute(5)
    clerk.fill_quantity, clerk.terminal, clerk.cancel_proven = 0, False, False
    duty, journal, _qualified = _duty(tmp_path / "unknown", binding, clock, clerk)
    await duty.tick()
    original_open = duty.window.open_ms
    clock.minute(60)
    await duty.tick()
    clock.minute(1_000)
    await duty.tick()
    assert duty.window.open_ms == original_open
    assert journal.state.legs[0].phase is Phase.ORDERED
    await duty.close()
    journal.close()


def test_cumulative_observed_average_is_quantity_weighted_and_context_independent() -> None:
    rows = (_fill(CALL, 1, 2.0, at(6)), _fill(CALL, 3, 3.0, at(6) + 1))
    with localcontext() as context:
        context.prec = 2
        observed = cumulative_fill("call", rows)
    assert observed.contracts == 4
    assert observed.filled == D("2.75")
    assert observed.bar_close_ms == at(6) + 1
    with pytest.raises(ValueError, match="positive whole"):
        cumulative_fill("call", (replace(rows[0], quantity=0.5),))


def test_journal_refuses_a_different_bot_or_approved_setup(tmp_path: Path) -> None:
    journal = OptionsBotJournal(tmp_path, strategy_instance_id="first", setup_sha256="a" * 64)
    journal.close()
    with pytest.raises(ValueError, match="another bot"):
        OptionsBotJournal(tmp_path, strategy_instance_id="second", setup_sha256="a" * 64)


async def test_stop_during_quote_preparation_prevents_entry_delivery(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    gate = asyncio.Event()
    gate.set()

    class StoppedWhileQuoting(Market):
        async def entry_evidence(self, decision: Any, terms: Any, **kwargs: Any) -> OptionEntryEvidence:
            evidence = await super().entry_evidence(decision, terms, **kwargs)
            gate.clear()
            return evidence

    duty.market_factory = lambda window: StoppedWhileQuoting(window, clock)
    await duty.run(gate)
    assert clerk.calls == []
    assert journal.decisions()[0].status == "pending"
    assert not journal.decisions()[0].dispatched
    journal.close()


async def test_wrong_clerk_decision_identity_cannot_supply_another_attempts_holdings(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    attempt = next(iter(clerk.snapshots))
    clerk.snapshots[attempt] = replace(clerk.snapshots[attempt], entry_decision_id="another-decision")
    clock.now += 1
    with pytest.raises(ValueError, match="exact leg, attempt"):
        await duty.tick()
    await duty.close()
    journal.close()


async def test_clerk_mandatory_close_finishes_session_without_duplicate_order_or_reentry(tmp_path: Path) -> None:
    binding = _binding(setup(leg(stop=("percent", "25"), reentry=("at_once", 1))))
    clock = Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    clock.minute(6)
    await duty.tick()
    attempt = next(iter(clerk.snapshots))
    snapshot = clerk.snapshots[attempt]
    clerk.snapshots[attempt] = replace(
        snapshot,
        held_quantity=0,
        close_pending=False,
        close_fills=(_fill(snapshot.held_contract, 1, 1.9, clock(), side="sell"),),
    )

    clock.now += 1
    await duty.tick()
    assert len(clerk.calls) == 1
    assert journal.state.legs[0].phase is Phase.DONE
    assert journal.state.legs[0].reentries_left == 1
    assert duty.window.open_ms > at(390)
    ending = journal.status()["events"][-1]
    assert ending["kind"] == "close_filled" and ending["reason"] == "clerk_reduction"
    await duty.close()
    journal.close()


async def test_late_entry_confirmed_after_session_close_still_delivers_its_owed_exit(tmp_path: Path) -> None:
    binding, clock = _binding(setup(leg())), Clock(at(5) + MINUTE_GRACE_MS)
    clerk = Clerk(clock)
    clerk.fill_quantity, clerk.terminal, clerk.cancel_proven = 0, False, False
    duty, journal, _qualified = _duty(tmp_path, binding, clock, clerk)
    await duty.tick()
    clock.minute(390)
    await duty.tick()
    attempt = next(iter(clerk.snapshots))
    snapshot = clerk.snapshots[attempt]
    clerk.snapshots[attempt] = replace(
        snapshot,
        standing=EntryStanding.FILLED,
        held_quantity=1,
        entry_fills=(_fill(snapshot.held_contract, 1, 2.0, clock()),),
    )

    clock.now += 1
    await duty.tick()
    assert [call["purpose"] for call in clerk.calls] == [EffectPurpose.ENTER, EffectPurpose.EXIT]
    assert journal.state.legs[0].position.close.reason.value == "exit_time"
    clock.now += 1
    await duty.tick()
    assert journal.state.legs[0].phase is Phase.DONE
    assert duty.window.open_ms > at(390)
    await duty.close()
    journal.close()

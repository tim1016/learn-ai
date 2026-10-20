"""Daily Options bot duty: clock, durable decisions and actual Clerk outcomes.

No replay fills are used. The canonical session decides; the Clerk owns every
order and confirmed holding. A missed observation holds fresh exposure for the
session, while stop management and clock-driven closes continue (ADR 0079).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionExitEvidence
from app.broker.alpaca.clerk.options_legs import OptionsLegSnapshot
from app.broker.alpaca.clerk.sqlite.entry_outcome import PROVEN_STANDINGS
from app.broker.ibkr.options_session_market import IbkrOptionsSessionMarket, OptionsMarketUnavailable
from app.marketdata.feed import MarketDataFeed
from app.options_backtest.cover_book import Candidate, Held, Holding, admit_opening
from app.options_backtest.setup import MS_PER_MINUTE
from app.options_bots.execution import observed_outcomes
from app.options_bots.golden import get_ready_setup
from app.options_bots.journal import OptionsBotJournal, PendingDecision, attempt_identity
from app.options_bots.observations import ObservedMarket, ObservedOrder
from app.options_bots.schedule import OptionsSessionWindow, first_eligible_session
from app.options_bots.session import (
    CancelEntry,
    ExecutionOutcome,
    OpenLeg,
    OptionsSession,
    OptionsStep,
    Phase,
)
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)
MINUTE_GRACE_MS = 8_000
ENTRY_DECISION_LIFETIME_MS = 50_000


class OptionsBotDuty:
    """Testable daily timer duty over the real session/journal interfaces."""

    def __init__(
        self,
        binding: BrokerBotBinding,
        clerk: Any,
        journal: OptionsBotJournal,
        *,
        market_factory: Callable[[OptionsSessionWindow], Any] | None = None,
        qualify: Callable[[str], Awaitable[Any]] = get_ready_setup,
        clock: Clock = now_ms_utc,
    ) -> None:
        if binding.options_bot is None or binding.option_order_terms is None:
            raise ValueError("an Options runner needs its sealed setup and execution terms")
        self.binding, self.clerk, self.journal = binding, clerk, journal
        self.config = binding.options_bot
        self.setup = self.config.setup
        if (
            journal.strategy_instance_id != binding.strategy_instance_id
            or journal.setup_sha256 != self.config.setup_sha256
        ):
            raise ValueError("an Options duty must use its own bot and exact-setup journal")
        self.clock, self.qualify = clock, qualify
        self.market_factory = market_factory or (
            lambda window: IbkrOptionsSessionMarket(
                self.setup,
                session_open_ms=window.open_ms,
                session_close_ms=window.close_ms,
                clock=self.clock,
            )
        )
        status, saved = journal.status(), journal.state
        if status.get("next_session") is not None:
            self.window = OptionsSessionWindow(**status["next_session"])
        elif saved is not None:
            self.window = OptionsSessionWindow(
                saved.session_open_ms,
                saved.session_close_ms,
                self.setup.entry.at(session_open_ms=saved.session_open_ms, session_close_ms=saved.session_close_ms),
                self.setup.exit.at(session_open_ms=saved.session_open_ms, session_close_ms=saved.session_close_ms),
            )
        else:
            self.window = first_eligible_session(self.setup, at_ms=binding.created_at_ms)
        if self.window != first_eligible_session(self.setup, at_ms=self.window.entry_ms):
            raise ValueError("the restored Options schedule must match its canonical calendar session")
        self.journal.note(next_session=asdict(self.window), next_entry_ms=self.window.entry_ms)
        self._session: OptionsSession | None = None
        self._observations: ObservedMarket | None = None
        self._observed_orders: list[ObservedOrder] = []
        self._recorded_orders: set[ObservedOrder] = set()
        self._market: Any = None
        self._market_entered = False
        self._qualified_minute: int | None = None
        self._entry_hold: str | None = None
        self._retry_at: dict[str, int] = {}
        self._run_gate: asyncio.Event | None = None

    async def close(self) -> None:
        if self._market is not None:
            await self._market.__aexit__(None, None, None)
        self._market, self._session, self._market_entered = None, None, False

    async def run(self, run_gate: asyncio.Event | None = None) -> None:
        self._run_gate = run_gate
        try:
            while run_gate is None or run_gate.is_set():
                await self.tick()
                await asyncio.sleep(1)
        finally:
            await self.close()

    async def _permission(self, minute: int, *, fresh: bool = False) -> bool:
        if fresh or self._qualified_minute != minute:
            self._qualified_minute = minute
            try:
                qualified = await asyncio.wait_for(self.qualify(self.config.golden_setup_id), timeout=5)
                if (
                    qualified.id != self.config.golden_setup_id
                    or qualified.setup_sha256 != self.config.setup_sha256
                    or qualified.setup != self.setup
                    or qualified.status != "ready"
                ):
                    raise ValueError("The current approval does not name this exact tested setup")
                self._entry_hold = None
            except Exception as exc:
                self._entry_hold = f"Golden setup is unavailable for new entries: {exc}"
                logger.warning(
                    "Options entries held by qualification",
                    extra={
                        "action": "options_qualification_held",
                        "strategy_instance_id": self.binding.strategy_instance_id,
                        "error": str(exc),
                    },
                )
        if not self.clerk.options_entries_enabled(self.binding.strategy_instance_id):
            self._entry_hold = "This resumed bot is managing its held contracts only; new entries are disabled."
        gap = bool(self.journal.status().get("data_gap"))
        hold = "A decision minute was missed. New entries are held for this session." if gap else self._entry_hold
        self.journal.note(entry_hold_reason=hold)
        return hold is None

    async def _ensure_session(self, now: int) -> None:
        if self._session is None:
            saved = self.journal.state
            self._market = self.market_factory(self.window)
            self._observations = ObservedMarket(self._market, self.setup.underlying)
            self._session = OptionsSession(
                self.setup,
                self._observations,
                state=saved if saved is not None and saved.session_open_ms == self.window.open_ms else None,
            )
            self.journal.commit(self._session.state, OptionsStep((), ()))
        if not self._market_entered and now < self.window.exit_ms:
            try:
                await self._market.__aenter__()
                self._market_entered = True
            except Exception as exc:
                self.journal.note(market_hold=f"IBKR observations are unavailable: {exc}")

    def _outcomes(self) -> tuple[ExecutionOutcome, ...]:
        assert self._session is not None
        outcomes: list[ExecutionOutcome] = []
        rows = self.journal.decisions(session_open_ms=self.window.open_ms)
        for leg in self._session.state.legs:
            if leg.phase is not Phase.ORDERED and leg.position is None:
                continue
            number = leg.entries_filled if leg.position is None else leg.position.entry_number
            attempt = attempt_identity(
                self.binding.strategy_instance_id,
                self.window.open_ms,
                leg.setup.leg_id,
                number,
                leg.order_decided_at_ms,
            )
            row = next((row for row in rows if row.attempt_id == attempt and isinstance(row.decision, OpenLeg)), None)
            if row is None:
                raise ValueError("a pending Options attempt has no durable opening decision")
            snapshot = self._snapshot(row)
            for kind, fills in (("entry", snapshot.entry_fills), ("close", snapshot.close_fills)):
                for fill in fills:
                    observed = ObservedOrder(fill.order_ref, row.attempt_id, leg.setup.leg_id, number, fill.contract, kind, fill.filled_at_ms)
                    if observed not in self._recorded_orders and observed not in self._observed_orders:
                        self._observed_orders.append(observed)
            if snapshot.entry_decision_id is not None:
                if row.status == "pending":
                    self.journal.finish(row.decision_id)
                outcomes.extend(
                    observed_outcomes(snapshot, entry_number=number, entry_decided_at_ms=leg.order_decided_at_ms)
                )
            elif row.status == "discarded":
                outcomes.append(
                    ExecutionOutcome(
                        f"discarded:{row.decision_id}",
                        leg.setup.leg_id,
                        number,
                        "entry",
                        terminal=True,
                        decided_at_ms=leg.order_decided_at_ms,
                    )
                )
        return tuple(outcomes)

    def _snapshot(self, row: PendingDecision) -> OptionsLegSnapshot:
        snapshot = self.clerk.options_leg_snapshot(
            self.binding.strategy_instance_id,
            row.decision.leg_id,
            row.attempt_id,
        )
        if (
            snapshot.leg_id != row.decision.leg_id
            or snapshot.attempt_id != row.attempt_id
            or (snapshot.entry_decision_id is not None and snapshot.entry_decision_id != row.decision_id)
        ):
            raise ValueError("Clerk observations must name this exact leg, attempt and opening decision")
        return snapshot

    def _contracts(self) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        assert self._session is not None
        retained = {
            leg.attempt.contract
            for leg in self._session.state.legs
            if leg.attempt is not None and leg.phase in (Phase.WAITING, Phase.ORDERED, Phase.HELD)
        }
        held = {leg.position.attempt.contract for leg in self._session.state.legs if leg.position is not None}
        held.update(
            leg.attempt.contract
            for leg in self._session.state.legs
            if leg.attempt is not None and leg.phase is Phase.ORDERED
        )
        return tuple(retained), tuple(held)

    async def tick(self, now_ms: int | None = None) -> None:
        now = self.clock() if now_ms is None else now_ms
        if now < self.window.open_ms:
            self.journal.note(phase="waiting", next_entry_ms=self.window.entry_ms)
            return
        await self._ensure_session(now)
        assert self._session is not None
        minute = min(((now - MINUTE_GRACE_MS) // MS_PER_MINUTE) * MS_PER_MINUTE, self.window.close_ms)
        if minute < self.window.entry_ms:
            self.journal.note(phase="waiting", next_entry_ms=self.window.entry_ms)
            return
        allowed = await self._permission(minute)
        outcomes = self._outcomes()
        state = self._session.state
        if state.last_minute_ms is not None and minute > state.last_minute_ms + MS_PER_MINUTE:
            self.journal.note(
                data_gap=True, entry_hold_reason="A decision minute was missed. New entries are held for this session."
            )
            allowed = False
        if state.last_minute_ms is not None:
            step = self._session.advance(state.last_minute_ms, outcomes, allow_entries=allowed, observed_at_ms=now)
            self._commit(step, now)
            outcomes = ()
        minutes = (
            (minute,) if state.last_minute_ms is None or state.last_minute_ms == minute
            else range(state.last_minute_ms + MS_PER_MINUTE, minute + MS_PER_MINUTE, MS_PER_MINUTE)
        )
        market_ready = False
        for completed in minutes:
            market_ready, allowed = await self._advance_minute(completed, outcomes, allowed=allowed, now=now)
            if not market_ready:
                if minute >= self.window.exit_ms:
                    market_ready, allowed = await self._advance_minute(minute, outcomes, allowed=False, now=now)
                break
        await self._deliver(allowed and market_ready, now)
        self.journal.note(phase="managing" if any(leg.position is not None for leg in state.legs) else "running")
        if state.started and all(leg.phase is Phase.DONE for leg in state.legs) and self._settled():
            next_window = first_eligible_session(self.setup, at_ms=max(now, self.window.entry_ms + 1))
            await self.close()
            self.window = next_window
            self.journal.note(
                next_session=asdict(self.window), next_entry_ms=self.window.entry_ms, data_gap=False, phase="waiting"
            )
            self._qualified_minute = None

    async def _advance_minute(
        self, minute: int, outcomes: tuple[ExecutionOutcome, ...], *, allowed: bool, now: int,
    ) -> tuple[bool, bool]:
        assert self._session is not None
        state = self._session.state
        retained, held = self._contracts()
        self._market.watch_contracts(retained, held_contracts=held)
        ready = minute >= self.window.exit_ms or minute == state.last_minute_ms
        if not ready:
            try:
                started = self.clock()
                candidate_hold = await self._market.prepare(
                    minute, contracts=retained, held_contracts=held, include_candidates=allowed,
                )
                self._log_prepared(minute, started)
                self.journal.note(market_hold=None)
                if candidate_hold is not None:
                    self.journal.note(entry_hold_reason=candidate_hold)
                    allowed = False
                ready = True
            except OptionsMarketUnavailable as exc:
                self.journal.note(market_hold=str(exc), entry_hold_reason=str(exc))
                allowed = False
        at_ms = minute if ready else state.last_minute_ms
        if at_ms is not None:
            step = self._session.advance(at_ms, outcomes, allow_entries=allowed and ready, observed_at_ms=now)
            self._commit(step, now)
        return ready, allowed

    def _commit(self, step: OptionsStep, now: int) -> None:
        assert self._session is not None and self._observations is not None
        self.journal.commit(self._session.state, step, minutes=self._observations.minutes,
                            orders=self._observed_orders, read_at_ms=now)
        self._recorded_orders.update(self._observed_orders)
        self._observations.minutes.clear()
        self._observed_orders.clear()

    def _log_prepared(self, minute: int, started: int) -> None:
        prepared = self.clock()
        # The production formatter drops ``extra``; the message carries the timings too.
        logger.info(
            "Options %s minute %d read %d ms after its close in %d ms",
            self.binding.strategy_instance_id,
            minute,
            started - minute,
            prepared - started,
            extra={
                "action": "options_minute_prepared",
                "strategy_instance_id": self.binding.strategy_instance_id,
                "minute_ms": minute,
                "started_after_close_ms": started - minute,
                "prepare_ms": prepared - started,
            },
        )

    def _settled(self) -> bool:
        for row in self.journal.decisions(session_open_ms=self.window.open_ms):
            if not isinstance(row.decision, OpenLeg):
                if row.status == "pending":
                    return False
                continue
            snapshot = self._snapshot(row)
            if snapshot.held_quantity or snapshot.close_pending:
                return False
            if row.status != "discarded" and (
                snapshot.entry_decision_id is None or snapshot.standing not in PROVEN_STANDINGS
            ):
                return False
        return True

    def _cover_ready(self, decision: OpenLeg) -> bool:
        assert self._session is not None
        if decision.side == "buy":
            return True
        holdings = tuple(
            Holding(
                leg.setup.leg_id,
                leg.position.attempt.contract,
                leg.setup.side,
                leg.position.contracts_held,
                leg.position.entry.multiplier,
            )
            for leg in self._session.state.legs
            if leg.position is not None and leg.position.contracts_held > 0
        )
        held = Held(tuple(leg.leg_id for leg in self.setup.legs), holdings)
        candidate = Candidate(
            decision.leg_id, decision.contract, decision.side, decision.contracts, decision.multiplier, Decimal(0)
        )
        return not admit_opening(held, (candidate,), cash=None, closing=()).refused

    def _dispatching(self, row: PendingDecision) -> None:
        self.journal.dispatching(row.decision_id)
        lag = self.clock() - row.decision.decided_at_ms
        logger.info(
            "Options %s decision %s for minute %d reaches the Clerk %d ms after its close",
            self.binding.strategy_instance_id,
            row.decision_id,
            row.decision.decided_at_ms,
            lag,
            extra={
                "action": "options_decision_dispatched",
                "strategy_instance_id": self.binding.strategy_instance_id,
                "decision_id": row.decision_id,
                "minute_ms": row.decision.decided_at_ms,
                "after_close_ms": lag,
            },
        )

    async def _deliver(self, allowed: bool, now: int) -> None:
        for row in self.journal.decisions(session_open_ms=self.window.open_ms, pending_only=True):
            if self._run_gate is not None and not self._run_gate.is_set():
                return
            if now < self._retry_at.get(row.decision_id, 0):
                continue
            decision = row.decision
            try:
                if isinstance(decision, OpenLeg):
                    snapshot = self._snapshot(row)
                    if snapshot.entry_decision_id is not None:
                        self.journal.finish(row.decision_id)
                        continue
                    late = now >= decision.decided_at_ms + ENTRY_DECISION_LIFETIME_MS
                    evidence = None
                    if allowed and not late:
                        if not self._cover_ready(decision):
                            continue
                        try:
                            evidence = await self._market.entry_evidence(
                                decision,
                                self.binding.option_order_terms,
                                proven_by_ms=decision.decided_at_ms + ENTRY_DECISION_LIFETIME_MS,
                            )
                        except OptionsMarketUnavailable as exc:
                            self.journal.finish(row.decision_id, discarded=True, explanation=str(exc))
                            continue
                        allowed = await self._permission(decision.decided_at_ms, fresh=True)
                    if not allowed or late:
                        if not row.dispatched:
                            self.journal.finish(
                                row.decision_id,
                                discarded=True,
                                explanation="Entry held by qualification, data or its decision deadline.",
                            )
                            continue
                        evidence = None
                    if self._run_gate is not None and not self._run_gate.is_set():
                        return
                    self._dispatching(row)
                    receipt = await self.clerk.execute_options_leg(
                        strategy_instance_id=self.binding.strategy_instance_id,
                        run_id=self.binding.run_id,
                        decision_id=row.decision_id,
                        leg_id=decision.leg_id,
                        attempt_id=row.attempt_id,
                        purpose=EffectPurpose.ENTER,
                        option_entry=evidence,
                    )
                    rejected = receipt.state == EffectOperationState.REJECTED
                    self.journal.finish(
                        row.decision_id,
                        discarded=rejected,
                        explanation=receipt.explanation if rejected else None,
                    )
                elif isinstance(decision, CancelEntry):
                    self._dispatching(row)
                    await self.clerk.cancel_options_leg_entry(
                        self.binding.strategy_instance_id, self.binding.run_id, decision.leg_id, row.attempt_id
                    )
                    self.journal.finish(row.decision_id)
                else:
                    feed = self._market.quote_feed()
                    quote = self._market.quote(decision.contract)
                    evidence = (
                        None
                        if feed is None or quote is None
                        else OptionExitEvidence(
                            quotes={decision.contract.occ_symbol: quote}, generation=feed.generation, read_at_ms=now
                        )
                    )
                    self._dispatching(row)
                    receipt = await self.clerk.execute_options_leg(
                        strategy_instance_id=self.binding.strategy_instance_id,
                        run_id=self.binding.run_id,
                        decision_id=row.decision_id,
                        leg_id=decision.leg_id,
                        attempt_id=row.attempt_id,
                        purpose=EffectPurpose.EXIT,
                        option_exit=evidence,
                    )
                    if receipt.state == EffectOperationState.REJECTED:
                        self._retry_at[row.decision_id] = now + 5_000
                        self.journal.defer(
                            row.decision_id,
                            f"{decision.leg_id}: {receipt.explanation} The same close decision will be retried.",
                        )
                    else:
                        self.journal.finish(row.decision_id)
            except ValueError:
                raise
            except Exception as exc:
                self._retry_at[row.decision_id] = now + 5_000
                self.journal.defer(
                    row.decision_id,
                    f"{decision.leg_id}: An order outcome is unproven; the same decision will be checked again: {exc}",
                )
                logger.exception(
                    "Options decision delivery remains unproven",
                    extra={
                        "action": "options_delivery_unproven",
                        "strategy_instance_id": self.binding.strategy_instance_id,
                        "decision_id": row.decision_id,
                    },
                )


async def run_options_bot(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    *,
    instance_dir: Path,
    source_bars: SourceBarLedger | None,
    run_gate: asyncio.Event | None = None,
) -> None:
    """Run one bot under its exact activated authority and retained underlying feed."""
    if binding.options_bot is None or source_bars is None:
        raise RuntimeError("Options duty requires its sealed setup and durable source-bar ledger")
    account = (
        synthetic_account_id_for_strategy(binding.strategy_instance_id)
        if binding.mode == "dry_run"
        else binding.sealed_account_id
    )
    authority = None if account is None else get_clerk_runtime(account)
    clerk = None if authority is None else authority.clerk
    if clerk is None:
        raise RuntimeError("This Options bot's exact Clerk account authority is unavailable")
    journal = OptionsBotJournal(
        instance_dir, strategy_instance_id=binding.strategy_instance_id, setup_sha256=binding.options_bot.setup_sha256
    )
    duty = OptionsBotDuty(binding, clerk, journal)

    async def retain_underlying() -> None:
        async for bar in feed.stream_bars(binding.symbol, use_rth=True):
            source_bars.append(bar, run_id=binding.run_id)
        raise RuntimeError("The Options bot's retained underlying stream ended")

    try:
        async with asyncio.TaskGroup() as tasks:
            retention = tasks.create_task(retain_underlying())
            try:
                await duty.run(run_gate)
            finally:
                retention.cancel()
    finally:
        journal.close()

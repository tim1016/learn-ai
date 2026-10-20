"""Present durable Options rule progress separately from Clerk execution truth."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError, SqliteEconomicProjectionReader
from app.engine.live.identity import strategy_instance_artifact_dir
from app.options_backtest.results import EventKind
from app.options_bots.journal import JOURNAL_FILENAME, OptionsBotJournal
from app.options_bots.observations import ConsumedMinute
from app.options_bots.session import SessionState
from app.schemas.options_deploy import (
    OptionsBotEventView,
    OptionsBotLegView,
    OptionsBotSessionView,
    OptionsWatchedPremiumView,
)
from app.services.bot_runner import BotTaskRegistry, RunAdmissionRefusedError
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


def options_session_view(registry: BotTaskRegistry, broker: str, sid: str) -> OptionsBotSessionView:
    binding = registry.binding_for_control(broker, sid)
    if binding.options_bot is None:
        raise RunAdmissionRefusedError("This bot follows a Signal Program and has no Options session.")
    instance_dir = strategy_instance_artifact_dir(registry.artifacts_root, "live_state", sid)
    state, notes, minutes, order_legs = None, {}, (), {}
    if (instance_dir / JOURNAL_FILENAME).is_file():
        journal = OptionsBotJournal(instance_dir)
        try:
            state, notes = journal.state, journal.status()
            if state is not None:
                minutes = journal.minutes(state.session_open_ms)
                order_legs = journal.order_legs(state.session_open_ms)
        finally:
            journal.close()
    status = registry.status(broker, sid)
    ended = registry.bot_end(broker, sid).status in {"ending", "ended"}
    resumable = not ended and status.duty_outcome is not None and status.duty_outcome.kind in {"CRASHED", "EXITED_UNVERIFIED"}
    runtime = get_clerk_runtime(binding.sealed_account_id) if binding.sealed_account_id is not None else None
    entries_enabled = status.running and runtime is not None and runtime.clerk.options_entries_enabled(sid)
    fills = None
    if runtime is not None and runtime.sqlite_repository is not None and state is not None:
        reader = None
        try:
            reader = SqliteEconomicProjectionReader.from_repository(runtime.sqlite_repository)
            observed = reader.bot_fill_window(sid, from_ms=state.session_open_ms, to_ms=now_ms_utc() + 1)
            fills = None if observed is None else observed.fills
        except (EconomicProjectionError, sqlite3.Error):
            logger.exception("Options leg economics are unavailable", extra={"action": "options_leg_pnl_unavailable", "strategy_instance_id": sid})
        finally:
            if reader is not None:
                reader.close()
    legs = [] if state is None else leg_views(state, minutes=minutes, fills=fills, order_legs=order_legs)
    return OptionsBotSessionView(
        strategy_instance_id=sid, golden_setup_id=binding.options_bot.golden_setup_id,
        session_open_ms=None if state is None else state.session_open_ms,
        session_close_ms=None if state is None else state.session_close_ms,
        last_minute_ms=None if state is None else state.last_minute_ms,
        next_entry_ms=notes.get("next_entry_ms"), entry_hold_reason=notes.get("entry_hold_reason"),
        execution_hold_reason=notes.get("execution_hold_reason"),
        pending_decisions=notes.get("pending_decisions", 0), entries_enabled=entries_enabled,
        resume_allowed=resumable,
        resume_explanation=(
            "This deployment has reached its chosen end. Resume is unavailable."
            if ended else "Resume restores recorded positions and remaining re-entries; entry spending needs a fresh review."
            if resumable else "Resume is available after an interruption. Intentional Stop stays stopped."
        ),
        legs=legs, events=[] if state is None else [OptionsBotEventView(
            kind=event.kind, at_ms=event.at_ms, leg_id=event.leg_id, entry_number=event.entry_number,
            contract=event.contract, price=None if event.price is None else str(event.price),
            reason=event.reason, detail=event.detail,
        ) for event in state.events],
        order_terms=binding.option_order_terms, exit_terms=binding.exit_terms,
    )


def leg_views(
    state: SessionState, *, minutes: Sequence[ConsumedMinute] = (), fills: Sequence[FillRecord] | None = None,
    order_legs: Mapping[tuple[str, str], str] | None = None,
) -> list[OptionsBotLegView]:
    """Each leg's progress; a session the entry minute has not started has no leg outcome yet."""
    views = []
    owners = order_legs or {}
    marks = {minute.symbol: minute.bar.close for minute in minutes if minute.bar is not None}
    for leg in state.legs:
        position = leg.position
        held = position is not None and position.contracts_held > 0
        symbols = list(dict.fromkeys(event.contract.occ_symbol for event in state.events
                                    if event.kind is EventKind.PICKED and event.leg_id == leg.setup.leg_id
                                    and event.contract is not None))
        watched = []
        for symbol in symbols:
            last = next((minute for minute in reversed(minutes) if minute.symbol == symbol and minute.bar is not None), None)
            watched.append(OptionsWatchedPremiumView(symbol=symbol, premium=None if last is None else str(last.bar.close),
                                                    close_ms=None if last is None else last.close_ms))
        attributed = [fill for fill in fills or () if owners.get((fill.order_ref, fill.symbol)) == leg.setup.leg_id]
        unassigned = any(fill.symbol in symbols and (fill.order_ref, fill.symbol) not in owners for fill in fills or ())
        if unassigned:
            realized, opened, notice = None, None, "P&L is unavailable: some effective fills have not been attributed to the session's legs."
        elif fills is None or (leg.entries_filled and not attributed):
            realized, opened, notice = None, None, "The session's attributed fills are unavailable."
        else:
            realized, opened, notice = leg_pnl(attributed, marks)
        attempt = leg.attempt
        views.append(OptionsBotLegView(
            leg_id=leg.setup.leg_id, side=leg.setup.side, phase=leg.phase if state.started else "not_started",
            entries_filled=leg.entries_filled, reentries_left=leg.reentries_left,
            held_contract=position.attempt.contract.occ_symbol if held else None,
            held_contracts=position.contracts_held if held else 0,
            average_fill_price=str(position.entry.filled) if held else None,
            stop_level=str(position.stop_level) if held and position.stop_level is not None else None,
            waiting_contract=attempt.contract.occ_symbol if attempt is not None and leg.phase.value in {"waiting", "ordered"} else None,
            starting_price=None if attempt is None or attempt.starting_price is None else str(attempt.starting_price),
            momentum_level=None if attempt is None or attempt.momentum_level is None else str(attempt.momentum_level),
            watched_contracts=watched, realized_pnl_usd=realized, open_pnl_usd=opened, pnl_notice=notice,
        ))
    return views


def leg_pnl(fills: Sequence[FillRecord], marks: Mapping[str, Decimal]) -> tuple[str, str | None, str]:
    """A leg's attributed fills valued by the existing FIFO implementation.

    Formula: FIFO realized and open P&L over this leg's confirmed fills, at saved premium closes.
    Reference: app/broker/alpaca/clerk/fifo_pnl.py.
    Canonical implementation: app/broker/alpaca/clerk/fifo_pnl.py (called here).
    Validated against: tests/services/test_options_bot_status.py.
    """
    result = compute_fifo_pnl(fills, mark_prices=marks)
    notice = ("Open P&L is unavailable: a held contract has no saved premium close. Realized P&L is before fees."
              if result.exact_open_pnl is None else "Before fees; open P&L uses the latest saved premium close.")
    return str(result.exact_realized_pnl), None if result.exact_open_pnl is None else str(result.exact_open_pnl), notice

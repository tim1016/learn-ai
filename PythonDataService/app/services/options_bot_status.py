"""Present durable Options rule progress separately from Clerk execution truth."""

from __future__ import annotations

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.engine.live.identity import strategy_instance_artifact_dir
from app.options_bots.journal import JOURNAL_FILENAME, OptionsBotJournal
from app.options_bots.session import SessionState
from app.schemas.options_deploy import OptionsBotLegView, OptionsBotSessionView
from app.services.bot_runner import BotTaskRegistry, RunAdmissionRefusedError


def options_session_view(registry: BotTaskRegistry, broker: str, sid: str) -> OptionsBotSessionView:
    binding = registry.binding_for_control(broker, sid)
    if binding.options_bot is None:
        raise RunAdmissionRefusedError("This bot follows a Signal Program and has no Options session.")
    instance_dir = strategy_instance_artifact_dir(registry.artifacts_root, "live_state", sid)
    state, notes = None, {}
    if (instance_dir / JOURNAL_FILENAME).is_file():
        journal = OptionsBotJournal(instance_dir)
        try:
            state, notes = journal.state, journal.status()
        finally:
            journal.close()
    status = registry.status(broker, sid)
    ended = registry.bot_end(broker, sid).status in {"ending", "ended"}
    resumable = not ended and status.duty_outcome is not None and status.duty_outcome.kind in {"CRASHED", "EXITED_UNVERIFIED"}
    runtime = get_clerk_runtime(binding.sealed_account_id) if binding.sealed_account_id is not None else None
    entries_enabled = status.running and runtime is not None and runtime.clerk.options_entries_enabled(sid)
    legs = [] if state is None else leg_views(state)
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
        legs=legs, events=notes.get("events", []),
    )


def leg_views(state: SessionState) -> list[OptionsBotLegView]:
    """Each leg's progress; a session the entry minute has not started has no leg outcome yet."""
    views = []
    for leg in state.legs:
        position = leg.position
        held = position is not None and position.contracts_held > 0
        views.append(OptionsBotLegView(
            leg_id=leg.setup.leg_id, side=leg.setup.side, phase=leg.phase if state.started else "not_started",
            entries_filled=leg.entries_filled, reentries_left=leg.reentries_left,
            held_contract=position.attempt.contract.occ_symbol if held else None,
            held_contracts=position.contracts_held if held else 0,
            average_fill_price=str(position.entry.filled) if held else None,
            stop_level=str(position.stop_level) if held and position.stop_level is not None else None,
        ))
    return views

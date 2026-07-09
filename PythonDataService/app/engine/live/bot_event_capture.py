"""Live-runtime Bot event raw capture helpers (ADR 0024 / PRD #928)."""

from __future__ import annotations

import logging
from pathlib import Path

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    FactValue,
    GateStep,
    SourceAuthority,
    TerminalError,
    TerminalErrorSource,
)
from app.services.bot_event_wal import BotEventRawWal, run_bot_event_wal_path

logger = logging.getLogger(__name__)


def bot_event_wal_for_run(
    *,
    run_dir: Path | None,
    run_id: str,
    strategy_instance_id: str,
) -> BotEventRawWal | None:
    if run_dir is None or not run_id or not strategy_instance_id:
        return None
    return BotEventRawWal(run_bot_event_wal_path(run_dir), trusted_root=run_dir)


def launch_identity_for_run(run_id: str) -> BotEventIdentity:
    """Synthetic spine identity for pre-evaluation launch failures."""

    return BotEventIdentity(evaluation_id=f"launch:{run_id}")


def halt_identity_for_run(run_id: str, terminal_error: TerminalError) -> BotEventIdentity:
    """Best available identity for terminal halts that may have no bar/order."""

    facts = terminal_error.forensic_facts
    identity = {
        "evaluation_id": _str_fact(facts, "evaluation_id"),
        "intent_id": _str_fact(facts, "intent_id"),
        "order_ref": _str_fact(facts, "order_ref"),
        "req_id": _int_fact(facts, "req_id"),
        "order_id": _int_fact(facts, "order_id"),
        "perm_id": _int_fact(facts, "perm_id"),
        "exec_id": _str_fact(facts, "exec_id"),
    }
    if not any(value is not None for value in identity.values()):
        identity["evaluation_id"] = f"halt:{run_id}"
    return BotEventIdentity(**identity)


def evaluation_id_for_bar(bar_close_ms: int) -> str:
    return f"bar:{bar_close_ms}"


def source_authority_for_terminal_error(terminal_error: TerminalError) -> SourceAuthority:
    if terminal_error.source in {TerminalErrorSource.IBKR, TerminalErrorSource.BROKER_SESSION}:
        return SourceAuthority.BROKER_SESSION
    if terminal_error.source in {TerminalErrorSource.DAEMON, TerminalErrorSource.OS}:
        return SourceAuthority.DAEMON_LAUNCHER
    return SourceAuthority.ENGINE_LOOP


def record_halted_terminal_event(
    *,
    run_dir: Path | None,
    run_id: str,
    strategy_instance_id: str,
    exc: BaseException,
    ts_ms: int,
) -> BotEventRaw | None:
    """Classify a live-runtime halt, append the raw event, and mint its incident."""

    from app.operator.incidents.store import IncidentStore
    from app.services.bot_event_incidents import append_terminal_incident
    from app.services.bot_event_terminal_classifier import classify_terminal_exception

    recorder = BotEventTerminalRecorder.for_run(
        run_dir=run_dir,
        run_id=run_id,
        strategy_instance_id=strategy_instance_id,
    )
    if recorder is None:
        logger.warning(
            "live halt occurred but Bot event terminal recorder is unavailable",
            extra={"run_id": run_id, "strategy_instance_id": strategy_instance_id},
        )
        return None
    try:
        classification = classify_terminal_exception(exc)
        raw_event = recorder.append_halted(
            ts_ms=ts_ms,
            terminal_error=classification.terminal_error,
            facts={
                "notice_code": classification.notice_code,
                "action_kind": classification.action_kind,
                "terminal_title": classification.title,
            },
        )
        if run_dir is not None:
            append_terminal_incident(IncidentStore(run_dir), raw_event)
        return raw_event
    except Exception:
        logger.exception(
            "Failed to record halted Bot event",
            extra={"run_id": run_id, "strategy_instance_id": strategy_instance_id},
        )
        return None


def _str_fact(facts: dict[str, FactValue], key: str) -> str | None:
    value = facts.get(key)
    return value if isinstance(value, str) and value else None


def _int_fact(facts: dict[str, FactValue], key: str) -> int | None:
    value = facts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class BotEventTerminalRecorder:
    """Append enforcement-point terminal events to a run-scoped bot-event WAL."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        strategy_instance_id: str,
        wal: BotEventRawWal | None = None,
    ) -> None:
        self._run_id = run_id
        self._strategy_instance_id = strategy_instance_id
        self._wal = (
            wal
            if wal is not None
            else BotEventRawWal(run_bot_event_wal_path(run_dir), trusted_root=run_dir)
        )

    @classmethod
    def for_run(
        cls,
        *,
        run_dir: Path | None,
        run_id: str,
        strategy_instance_id: str,
        wal: BotEventRawWal | None = None,
    ) -> BotEventTerminalRecorder | None:
        if run_dir is None or not run_id or not strategy_instance_id:
            return None
        return cls(run_dir=run_dir, run_id=run_id, strategy_instance_id=strategy_instance_id, wal=wal)

    def append_launch_failed(
        self,
        *,
        ts_ms: int,
        terminal_error: TerminalError,
        facts: dict[str, FactValue] | None = None,
    ) -> BotEventRaw:
        return self._append_terminal_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.LAUNCH_FAILED,
            source_authority=SourceAuthority.DAEMON_LAUNCHER,
            identity=launch_identity_for_run(self._run_id),
            terminal_error=terminal_error,
            facts=facts,
        )

    def append_halted(
        self,
        *,
        ts_ms: int,
        terminal_error: TerminalError,
        facts: dict[str, FactValue] | None = None,
    ) -> BotEventRaw:
        return self._append_terminal_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.HALTED,
            source_authority=source_authority_for_terminal_error(terminal_error),
            identity=halt_identity_for_run(self._run_id, terminal_error),
            terminal_error=terminal_error,
            facts=facts,
        )

    def _append_terminal_event(
        self,
        *,
        ts_ms: int,
        event_type: BotEventRawType,
        source_authority: SourceAuthority,
        identity: BotEventIdentity,
        terminal_error: TerminalError,
        facts: dict[str, FactValue] | None,
    ) -> BotEventRaw:
        raw_event = BotEventRaw(
            seq=self._wal.allocate_seq(),
            ts_ms=ts_ms,
            strategy_instance_id=self._strategy_instance_id,
            run_id=self._run_id,
            event_type=event_type,
            source_authority=source_authority,
            identity=identity,
            terminal_error=terminal_error,
            facts=facts or {},
        )
        self._wal.append_event(raw_event)
        return raw_event


class BotEventGateStepRecorder:
    """Append enforcement-point gate-steps to a run-scoped bot-event WAL."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        strategy_instance_id: str,
        wal: BotEventRawWal | None = None,
    ) -> None:
        self._run_id = run_id
        self._strategy_instance_id = strategy_instance_id
        self._wal = (
            wal
            if wal is not None
            else BotEventRawWal(run_bot_event_wal_path(run_dir), trusted_root=run_dir)
        )

    @classmethod
    def for_run(
        cls,
        *,
        run_dir: Path | None,
        run_id: str,
        strategy_instance_id: str,
        wal: BotEventRawWal | None = None,
    ) -> BotEventGateStepRecorder | None:
        if run_dir is None or not run_id or not strategy_instance_id:
            return None
        return cls(run_dir=run_dir, run_id=run_id, strategy_instance_id=strategy_instance_id, wal=wal)

    def append_many(
        self,
        *,
        ts_ms: int,
        gate_steps: tuple[GateStep, ...],
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        for gate_step in gate_steps:
            self.append(ts_ms=ts_ms, gate_step=gate_step, facts=facts)

    def append(
        self,
        *,
        ts_ms: int,
        gate_step: GateStep,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._wal.append_event(
            BotEventRaw(
                seq=self._wal.allocate_seq(),
                ts_ms=ts_ms,
                strategy_instance_id=self._strategy_instance_id,
                run_id=self._run_id,
                event_type=BotEventRawType.GATE_STEP,
                source_authority=gate_step.source_authority,
                identity=BotEventIdentity(evaluation_id=gate_step.evaluation_id),
                gate_step=gate_step,
                facts=facts or {},
            )
        )


class BotEventSpineRecorder:
    """Append evaluation/order spine events to the run-scoped bot-event WAL."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        strategy_instance_id: str,
        wal: BotEventRawWal | None = None,
    ) -> None:
        self._run_id = run_id
        self._strategy_instance_id = strategy_instance_id
        self._wal = (
            wal
            if wal is not None
            else BotEventRawWal(run_bot_event_wal_path(run_dir), trusted_root=run_dir)
        )

    @classmethod
    def for_run(
        cls,
        *,
        run_dir: Path | None,
        run_id: str,
        strategy_instance_id: str,
        wal: BotEventRawWal | None = None,
    ) -> BotEventSpineRecorder | None:
        if run_dir is None or not run_id or not strategy_instance_id:
            return None
        return cls(run_dir=run_dir, run_id=run_id, strategy_instance_id=strategy_instance_id, wal=wal)

    def append_evaluation_idle(
        self,
        *,
        ts_ms: int,
        evaluation_id: str,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._append_spine_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.EVALUATION_IDLE,
            identity=BotEventIdentity(evaluation_id=evaluation_id),
            facts=facts,
        )

    def append_signal_fired(
        self,
        *,
        ts_ms: int,
        evaluation_id: str,
        intent_id: str | None = None,
        order_ref: str | None = None,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._append_spine_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.SIGNAL_FIRED,
            identity=BotEventIdentity(
                evaluation_id=evaluation_id,
                intent_id=intent_id,
                order_ref=order_ref,
            ),
            facts=facts,
        )

    def append_order_submitted(
        self,
        *,
        ts_ms: int,
        identity: BotEventIdentity,
        source_authority: SourceAuthority = SourceAuthority.ENGINE_LOOP,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._append_spine_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.ORDER_SUBMITTED,
            identity=identity,
            source_authority=source_authority,
            facts=facts,
        )

    def append_order_filled(
        self,
        *,
        ts_ms: int,
        identity: BotEventIdentity,
        source_authority: SourceAuthority = SourceAuthority.ENGINE_LOOP,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._append_spine_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.ORDER_FILLED,
            identity=identity,
            source_authority=source_authority,
            facts=facts,
        )

    def append_order_cancelled(
        self,
        *,
        ts_ms: int,
        identity: BotEventIdentity,
        source_authority: SourceAuthority = SourceAuthority.ENGINE_LOOP,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._append_spine_event(
            ts_ms=ts_ms,
            event_type=BotEventRawType.ORDER_CANCELLED,
            identity=identity,
            source_authority=source_authority,
            facts=facts,
        )

    def _append_spine_event(
        self,
        *,
        ts_ms: int,
        event_type: BotEventRawType,
        identity: BotEventIdentity,
        source_authority: SourceAuthority = SourceAuthority.ENGINE_LOOP,
        facts: dict[str, FactValue] | None = None,
    ) -> None:
        self._wal.append_event(
            BotEventRaw(
                seq=self._wal.allocate_seq(),
                ts_ms=ts_ms,
                strategy_instance_id=self._strategy_instance_id,
                run_id=self._run_id,
                event_type=event_type,
                source_authority=source_authority,
                identity=identity,
                facts=facts or {},
            )
        )

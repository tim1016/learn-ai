"""Live-runtime Bot event raw capture helpers (ADR 0024 / PRD #928)."""

from __future__ import annotations

from pathlib import Path

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    FactValue,
    GateStep,
    SourceAuthority,
    TerminalError,
)
from app.services.bot_event_wal import BotEventRawWal, run_bot_event_wal_path


def bot_event_wal_for_run(
    *,
    run_dir: Path | None,
    run_id: str,
    strategy_instance_id: str,
) -> BotEventRawWal | None:
    if run_dir is None or not run_id or not strategy_instance_id:
        return None
    return BotEventRawWal(run_bot_event_wal_path(run_dir))


def launch_identity_for_run(run_id: str) -> BotEventIdentity:
    """Synthetic spine identity for pre-evaluation launch failures."""

    return BotEventIdentity(evaluation_id=f"launch:{run_id}")


def evaluation_id_for_bar(bar_close_ms: int) -> str:
    return f"bar:{bar_close_ms}"


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
        self._wal = wal if wal is not None else BotEventRawWal(run_bot_event_wal_path(run_dir))

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
    ) -> None:
        self._wal.append_event(
            BotEventRaw(
                seq=self._wal.allocate_seq(),
                ts_ms=ts_ms,
                strategy_instance_id=self._strategy_instance_id,
                run_id=self._run_id,
                event_type=BotEventRawType.LAUNCH_FAILED,
                source_authority=SourceAuthority.DAEMON_LAUNCHER,
                identity=launch_identity_for_run(self._run_id),
                terminal_error=terminal_error,
                facts=facts or {},
            )
        )


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
        self._wal = wal if wal is not None else BotEventRawWal(run_bot_event_wal_path(run_dir))

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
        self._wal = wal if wal is not None else BotEventRawWal(run_bot_event_wal_path(run_dir))

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

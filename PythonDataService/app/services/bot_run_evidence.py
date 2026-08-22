"""Durable terminal evidence plus current and previous bot-run projections."""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
)
from app.schemas.bot_run_evidence import BotCrashDiagnostic, BotRunTerminalOutcomeView
from app.schemas.broker_bots import (
    BotProcessFact,
    BotRunHistoryPage,
    BotRunView,
)
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BotRunOutcomeRecord,
    BotRunRecord,
    BrokerBotBinding,
)
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleProjectionResult,
    AlpacaLifecycleProjector,
)
from app.services.bot_runner_errors import (
    InvalidRunHistoryCursorError,
    UnknownBotError,
)

PROVISIONAL_STOP_REASON_CODE = "STOPPED_PENDING_CUSTODY_PROOF"

logger = logging.getLogger(__name__)


class BotRunEvidenceService:
    """Own run terminal receipts and compose command-neutral run views."""

    def __init__(
        self,
        repository: BotBindingRepository,
        *,
        lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo],
        lifecycle_projector: AlpacaLifecycleProjector,
        lifecycle_projector_for: Callable[[str], AlpacaLifecycleProjector] | None = None,
    ) -> None:
        self._repository = repository
        self._lifecycle_repo_for = lifecycle_repo_for
        self._lifecycle_projector = lifecycle_projector
        self._lifecycle_projector_for = lifecycle_projector_for or (lambda _sid: lifecycle_projector)

    def preserve_terminal(
        self,
        strategy_instance_id: str,
        lifecycle: BotLifecycleStateRecord | None,
    ) -> None:
        """Reuse the prior run's authoritative receipt before a new run clears it.

        The receipt (`run_outcomes/{run_id}.json`) is create-once terminal
        evidence; the lifecycle projection is a lower-fidelity summary of it
        and never overwrites or competes with an existing receipt. Only a
        genuinely absent receipt is synthesized from the projection — an
        existing receipt is read and, if present, left untouched. A receipt
        that fails to read propagates rather than falling through to
        reconstruction.
        """
        if (
            lifecycle is None
            or lifecycle.duty_outcome is None
            or lifecycle.duty_outcome.reason_code == PROVISIONAL_STOP_REASON_CODE
        ):
            return
        outcome = lifecycle.duty_outcome
        if outcome.run_id is None:
            return
        existing = self._repository.read_outcome(strategy_instance_id, outcome.run_id)
        if existing is not None:
            if (
                existing.kind != outcome.kind
                or existing.reason_code != outcome.reason_code
                or existing.recorded_at_ms != outcome.recorded_at_ms
            ):
                logger.warning(
                    "Terminal receipt disagrees with the lifecycle projection; "
                    "the receipt remains authoritative",
                    extra={
                        "action": "terminal_receipt_projection_disagreement",
                        "strategy_instance_id": strategy_instance_id,
                        "run_id": outcome.run_id,
                        "receipt_kind": existing.kind,
                        "receipt_reason_code": existing.reason_code,
                        "receipt_recorded_at_ms": existing.recorded_at_ms,
                        "projection_kind": outcome.kind,
                        "projection_reason_code": outcome.reason_code,
                        "projection_recorded_at_ms": outcome.recorded_at_ms,
                    },
                )
            return
        self._record_terminal_receipt(strategy_instance_id, outcome)

    def record_terminal(
        self,
        strategy_instance_id: str,
        outcome: BotDutyOutcome,
        *,
        updated_by: str,
        reason: str,
        expected_active_run_id: str | None = None,
        persist_receipt: bool = True,
        crash_diagnostic: BotCrashDiagnostic | None = None,
    ) -> AlpacaLifecycleProjectionResult:
        """Publish immutable evidence before mutating the lifecycle projection."""
        if persist_receipt:
            self._record_terminal_receipt(
                strategy_instance_id,
                outcome,
                crash_diagnostic=crash_diagnostic,
            )
        run_id = expected_active_run_id or outcome.run_id
        if run_id is None:
            return self._lifecycle_projector_for(strategy_instance_id).refresh(
                strategy_instance_id=strategy_instance_id,
                now_ms=outcome.recorded_at_ms,
                updated_by=updated_by,
                reason=reason,
            )
        return self._lifecycle_projector_for(strategy_instance_id).project_terminal(
            strategy_instance_id=strategy_instance_id,
            outcome=outcome,
            now_ms=outcome.recorded_at_ms,
            updated_by=updated_by,
            reason=reason,
        )

    def _record_terminal_receipt(
        self,
        strategy_instance_id: str,
        outcome: BotDutyOutcome,
        *,
        crash_diagnostic: BotCrashDiagnostic | None = None,
    ) -> None:
        if outcome.run_id is None:
            return
        self._repository.record_outcome(
            BotRunOutcomeRecord(
                strategy_instance_id=strategy_instance_id,
                run_id=outcome.run_id,
                kind=outcome.kind,
                reason_code=outcome.reason_code,
                recorded_at_ms=outcome.recorded_at_ms,
                crash_diagnostic=crash_diagnostic,
            )
        )

    def current(
        self,
        binding: BrokerBotBinding,
        process: BotProcessFact,
    ) -> BotRunView:
        """Return the current run with process and terminal authorities intact."""
        record = self._repository.read_run(
            binding.strategy_instance_id,
            binding.run_id,
        )
        if record is None:
            raise UnknownBotError(
                f"Current run '{binding.run_id}' has no launch evidence.",
                detail="Recover the bot run artifacts before requesting current-run state.",
            )
        return self._compose(
            record,
            is_current=True,
            process=process,
        )

    def history(
        self,
        binding: BrokerBotBinding,
        *,
        cursor: str | None,
        limit: int,
    ) -> BotRunHistoryPage:
        """Return one bounded newest-first page of non-current runs."""
        if limit < 1 or limit > 25:
            raise InvalidRunHistoryCursorError(
                "Run-history limit must be between 1 and 25.",
                detail="Request a bounded page of previous runs.",
            )
        previous = [
            run
            for run in self._repository.list_runs(binding.strategy_instance_id)
            if run.run_id != binding.run_id
        ]
        start = self._page_start(previous, cursor)
        selected = previous[start : start + limit]
        next_cursor = (
            selected[-1].run_id
            if selected and start + len(selected) < len(previous)
            else None
        )
        return BotRunHistoryPage(
            runs=tuple(
                self._compose(run, is_current=False, process=None)
                for run in selected
            ),
            next_cursor=next_cursor,
        )

    @staticmethod
    def _page_start(records: list[BotRunRecord], cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            return next(
                index + 1
                for index, run in enumerate(records)
                if run.run_id == cursor
            )
        except StopIteration as exc:
            raise InvalidRunHistoryCursorError(
                "Run-history cursor is not part of this strategy instance.",
                detail="Restart history navigation from the first page.",
            ) from exc

    def _compose(
        self,
        record: BotRunRecord,
        *,
        is_current: bool,
        process: BotProcessFact | None,
    ) -> BotRunView:
        outcome = self._repository.read_outcome(
            record.strategy_instance_id,
            record.run_id,
        )
        terminal = (
            BotRunTerminalOutcomeView(
                kind=outcome.kind,
                reason_code=outcome.reason_code,
                recorded_at_ms=outcome.recorded_at_ms,
                run_id=outcome.run_id,
                crash_diagnostic=outcome.crash_diagnostic,
            )
            if outcome is not None
            else self._current_lifecycle_outcome(record, is_current=is_current)
        )
        return BotRunView(
            strategy_instance_id=record.strategy_instance_id,
            run_id=record.run_id,
            configuration_hash=record.configuration_hash,
            launch_reason=record.launch_reason,
            started_at_ms=record.started_at_ms,
            is_current=is_current,
            process=process,
            terminal_outcome=terminal,
        )

    def _current_lifecycle_outcome(
        self,
        record: BotRunRecord,
        *,
        is_current: bool,
    ) -> BotRunTerminalOutcomeView | None:
        if not is_current:
            return None
        lifecycle = self._lifecycle_repo_for(record.strategy_instance_id).read()
        if (
            lifecycle is None
            or lifecycle.duty_outcome is None
            or lifecycle.duty_outcome.run_id != record.run_id
        ):
            return None
        return BotRunTerminalOutcomeView(
            kind=lifecycle.duty_outcome.kind,
            reason_code=lifecycle.duty_outcome.reason_code,
            recorded_at_ms=lifecycle.duty_outcome.recorded_at_ms,
            run_id=lifecycle.duty_outcome.run_id,
        )

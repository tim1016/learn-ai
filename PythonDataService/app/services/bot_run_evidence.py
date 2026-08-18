"""Durable terminal evidence plus current and previous bot-run projections."""

from __future__ import annotations

from collections.abc import Callable

from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
    BotLifecycleStateUpdateResult,
)
from app.schemas.broker_bots import (
    BotDutyOutcomeView,
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
from app.services.bot_runner_errors import (
    InvalidRunHistoryCursorError,
    UnknownBotError,
)

LifecycleRepoResolver = Callable[[str], BotLifecycleStateRepo]
PROVISIONAL_STOP_REASON_CODE = "STOPPED_PENDING_CUSTODY_PROOF"


class BotRunEvidenceService:
    """Own run terminal receipts and compose command-neutral run views."""

    def __init__(
        self,
        repository: BotBindingRepository,
        *,
        lifecycle_repo_for: LifecycleRepoResolver,
    ) -> None:
        self._repository = repository
        self._lifecycle_repo_for = lifecycle_repo_for

    def preserve_terminal(
        self,
        strategy_instance_id: str,
        lifecycle: BotLifecycleStateRecord | None,
    ) -> None:
        """Copy the terminal projection before a new run clears it."""
        if (
            lifecycle is None
            or lifecycle.duty_outcome is None
            or lifecycle.duty_outcome.reason_code == PROVISIONAL_STOP_REASON_CODE
        ):
            return
        self._record_terminal_receipt(strategy_instance_id, lifecycle.duty_outcome)

    def record_terminal(
        self,
        strategy_instance_id: str,
        outcome: BotDutyOutcome,
        *,
        updated_by: str,
        reason: str,
        expected_active_run_id: str | None = None,
        persist_receipt: bool = True,
    ) -> BotLifecycleStateUpdateResult:
        """Publish immutable evidence before mutating the lifecycle projection."""
        if persist_receipt:
            self._record_terminal_receipt(strategy_instance_id, outcome)
        return self._lifecycle_repo_for(strategy_instance_id).record_terminal_outcome(
            outcome,
            updated_by=updated_by,
            reason=reason,
            expected_active_run_id=expected_active_run_id,
        )

    def _record_terminal_receipt(
        self,
        strategy_instance_id: str,
        outcome: BotDutyOutcome,
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
            BotDutyOutcomeView(
                kind=outcome.kind,
                reason_code=outcome.reason_code,
                recorded_at_ms=outcome.recorded_at_ms,
                run_id=outcome.run_id,
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
    ) -> BotDutyOutcomeView | None:
        if not is_current:
            return None
        lifecycle = self._lifecycle_repo_for(record.strategy_instance_id).read()
        if (
            lifecycle is None
            or lifecycle.duty_outcome is None
            or lifecycle.duty_outcome.run_id != record.run_id
        ):
            return None
        return BotDutyOutcomeView(
            kind=lifecycle.duty_outcome.kind,
            reason_code=lifecycle.duty_outcome.reason_code,
            recorded_at_ms=lifecycle.duty_outcome.recorded_at_ms,
            run_id=lifecycle.duty_outcome.run_id,
        )

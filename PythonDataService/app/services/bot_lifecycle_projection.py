"""Revision-fenced lifecycle projection for the Alpaca bot-control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
)

_MAX_AUTHORITY_RETRIES = 8
FileRefusalReason = Literal["ACTIVE_RUN_MISMATCH", "VERSION_MISMATCH"]


class ProjectionStatus(StrEnum):
    RECORDED = "RECORDED"
    AUTHORITY_EXPECTATION_SUPERSEDED = "AUTHORITY_EXPECTATION_SUPERSEDED"
    FILE_CAS_REFUSED = "FILE_CAS_REFUSED"
    AUTHORITY_CHANGED = "AUTHORITY_CHANGED"


class AlpacaLifecycleAuthorityUnavailableError(RuntimeError):
    """The active Clerk cannot prove SQLite lifecycle facts."""


@dataclass(frozen=True, slots=True)
class AlpacaLifecycleAuthoritySnapshot:
    strategy_instance_exists: bool
    active_run_id: str | None
    retired_at_ms: int | None
    expected_run_state: Literal["ACTIVE", "STOPPED", "MISSING"] | None
    control_revision: int


class AlpacaLifecycleAuthority(Protocol):
    def snapshot(
        self,
        strategy_instance_id: str,
        expected_run_id: str | None,
    ) -> AlpacaLifecycleAuthoritySnapshot: ...


class SqliteAlpacaLifecycleAuthority:
    """Read the lifecycle facts SQLite already owns."""

    def __init__(self, repository: ClerkSqliteRepository) -> None:
        self._repository = repository

    def snapshot(
        self,
        strategy_instance_id: str,
        expected_run_id: str | None,
    ) -> AlpacaLifecycleAuthoritySnapshot:
        snapshot = self._repository.lifecycle_projection_snapshot(
            strategy_instance_id,
            expected_run_id,
        )
        return AlpacaLifecycleAuthoritySnapshot(
            strategy_instance_exists=snapshot.strategy_instance_exists,
            active_run_id=snapshot.active_run_id,
            retired_at_ms=snapshot.retired_at_ms,
            expected_run_state=snapshot.expected_run_state,
            control_revision=snapshot.control_revision,
        )


class ActiveSqliteAlpacaLifecycleAuthority:
    """Resolve the process-selected SQLite Clerk at each projection boundary."""

    def recovery_candidates(self) -> tuple[tuple[str, str], ...]:
        """Enumerate SQLite-active runs, including pre-binding crash windows."""

        from app.broker.alpaca.clerk import get_alpaca_clerk

        clerk = get_alpaca_clerk()
        candidate_reader = getattr(clerk, "lifecycle_recovery_candidates", None)
        if callable(candidate_reader):
            return tuple(candidate_reader())
        repository = getattr(clerk, "repository", None)
        if getattr(clerk, "authority_kind", None) != "sqlite" or not isinstance(
            repository, ClerkSqliteRepository
        ):
            raise AlpacaLifecycleAuthorityUnavailableError(
                "SQLite Alpaca lifecycle authority is unavailable"
            )
        return tuple(
            (candidate.strategy_instance_id, candidate.run_id)
            for candidate in repository.lifecycle_recovery_candidates()
        )

    def snapshot(
        self,
        strategy_instance_id: str,
        expected_run_id: str | None,
    ) -> AlpacaLifecycleAuthoritySnapshot:
        from app.broker.alpaca.clerk import get_alpaca_clerk

        clerk = get_alpaca_clerk()
        snapshotter = getattr(clerk, "lifecycle_snapshot", None)
        if callable(snapshotter):
            return snapshotter(strategy_instance_id, expected_run_id)
        repository = getattr(clerk, "repository", None)
        if getattr(clerk, "authority_kind", None) != "sqlite" or not isinstance(
            repository, ClerkSqliteRepository
        ):
            raise AlpacaLifecycleAuthorityUnavailableError(
                "SQLite Alpaca lifecycle authority is unavailable"
            )
        return SqliteAlpacaLifecycleAuthority(repository).snapshot(
            strategy_instance_id,
            expected_run_id,
        )


@dataclass(frozen=True, slots=True)
class AlpacaLifecycleProjectionResult:
    status: ProjectionStatus
    record: BotLifecycleStateRecord | None
    terminal_outcome_attached: bool
    refusal_reason: FileRefusalReason | None = None


@dataclass(frozen=True, slots=True)
class _ActiveProjection:
    run_id: str
    carryover_policy: Literal["FORBID", "ALLOW"]


@dataclass(frozen=True, slots=True)
class _TerminalProjection:
    run_id: str
    outcome: BotDutyOutcome


@dataclass(frozen=True, slots=True)
class _RefreshProjection:
    pass


_ProjectionIntent = _ActiveProjection | _TerminalProjection | _RefreshProjection


class AlpacaLifecycleProjector:
    """Project SQLite duty facts without accepting caller-authored duty state."""

    def __init__(
        self,
        *,
        authority: AlpacaLifecycleAuthority,
        lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo],
        require_alpaca_identity: Callable[[str, bool], None],
    ) -> None:
        self._authority = authority
        self._lifecycle_repo_for = lifecycle_repo_for
        self._require_alpaca_identity = require_alpaca_identity

    def project_active(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        now_ms: int,
        updated_by: str,
        reason: str,
        carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID",
    ) -> AlpacaLifecycleProjectionResult:
        return self._project(
            strategy_instance_id=strategy_instance_id,
            intent=_ActiveProjection(run_id=run_id, carryover_policy=carryover_policy),
            now_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
        )

    def project_terminal(
        self,
        *,
        strategy_instance_id: str,
        outcome: BotDutyOutcome,
        now_ms: int,
        updated_by: str,
        reason: str,
    ) -> AlpacaLifecycleProjectionResult:
        if outcome.run_id is None:
            raise ValueError("terminal projection requires an identified run")
        return self._project(
            strategy_instance_id=strategy_instance_id,
            intent=_TerminalProjection(run_id=outcome.run_id, outcome=outcome),
            now_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
        )

    def refresh(
        self,
        *,
        strategy_instance_id: str,
        now_ms: int,
        updated_by: str,
        reason: str,
    ) -> AlpacaLifecycleProjectionResult:
        return self._project(
            strategy_instance_id=strategy_instance_id,
            intent=_RefreshProjection(),
            now_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
        )

    def _project(
        self,
        *,
        strategy_instance_id: str,
        intent: _ProjectionIntent,
        now_ms: int,
        updated_by: str,
        reason: str,
    ) -> AlpacaLifecycleProjectionResult:
        """Use SQLite -> file -> SQLite ordering; the locks are never nested."""

        expected_run_id = _expected_run_id(intent)
        repo = self._lifecycle_repo_for(strategy_instance_id)
        for _attempt in range(_MAX_AUTHORITY_RETRIES):
            authority = self._authority.snapshot(strategy_instance_id, expected_run_id)
            expectation_matches = _expectation_matches(intent, authority)
            attach_terminal = expectation_matches and isinstance(intent, _TerminalProjection)
            current = repo.read()
            self._require_alpaca_identity(
                strategy_instance_id,
                authority.strategy_instance_exists,
            )
            phase = _authority_phase(authority)
            update = repo.update(
                now_ms=now_ms,
                updated_by=updated_by,
                phase=phase,
                active_run_id=authority.active_run_id,
                reason=reason,
                retired_at_ms=authority.retired_at_ms,
                duty_outcome=intent.outcome if attach_terminal else None,
                carryover_policy=(
                    intent.carryover_policy if isinstance(intent, _ActiveProjection) else None
                ),
                clear_retirement=authority.retired_at_ms is None,
                clear_duty_outcome=phase is BotLifecyclePhase.ON_DUTY,
                expected_version=current.version if current is not None else None,
            )
            if update.status == "SUPERSEDED":
                return AlpacaLifecycleProjectionResult(
                    status=ProjectionStatus.FILE_CAS_REFUSED,
                    record=update.record,
                    terminal_outcome_attached=False,
                    refusal_reason=update.refusal_reason,
                )
            record = update.require_recorded()
            verified = self._authority.snapshot(strategy_instance_id, expected_run_id)
            if verified.control_revision != authority.control_revision:
                continue
            return AlpacaLifecycleProjectionResult(
                status=(
                    ProjectionStatus.RECORDED
                    if expectation_matches
                    else ProjectionStatus.AUTHORITY_EXPECTATION_SUPERSEDED
                ),
                record=record,
                terminal_outcome_attached=attach_terminal,
            )
        return AlpacaLifecycleProjectionResult(
            status=ProjectionStatus.AUTHORITY_CHANGED,
            record=repo.read(),
            terminal_outcome_attached=False,
        )


def _expected_run_id(intent: _ProjectionIntent) -> str | None:
    return intent.run_id if isinstance(intent, (_ActiveProjection, _TerminalProjection)) else None


def _authority_phase(authority: AlpacaLifecycleAuthoritySnapshot) -> BotLifecyclePhase:
    if authority.retired_at_ms is not None:
        return BotLifecyclePhase.RETIRED
    if authority.active_run_id is not None:
        return BotLifecyclePhase.ON_DUTY
    return BotLifecyclePhase.OFF_DUTY


def _expectation_matches(
    intent: _ProjectionIntent,
    authority: AlpacaLifecycleAuthoritySnapshot,
) -> bool:
    if isinstance(intent, _RefreshProjection):
        return True
    if isinstance(intent, _ActiveProjection):
        return (
            authority.active_run_id == intent.run_id
            and authority.expected_run_state == "ACTIVE"
        )
    return (
        authority.active_run_id is None
        and authority.expected_run_state == "STOPPED"
    )


__all__ = [
    "ActiveSqliteAlpacaLifecycleAuthority",
    "AlpacaLifecycleAuthority",
    "AlpacaLifecycleAuthoritySnapshot",
    "AlpacaLifecycleAuthorityUnavailableError",
    "AlpacaLifecycleProjectionResult",
    "AlpacaLifecycleProjector",
    "ProjectionStatus",
    "SqliteAlpacaLifecycleAuthority",
]

"""Boot-time repair of durable bot lifecycle artifacts.

This collaborator never owns asyncio tasks. The task registry supplies its
current liveness and broker-ownership predicates; the sweep only repairs
durable lifecycle intent and records interrupted runs after a container boot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateCorruptError,
    BotLifecycleStateRepo,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRepo
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleAuthorityUnavailableError,
    AlpacaLifecycleProjectionResult,
    AlpacaLifecycleProjector,
    ProjectionStatus,
)

logger = logging.getLogger(__name__)


class BootRecoveryReport(BaseModel):
    """What the boot sweep found and did (S5, #1263)."""

    model_config = ConfigDict(frozen=True)

    interrupted_instances: tuple[str, ...]
    unresolved_intents: int
    completed_at_ms: int
    # Bots the sweep could not project because no SQLite lifecycle authority
    # was installed. Their durable evidence is left exactly as the dead
    # process wrote it; start admission keeps the gate closed while this is
    # non-empty (``resolve_start_runtime_fact``).
    authority_unavailable_instances: tuple[str, ...]
    # Bindings sealed on a custody account the installed authority does not
    # custody -- after graduation, the rehearsal's ``shadow:<live_account_id>``
    # bindings under the live primary (ADR 0059 slice 7, R15); left as their
    # own files say; Start refuses them ``SEALED_ACCOUNT_MISMATCH``. Unlike
    # ``authority_unavailable_instances`` this never closes the start gate:
    # a foreign binding is refused one at a time, and the instances the
    # installed authority does custody stay startable.
    foreign_instances: tuple[str, ...] = ()


class BootAuthorityPreparationError(RuntimeError):
    """SQLite recovery or reconciliation failed before projection repair."""


@dataclass(frozen=True, slots=True)
class BotRecoveryCandidate:
    """One binding- or SQLite-derived run that boot must reconcile."""

    strategy_instance_id: str
    run_id: str
    sqlite_active: bool


@dataclass(frozen=True, slots=True)
class _ForeignBinding:
    """A binding the installed primary authority does not custody."""

    strategy_instance_id: str
    sealed_account_id: str
    installed_account_id: str


@dataclass(frozen=True, slots=True)
class RecoverySweepProvenance:
    """Who ran the lifecycle repair pass, stamped into what it writes.

    ADR 0050 runs the same repair pass in two circumstances -- container
    boot and in-process lease revival -- and the records it writes must say
    which one actually happened, not claim a restart that never occurred.
    """

    updated_by: str
    interrupted_reason_code: str
    interrupted_reason: str
    desired_state_reason: str


BOOT_SWEEP_PROVENANCE = RecoverySweepProvenance(
    updated_by="bot_runner_boot_sweep",
    interrupted_reason_code="INTERRUPTED_BY_RESTART",
    interrupted_reason="container_restart",
    desired_state_reason="interrupted_by_restart",
)

LEASE_REVIVAL_PROVENANCE = RecoverySweepProvenance(
    updated_by="bot_runner_lease_revival",
    interrupted_reason_code="INTERRUPTED_BY_AUTHORITY_OUTAGE",
    interrupted_reason="execution_lease_revival",
    desired_state_reason="interrupted_by_authority_outage",
)


class BotBootRecovery:
    """Repair interrupted durable state and run the Clerk recovery sequence."""

    def __init__(
        self,
        artifacts_root: Path,
        *,
        lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo],
        lifecycle_projector: AlpacaLifecycleProjector,
        lifecycle_projector_for: Callable[[str], AlpacaLifecycleProjector] | None = None,
        desired_repo_for: Callable[[str], DesiredStateRepo],
        recovery_candidates: Callable[[], Iterable[BotRecoveryCandidate]],
        stop_authority_run: Callable[[str, str], Awaitable[None]],
        manages_instance: Callable[[str], bool],
        is_running: Callable[[str], bool],
        now_ms: Callable[[], int],
        binding_for: Callable[[str], BrokerBotBinding | None],
        installed_custody_account_id: Callable[[], str | None],
    ) -> None:
        del artifacts_root
        self._lifecycle_repo_for = lifecycle_repo_for
        self._lifecycle_projector = lifecycle_projector
        self._lifecycle_projector_for = lifecycle_projector_for or (
            lambda _strategy_instance_id: lifecycle_projector
        )
        self._desired_repo_for = desired_repo_for
        self._recovery_candidates = recovery_candidates
        self._stop_authority_run = stop_authority_run
        self._manages_instance = manages_instance
        self._is_running = is_running
        self._now_ms = now_ms
        # The binding plane's reader and the installed authority's custody id:
        # the two facts the foreign-binding classification compares. Required,
        # both of them — this decides whether a real-money boot repairs a
        # binding the installed authority does not custody, and a construction
        # site that could omit it would get the pre-slice-7 behaviour with no
        # signal (ADR 0059 slice 7, R15).
        self._binding_for = binding_for
        self._installed_custody_account_id = installed_custody_account_id

    async def run(
        self,
        *,
        recover: Callable[[], Awaitable[None]] | None = None,
        reconcile: Callable[[], Awaitable[object]] | None = None,
        unresolved_intents_probe: Callable[[str | None], Awaitable[int]] | None = None,
        provenance: RecoverySweepProvenance = BOOT_SWEEP_PROVENANCE,
    ) -> BootRecoveryReport:
        """Recover SQLite authority first, then repair derived file projections."""
        for step_name, step in (("recover", recover), ("reconcile", reconcile)):
            if step is None:
                continue
            try:
                await step()
            except Exception as exc:
                logger.exception(
                    "Boot recovery step failed",
                    extra={"action": "boot_recovery_step_failed", "step": step_name},
                )
                raise BootAuthorityPreparationError(
                    f"SQLite boot authority step {step_name!r} failed"
                ) from exc
        interrupted, authority_unavailable, foreign = await self._repair_lifecycle_artifacts(
            provenance
        )
        # Account-wide on purpose: this is a boot summary of the whole
        # authority, not an admission decision about one bot (#1793).
        unresolved = (
            await unresolved_intents_probe(None) if unresolved_intents_probe is not None else 0
        )
        report = BootRecoveryReport(
            interrupted_instances=tuple(interrupted),
            unresolved_intents=unresolved,
            completed_at_ms=self._now_ms(),
            authority_unavailable_instances=tuple(authority_unavailable),
            foreign_instances=tuple(foreign),
        )
        logger.info(
            "Boot recovery sweep complete",
            extra={
                "action": "boot_recovery_complete",
                "interrupted": list(report.interrupted_instances),
                "unresolved_intents": report.unresolved_intents,
                "authority_unavailable": list(report.authority_unavailable_instances),
                "foreign": list(report.foreign_instances),
            },
        )
        return report

    async def _repair_lifecycle_artifacts(
        self, provenance: RecoverySweepProvenance
    ) -> tuple[list[str], list[str], list[str]]:
        """Repair every managed candidate.

        Returns the bots that received interrupted evidence; the bots left
        unprojected because no lifecycle authority was installed; and the
        bots left as their own files say because the installed authority
        does not custody the account they are sealed on.
        """
        interrupted: list[str] = []
        authority_unavailable: list[str] = []
        foreign: list[str] = []
        try:
            candidates = sorted(
                set(self._recovery_candidates()),
                key=lambda candidate: (
                    candidate.strategy_instance_id,
                    candidate.run_id,
                ),
            )
        except Exception as exc:
            raise BootAuthorityPreparationError(
                "SQLite boot recovery candidate enumeration failed"
            ) from exc
        for candidate in candidates:
            if not self._manages_instance(candidate.strategy_instance_id):
                continue
            foreign_binding = self._foreign_binding(candidate.strategy_instance_id)
            if foreign_binding is not None:
                # Same posture as ``authority_unavailable`` below and for the
                # same ADR 0050 reason: this authority has never seen the
                # instance, so any duty state written from here would be
                # caller-authored. The binding's own lifecycle files stand.
                logger.warning(
                    "boot sweep leaves a foreign binding as its files say",
                    extra={
                        "action": "boot_recovery_foreign_binding",
                        "strategy_instance_id": foreign_binding.strategy_instance_id,
                        "sealed_account_id": foreign_binding.sealed_account_id,
                        "installed_account_id": foreign_binding.installed_account_id,
                    },
                )
                foreign.append(candidate.strategy_instance_id)
                continue
            try:
                recorded_interruption = await self._repair_candidate(candidate, provenance)
            except AlpacaLifecycleAuthorityUnavailableError as exc:
                # No Clerk is installed (invalid ALPACA_* settings, or the
                # authority selection failed), so SQLite cannot be asked what
                # this run's duty state is. Writing OFF_DUTY from here would be
                # caller-authored duty state -- exactly what the projector
                # refuses -- so the durable evidence stays as the dead process
                # left it, and the registry keeps the start gate closed. This
                # must not abort the lifespan: a Clerk-less boot with stale
                # bindings used to crash-loop the container (2026-09-09).
                logger.warning(
                    "Boot sweep left a bot unprojected: no lifecycle authority",
                    extra={
                        "action": "boot_sweep_authority_unavailable",
                        "reason_code": "LIFECYCLE_AUTHORITY_UNAVAILABLE",
                        "strategy_instance_id": candidate.strategy_instance_id,
                        "run_id": candidate.run_id,
                        "error": str(exc),
                    },
                )
                authority_unavailable.append(candidate.strategy_instance_id)
                continue
            if recorded_interruption:
                interrupted.append(candidate.strategy_instance_id)
        return interrupted, authority_unavailable, foreign

    def _foreign_binding(self, strategy_instance_id: str) -> _ForeignBinding | None:
        """Why the installed primary authority does not custody this binding, if so.

        Foreignness is the boot-side half of Start's ``SEALED_ACCOUNT_MISMATCH``
        (``run_admission``) and is decided by the same comparison: the
        immutable binding names a custody account this Clerk does not hold.
        After a real-money account graduates, every instance that rehearsed
        under the Shadow Account Authority is sealed on
        ``shadow:<live_account_id>`` while the primary custodies
        ``<live_account_id>`` -- foreign to it, and refused rather than
        repaired (ADR 0059 slice 7, R15).

        A Dry Run binding is never foreign, however its ``sim:`` custody id
        compares: the registry routes it to its own per-instance authority,
        so the projector that would repair it is not the primary one. That
        routing is *read* here through ``lifecycle_projector_for``, never
        re-derived, so this predicate cannot drift from the selector that
        owns it -- and a ``sim:`` binding is excluded because of where it
        routes, not because of how its id is spelled.

        Corruption is not foreignness: an unreadable binding, a non-Alpaca
        identity, and a historical IBKR binding keep raising exactly as they
        do today.

        No installed custody id means "not foreign": with no authority
        installed there is nothing for the binding to be foreign *to*, and
        that boot is already the ``authority_unavailable`` branch's to
        report -- it closes the start gate on its own.
        """
        binding = self._binding_for(strategy_instance_id)
        if binding is None or binding.sealed_account_id is None:
            return None
        # The custody id the installed primary authority holds -- the same
        # ``account_id`` a ``ClerkCustodySnapshot`` carries into Start
        # admission. Every authority declares it (``ActiveAlpacaClerk``), so
        # the only absence is "no authority installed".
        installed_account_id = self._installed_custody_account_id()
        if installed_account_id is None or binding.sealed_account_id == installed_account_id:
            return None
        if self._lifecycle_projector_for(strategy_instance_id) is not self._lifecycle_projector:
            return None
        return _ForeignBinding(
            strategy_instance_id=strategy_instance_id,
            sealed_account_id=binding.sealed_account_id,
            installed_account_id=installed_account_id,
        )

    async def _repair_candidate(
        self,
        candidate: BotRecoveryCandidate,
        provenance: RecoverySweepProvenance,
    ) -> bool:
        """Repair one candidate; True when it received interrupted evidence.

        Every branch consults the projector before it writes anything; the
        unavailable-authority handler in the caller relies on that ordering
        to leave durable evidence untouched.
        """
        strategy_instance_id = candidate.strategy_instance_id
        run_id = candidate.run_id
        projector = self._lifecycle_projector_for(strategy_instance_id)
        if self._is_running(strategy_instance_id):
            projection = projector.refresh(
                strategy_instance_id=strategy_instance_id,
                now_ms=self._now_ms(),
                updated_by=provenance.updated_by,
                reason="refresh_running_projection",
            )
            self._require_settled_projection(
                projection,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
            )
            return False
        if candidate.sqlite_active:
            try:
                await self._stop_authority_run(strategy_instance_id, run_id)
            except Exception as exc:
                raise BootAuthorityPreparationError(
                    f"SQLite boot stop failed for {strategy_instance_id!r}"
                ) from exc
        repo = self._lifecycle_repo_for(strategy_instance_id)
        try:
            record = repo.read()
        except BotLifecycleStateCorruptError as exc:
            logger.warning(
                "Boot sweep skipping corrupt lifecycle state",
                extra={"action": "boot_sweep_corrupt_lifecycle", "path": str(exc.path)},
            )
            return False

        desired_repo = self._desired_repo_for(strategy_instance_id)
        desired_state = desired_repo.read_state()
        if (
            record is not None
            and record.phase is BotLifecyclePhase.OFF_DUTY
            and record.duty_outcome is not None
            and desired_state in {DesiredState.RUNNING, DesiredState.PAUSED}
        ):
            projection = projector.refresh(
                strategy_instance_id=strategy_instance_id,
                now_ms=self._now_ms(),
                updated_by=provenance.updated_by,
                reason="repair_terminal_projection",
            )
            if not self._require_settled_projection(
                projection,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
            ):
                return False
            desired_repo.set(
                DesiredState.STOPPED,
                updated_by=provenance.updated_by,
                now_ms=self._now_ms(),
                reason="repair_terminal_nonstopped_intent",
            )
            logger.warning(
                "Boot sweep repaired terminal bot desired state",
                extra={
                    "action": "boot_sweep_repaired_terminal_intent",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": record.duty_outcome.run_id,
                    "reason_code": record.duty_outcome.reason_code,
                },
            )
            return False
        # A run that already carries its own terminal outcome (the bot
        # finalized file-side; only the SQLite STOP was still owed, and it
        # was committed above) keeps that outcome: overwriting a durable
        # CRASHED record with generic interrupted evidence would replace
        # the more specific receipt with the less specific one (ADR 0050).
        already_terminal_for_run = (
            record is not None
            and record.duty_outcome is not None
            and record.duty_outcome.run_id == run_id
        )
        run_looks_interrupted = candidate.sqlite_active or (
            record is not None and record.phase is BotLifecyclePhase.ON_DUTY
        ) or (
            desired_state in {DesiredState.RUNNING, DesiredState.PAUSED}
            and (
                record is None
                or (
                    record.phase is BotLifecyclePhase.OFF_DUTY
                    and record.duty_outcome is None
                )
            )
        )
        needs_interrupted_evidence = run_looks_interrupted and not already_terminal_for_run
        if not needs_interrupted_evidence:
            projection = projector.refresh(
                strategy_instance_id=strategy_instance_id,
                now_ms=self._now_ms(),
                updated_by=provenance.updated_by,
                reason="refresh_boot_projection",
            )
            self._require_settled_projection(
                projection,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
            )
            return False

        now_ms = self._now_ms()
        outcome = BotDutyOutcome(
            kind="EXITED_UNVERIFIED",
            reason_code=provenance.interrupted_reason_code,
            recorded_at_ms=now_ms,
            run_id=run_id,
        )
        update_result = projector.project_terminal(
            strategy_instance_id=strategy_instance_id,
            outcome=outcome,
            now_ms=now_ms,
            updated_by=provenance.updated_by,
            reason=provenance.interrupted_reason,
        )
        if not self._require_settled_projection(
            update_result,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
        ):
            return False
        desired_repo.set(
            DesiredState.STOPPED,
            updated_by=provenance.updated_by,
            now_ms=now_ms,
            reason=provenance.desired_state_reason,
        )
        logger.warning(
            "Boot sweep recorded interrupted bot",
            extra={
                "action": "boot_sweep_interrupted",
                "strategy_instance_id": strategy_instance_id,
                "run_id": run_id,
            },
        )
        return True

    @staticmethod
    def _require_settled_projection(
        result: AlpacaLifecycleProjectionResult,
        *,
        strategy_instance_id: str,
        run_id: str,
    ) -> bool:
        if result.status is ProjectionStatus.RECORDED:
            return True
        if result.status is ProjectionStatus.AUTHORITY_EXPECTATION_SUPERSEDED:
            logger.info(
                "Boot sweep skipped authority-superseded lifecycle expectation",
                extra={
                    "action": "boot_sweep_superseded",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": run_id,
                },
            )
            return False
        detail = result.status.value
        if result.refusal_reason is not None:
            detail = f"{detail}:{result.refusal_reason}"
        raise BootAuthorityPreparationError(
            f"Lifecycle projection repair remained unresolved for "
            f"{strategy_instance_id!r}: {detail}"
        )

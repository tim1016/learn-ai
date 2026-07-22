"""Account cockpit composition over the Clerk directory and daemon facts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.engine.live.daemon_transport import DaemonResult
from app.schemas.account_cockpit import AccountCockpitDaemon, AccountCockpitResponse
from app.schemas.live_runs import HostRunnerHealth
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    ConfirmInFormAction,
    NavigateAction,
    OperatorBlocker,
    OperatorConfirmationCopy,
    OperatorMove,
)
from app.services.account_directory import AccountDirectoryService
from app.utils.timestamps import now_ms_utc

DaemonHealthFetcher = Callable[[], Awaitable[tuple[DaemonResult, HostRunnerHealth | None]]]


class AccountCockpitService:
    """Author the limited degraded-mode choices for one account cockpit.

    The daemon result controls only process guidance.  It never changes the
    Clerk's account facts or offers the data plane a host-process restart.
    """

    def __init__(
        self,
        *,
        directory: AccountDirectoryService,
        fetch_daemon_health: DaemonHealthFetcher,
        now_ms: Callable[[], int] = now_ms_utc,
    ) -> None:
        self._directory = directory
        self._fetch_daemon_health = fetch_daemon_health
        self._now_ms = now_ms

    async def surface(self, *, account_id: str) -> AccountCockpitResponse:
        """Compose read-only account and host facts into one explicit mode."""

        clerk = self._directory.service_status(account_id=account_id)
        daemon_result, daemon_health = await self._fetch_daemon_health()
        generated_at_ms = self._now_ms()
        daemon = _daemon_surface(daemon_result, daemon_health, generated_at_ms)

        if daemon.availability == "DOWN":
            return AccountCockpitResponse(
                account_id=clerk.account_id,
                generated_at_ms=generated_at_ms,
                mode="DAEMON_DOWN",
                clerk=clerk,
                daemon=daemon,
                blockers=[_daemon_down_blocker(daemon.reason_code, daemon.detail)],
            )
        if daemon.availability == "UNREADABLE":
            return AccountCockpitResponse(
                account_id=clerk.account_id,
                generated_at_ms=generated_at_ms,
                mode="DAEMON_UNREADABLE",
                clerk=clerk,
                daemon=daemon,
                blockers=[_daemon_down_blocker(daemon.reason_code, daemon.detail)],
            )
        if clerk.journal.integrity == "corrupt":
            return AccountCockpitResponse(
                account_id=clerk.account_id,
                generated_at_ms=generated_at_ms,
                mode="JOURNAL_CORRUPT",
                clerk=clerk,
                daemon=daemon,
                blockers=[_journal_corrupt_blocker(clerk.journal.corruption_detail, clerk.journal.recovery_phase)],
            )
        if clerk.journal.integrity == "broker_evidence_only":
            return AccountCockpitResponse(
                account_id=clerk.account_id,
                generated_at_ms=generated_at_ms,
                mode="JOURNAL_EVIDENCE_HOLD",
                clerk=clerk,
                daemon=daemon,
                blockers=[_broker_evidence_hold_blocker(clerk.journal.corruption_detail)],
            )
        if clerk.attachment != "ATTACHED":
            return AccountCockpitResponse(
                account_id=clerk.account_id,
                generated_at_ms=generated_at_ms,
                mode="CLERK_DOWN",
                clerk=clerk,
                daemon=daemon,
                blockers=[_restore_clerk_blocker()],
            )
        return AccountCockpitResponse(
            account_id=clerk.account_id,
            generated_at_ms=generated_at_ms,
            mode="NORMAL",
            clerk=clerk,
            daemon=daemon,
        )


def _daemon_surface(
    result: DaemonResult,
    health: HostRunnerHealth | None,
    observed_at_ms: int,
) -> AccountCockpitDaemon:
    if health is not None:
        return AccountCockpitDaemon(
            availability="AVAILABLE",
            reason_code="DAEMON_CONNECTED",
            detail="The host daemon is reachable and reporting its current process facts.",
            observed_at_ms=observed_at_ms,
        )
    if result.kind in {"UNREACHABLE", "RETRYING"}:
        return AccountCockpitDaemon(
            availability="DOWN",
            reason_code="DAEMON_UNREACHABLE",
            detail=(
                result.detail
                or "The host daemon did not answer. Restore it on the host; the data plane cannot restart host processes."
            ),
            observed_at_ms=observed_at_ms,
        )
    return AccountCockpitDaemon(
        availability="UNREADABLE",
        reason_code=f"DAEMON_{result.kind}",
        detail=(
            result.detail
            or "The host daemon answered without usable health facts. Inspect host-side daemon diagnostics."
        ),
        observed_at_ms=observed_at_ms,
    )


def _restore_clerk_blocker() -> OperatorBlocker:
    return OperatorBlocker.for_host(
        condition_id="ACCOUNT_CLERK_UNAVAILABLE",
        scope="account",
        host="account_desk",
        anchor=SURFACE_ANCHOR,
        audience="both",
        disposition="fix_here",
        headline="Account Clerk is unavailable",
        detail="Restore the Clerk through the host daemon. No bypass broker writer is available.",
        applies_to="both",
        primary_move=OperatorMove(
            label="Restore Clerk",
            action=ConfirmInFormAction(kind="confirm_in_form", anchor="account-clerk-restore-action"),
            confirmation=OperatorConfirmationCopy(
                title="Restore Account Clerk",
                body="Ask the host daemon to restore the sole Account Clerk for this account.",
                consequence="The daemon records a new Clerk generation if it must replace the process. The cockpit will re-observe account evidence after the restore.",
                confirm_label="Restore Clerk",
                required_token="RESTORE",
            ),
        ),
    )


def _journal_corrupt_blocker(
    detail: str | None,
    recovery_phase: str | None,
) -> OperatorBlocker:
    """Expose the sole deliberate operator ceremony for corrupt Clerk evidence."""

    rebaseline = recovery_phase in {"REBASELINE_REQUIRED", "REBASELINE_PENDING"}
    return OperatorBlocker.for_host(
        condition_id="ACCOUNT_CLERK_JOURNAL_CORRUPT",
        scope="account",
        host="account_desk",
        anchor=SURFACE_ANCHOR,
        audience="operator",
        disposition="fix_here",
        headline="Account Clerk journal is corrupt — broker writes are blocked",
        detail=(
            "The corrupt Clerk durability evidence must be quarantined and retained before a fresh broker "
            "baseline can be seeded. No terminal procedure or broker-writer bypass is available. "
            f"Integrity evidence: {detail or 'journal replay failed.'}"
        ),
        applies_to="both",
        primary_move=OperatorMove(
            label="Re-baseline from fresh broker snapshot" if rebaseline else "Begin journal recovery ceremony",
            action=ConfirmInFormAction(kind="confirm_in_form", anchor="account-journal-recovery-action"),
            confirmation=OperatorConfirmationCopy(
                title="Re-baseline Clerk journal" if rebaseline else "Quarantine corrupt Clerk journal",
                body=("Take a fresh full broker snapshot and seed a new Clerk journal from broker-evidence-only exposure." if rebaseline else "Rename the corrupt Clerk journal and its paired crash-boundary inbox aside permanently as audit evidence."),
                consequence=("Every discovered holding remains flag-and-hold broker evidence; no bot owner is guessed." if rebaseline else "All account broker writes remain blocked. The next step is a fresh broker snapshot and a new broker-evidence-only baseline."),
                confirm_label="Re-baseline journal" if rebaseline else "Quarantine journal",
                required_token="REBASELINE" if rebaseline else "QUARANTINE",
            ),
        ),
    )


def _broker_evidence_hold_blocker(detail: str | None) -> OperatorBlocker:
    """Keep newly discovered exposure visibly held without replacing another freeze."""

    return OperatorBlocker.for_host(
        condition_id="CLERK_BROKER_EVIDENCE_ONLY_HOLD",
        scope="account",
        host="account_desk",
        anchor=SURFACE_ANCHOR,
        audience="operator",
        disposition="wait",
        headline="Broker-evidence-only exposure is held",
        detail=(
            "The replacement journal records fresh broker facts without assigning a bot owner. "
            "Account broker writes remain blocked until the Account Clerk reconciles that evidence. "
            f"Evidence: {detail or 'fresh broker evidence remains unowned.'}"
        ),
        applies_to="both",
    )


def _daemon_down_blocker(reason_code: str, detail: str) -> OperatorBlocker:
    return OperatorBlocker.for_host(
        condition_id=reason_code,
        scope="host",
        host="account_desk",
        anchor=SURFACE_ANCHOR,
        audience="both",
        disposition="fix_elsewhere",
        headline="Host daemon needs host-side recovery",
        detail=(
            f"{detail} The data plane cannot restart a host process; use the host recovery guidance instead."
        ),
        applies_to="both",
        primary_move=OperatorMove(
            label="Open host recovery guidance",
            action=NavigateAction(kind="navigate", route="/broker/session-mirror"),
        ),
    )

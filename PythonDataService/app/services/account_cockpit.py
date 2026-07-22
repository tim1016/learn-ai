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

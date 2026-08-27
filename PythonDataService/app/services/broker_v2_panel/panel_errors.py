"""The panel's typed error vocabulary, and the HTTP status each one carries.

Declarations only — no behaviour, no dependencies inside this package. Every
panel service raises these; the router is the single place that translates
them to responses, reading ``http_status`` rather than re-deciding it.
"""

from __future__ import annotations

from app.schemas.run_admission import RunAdmissionDecision


class PanelDataError(Exception):
    """Base typed panel-data error; the router translates to HTTP."""

    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.next_action = next_action


class PanelUnavailableError(PanelDataError):
    """A required backend (clerk / bot runner) is not configured (503)."""

    http_status = 503


class AccountMismatchError(PanelDataError):
    """The path ``account_id`` does not match the broker's account (404)."""

    http_status = 404


class UnknownBotError(PanelDataError):
    """No bot with this sid is bound to the broker (404)."""

    http_status = 404


class PanelRunnerError(PanelDataError):
    """The bot runner rejected a panel operation with a typed status."""

    def __init__(
        self,
        message: str,
        *,
        detail: str | None,
        http_status: int,
        next_action: str | None = None,
        operation_attempted: bool = False,
        admission_decision: RunAdmissionDecision | None = None,
    ) -> None:
        super().__init__(message, detail=detail, next_action=next_action)
        self.http_status = http_status
        self.operation_attempted = operation_attempted
        self.admission_decision = admission_decision

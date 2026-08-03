"""Typed errors emitted by the in-process bot runner boundary."""

from __future__ import annotations

from typing import Literal, NoReturn

from app.schemas.run_admission import RunAdmissionDecision


class BotRunnerError(Exception):
    """Base typed bot-runner error; routers translate its HTTP status."""

    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        admission_decision: RunAdmissionDecision | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.admission_decision = admission_decision


class UnknownBotError(BotRunnerError):
    http_status = 404


class InvalidStrategyInstanceIdError(BotRunnerError):
    http_status = 422


class InvalidRunHistoryCursorError(BotRunnerError):
    http_status = 422


class BotAlreadyRunningError(BotRunnerError):
    http_status = 409


class MarketDataFeedUnavailableError(BotRunnerError):
    http_status = 503


class RestartIntensityRefusedError(BotRunnerError):
    http_status = 429


class BootRecoveryIncompleteError(BotRunnerError):
    http_status = 503


class RecoveryUncertainError(BotRunnerError):
    http_status = 409


class CarryoverPolicyRefusedError(BotRunnerError):
    http_status = 409


class RunAdmissionRefusedError(BotRunnerError):
    http_status = 409


def raise_run_refusal(decision: RunAdmissionDecision) -> NoReturn:
    """Translate one denied policy decision into the stable runner taxonomy."""
    if decision.reason_code == "RUN_ALREADY_ACTIVE":
        raise BotAlreadyRunningError(
            "The strategy instance already has an active run.",
            detail=decision.explanation,
            admission_decision=decision,
        )
    if decision.reason_code in {
        "MARKET_DATA_STALE",
        "MARKET_DATA_UNAVAILABLE",
        "MARKET_DATA_UNKNOWN",
    }:
        raise MarketDataFeedUnavailableError(
            f"Market data is not ready; bot cannot {decision.operation.title()}.",
            detail=decision.explanation,
            admission_decision=decision,
        )
    error_types = {
        "BOOT_RECOVERY_INCOMPLETE": BootRecoveryIncompleteError,
        "RECOVERY_UNCERTAIN": RecoveryUncertainError,
        "RESTART_INTENSITY_EXCEEDED": RestartIntensityRefusedError,
        "RESUME_CARRYOVER_UNSUPPORTED": RecoveryUncertainError,
        "RESUME_CHECKPOINT_MISSING": RecoveryUncertainError,
        "RESUME_CHECKPOINT_MISMATCH": RecoveryUncertainError,
    }
    error_type = error_types.get(decision.reason_code)
    if error_type is not None:
        raise error_type(
            decision.explanation,
            detail=f"Bot runtime safety refused {decision.operation.title()}.",
            admission_decision=decision,
        )
    if decision.reason_code == "RESUME_CARRYOVER_NOT_ALLOWED":
        raise CarryoverPolicyRefusedError(
            "Exposure carryover is not approved for this Resume.",
            detail=decision.explanation,
            admission_decision=decision,
        )
    raise RunAdmissionRefusedError(
        f"{decision.operation.title()} admission was refused.",
        detail=decision.explanation,
        admission_decision=decision,
    )


def require_start_configuration(
    carryover_policy: Literal["FORBID", "ALLOW"],
    *,
    carryover_allowed: bool,
) -> None:
    """Apply request-configuration rules shared by preview and execution."""
    if carryover_policy == "ALLOW" and not carryover_allowed:
        raise CarryoverPolicyRefusedError(
            "Exposure carryover is disabled for this Alpaca paper account.",
            detail=(
                "Both the account policy and this deployment must opt in; the account policy is currently disabled."
            ),
        )

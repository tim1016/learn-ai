"""Typed errors emitted by the in-process bot runner boundary."""

from __future__ import annotations

from typing import Literal, NoReturn

from app.schemas.action_plan import ActionPlan, OptionEntryLeg, traded_instrument
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.traded_asset_acknowledgement import (
    UntestedTradedAssetAcknowledgement,
    traded_asset_acknowledgement_fault,
)


class BotRunnerError(Exception):
    """Base typed bot-runner error; routers translate its HTTP status."""

    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        admission_decision: RunAdmissionDecision | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.admission_decision = admission_decision
        #: A stable code for a refusal that has no admission decision to
        #: carry one (the lane-level start gates); ``None`` otherwise.
        self.reason_code = reason_code


class UnknownBotError(BotRunnerError):
    http_status = 404


class InvalidStrategyInstanceIdError(BotRunnerError):
    http_status = 422


class BotAlreadyRunningError(BotRunnerError):
    http_status = 409


class MarketDataFeedUnavailableError(BotRunnerError):
    http_status = 503


class BootRecoveryIncompleteError(BotRunnerError):
    http_status = 503


class RecoverySweepEvaluatingError(BotRunnerError):
    """The reconciliation sweep is still evaluating recovery (#1808) — wait."""

    http_status = 503


class RecoveryUncertainError(BotRunnerError):
    http_status = 409


class CarryoverPolicyRefusedError(BotRunnerError):
    http_status = 409


class TradedAssetAcknowledgementRefusedError(BotRunnerError):
    """A bot may not start with this acknowledgement of an untested traded asset, or without one (#2912)."""

    http_status = 409


class OptionStartRefusedError(BotRunnerError):
    """A bot that trades option contracts may not start as it was asked to (ADR 0076 decision 24)."""

    http_status = 409


class RunAdmissionRefusedError(BotRunnerError):
    http_status = 409


#: The lane is held for go-live after an installation migration (#2269).
LANE_GO_LIVE_PENDING = "LANE_GO_LIVE_PENDING"
#: The lane cannot read its go-live hold, so it holds (fail closed).
LANE_GO_LIVE_HOLD_UNREADABLE = "LANE_GO_LIVE_HOLD_UNREADABLE"


class ActivationFailedCleanupProvenError(BotRunnerError):
    """Activation failed after Clerk registration, but the Clerk stop committed.

    A known, resolved failure — the attempted run is durably stopped, not
    lost in an unknown state. Distinct from an unproven cleanup, which keeps
    today's untyped propagation so the action layer reports ``unknown``.
    """

    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        attempted_run_id: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.attempted_run_id = attempted_run_id


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
        "RECOVERY_SWEEP_EVALUATING": RecoverySweepEvaluatingError,
        "RECOVERY_UNCERTAIN": RecoveryUncertainError,
    }
    error_type = error_types.get(decision.reason_code)
    if error_type is not None:
        raise error_type(
            decision.explanation,
            detail=f"Bot runtime safety refused {decision.operation.title()}.",
            admission_decision=decision,
        )
    raise RunAdmissionRefusedError(
        f"{decision.operation.title()} admission was refused.",
        detail=decision.explanation,
        admission_decision=decision,
    )


TRADED_ASSET_ACKNOWLEDGEMENT_REQUIRED = "TRADED_ASSET_ACKNOWLEDGEMENT_REQUIRED"
TRADED_ASSET_ACKNOWLEDGEMENT_NOT_APPLICABLE = "TRADED_ASSET_ACKNOWLEDGEMENT_NOT_APPLICABLE"


def require_traded_asset_acknowledgement(
    *,
    mode: Literal["log_only", "dry_run", "trade"],
    action_plan: ActionPlan,
    signal_symbol: str,
    acknowledgement: UntestedTradedAssetAcknowledgement | None,
) -> None:
    """Refuse to start a bot the acknowledgement rule does not allow (#2912).

    The runner is where every bot starts, so the rule is held here whatever
    route asked. Deploy asks the same rule first, to refuse before the bot
    has a name.
    """
    fault = traded_asset_acknowledgement_fault(
        mode=mode, action_plan=action_plan, signal_symbol=signal_symbol, acknowledgement=acknowledgement,
    )
    if fault is None:
        return
    instrument = traded_instrument(action_plan)
    traded = instrument.underlying if instrument.kind == "stock" else f"options on {instrument.underlying}"
    if fault == "required":
        raise TradedAssetAcknowledgementRefusedError(
            f"Accept that this strategy is untested trading {traded} before deploying.",
            detail=(
                f"The strategy's evidence is about trading {signal_symbol}, the symbol it listens to. "
                "A Paper, Live Shadow or Live bot that trades something else starts only with the "
                "owner's written acknowledgement."
            ),
            reason_code=TRADED_ASSET_ACKNOWLEDGEMENT_REQUIRED,
        )
    raise TradedAssetAcknowledgementRefusedError(
        "This bot has no untested traded asset to accept.",
        detail=(
            "The acknowledgement is for a Paper, Live Shadow or Live bot that trades something other "
            "than the shares it listens to. It is kept with the bot for good, so no other bot may carry one."
        ),
        reason_code=TRADED_ASSET_ACKNOWLEDGEMENT_NOT_APPLICABLE,
    )


def require_start_configuration(
    carryover_policy: Literal["FORBID", "ALLOW"],
) -> None:
    """Apply request-configuration rules shared by preview and execution."""
    if carryover_policy == "ALLOW":
        raise CarryoverPolicyRefusedError(
            "Exposure carryover is globally disabled for Alpaca bots.",
            detail=(
                "Deploy creates a fresh identity. Resolve the previous deployment’s exposure "
                "through Flatten; a new deployment never adopts it."
            ),
        )


OPTION_BOT_REGULAR_HOURS_ONLY = "OPTION_BOT_REGULAR_HOURS_ONLY"
OPTION_ORDER_TERMS_REQUIRED = "OPTION_ORDER_TERMS_REQUIRED"
OPTION_ORDER_TERMS_NOT_APPLICABLE = "OPTION_ORDER_TERMS_NOT_APPLICABLE"


def require_option_start_configuration(
    *,
    action_plan: ActionPlan,
    use_rth: bool,
    option_order_terms: SealedOptionOrderTerms | None,
) -> None:
    """Refuse to start a bot whose option settings and plan do not go together (ADR 0076 decision 24).

    A bot that trades option contracts runs in regular hours only, and
    starts with its four order terms: they are sealed with it and fixed for
    its life, so nothing later could supply them. A bot in shares carries
    none. The runner is where every bot starts, so the rule is held here
    whatever route asked.
    """
    trades_options = any(isinstance(leg, OptionEntryLeg) for leg in action_plan.on_enter)
    if not trades_options:
        if option_order_terms is None:
            return
        raise OptionStartRefusedError(
            "This bot trades shares, so it has no option order terms.",
            detail="Option order terms are sealed only with a bot that trades option contracts.",
            reason_code=OPTION_ORDER_TERMS_NOT_APPLICABLE,
        )
    if not use_rth:
        raise OptionStartRefusedError(
            "Options trade in regular hours only.",
            detail="A bot that trades option contracts cannot be started for extended hours.",
            reason_code=OPTION_BOT_REGULAR_HOURS_ONLY,
        )
    if option_order_terms is None:
        raise OptionStartRefusedError(
            "This option bot has no order terms.",
            detail=(
                "A bot that trades option contracts starts with its four order terms: the widest gap a leg, "
                "the entry wait, the whole-structure cost limit and the expiry-day close. They are sealed with "
                "the bot and fixed for its life."
            ),
            reason_code=OPTION_ORDER_TERMS_REQUIRED,
        )

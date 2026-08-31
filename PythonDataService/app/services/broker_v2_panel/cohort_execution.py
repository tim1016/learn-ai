"""The batch contract every cohort-scoped affordance executes under (ADR 0051).

Extracted when ``archive`` became the second cohort action (ADR 0052). The
loop below is the part that has nothing to do with *which* action a leg
carries: explicit membership, sequential execution, a derived per-leg
idempotency identity, one typed answer per leg, and a single account-scoped
early exit. Duplicating it per action would give the batch-outcome contract
two homes that can drift, and it is precisely the contract an operator
depends on when a leg fails halfway through a fleet-wide command.

What stays with each caller is what genuinely differs: how its cohorts are
presented, and which action id its legs carry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.schemas.broker_v2_panel import (
    CohortLegResult,
    PanelActionErrorResponse,
    PanelActionRequest,
)
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    ActionExecutionError,
    ActionOutcomeUnknownError,
    AuthorityPoisonedError,
    ExecutionAuthorityLostError,
    StaleRevisionError,
)
from app.services.broker_v2_panel.panel_errors import PanelDataError
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CohortLegCommand:
    """One leg to execute, echoing the facts its presentation carried."""

    strategy_instance_id: str
    action_id: str
    revision: int
    concurrency_token: str


def _leg_error(
    leg: CohortLegCommand,
    error: Exception,
    *,
    outcome: str,
    receipt_id: str | None,
    reason_code: str | None,
) -> CohortLegResult:
    detail = getattr(error, "detail", None)
    return CohortLegResult(
        strategy_instance_id=leg.strategy_instance_id,
        outcome=outcome,
        result=None,
        error=PanelActionErrorResponse(
            action_id=leg.action_id,
            outcome=(
                "unknown"
                if outcome == "unknown"
                else ("conflict" if outcome == "refused" else "failure")
            ),
            receipt_id=receipt_id,
            recorded_at_ms=now_ms_utc(),
            message=str(error),
            why=detail,
            reason_code=reason_code,
        ),
    )


async def execute_cohort_legs(
    broker: str,
    account_id: str,
    *,
    legs: Sequence[CohortLegCommand],
    idempotency_key: str,
    reason: str | None,
    operator_identity: str,
    telemetry_kind: str,
) -> list[CohortLegResult]:
    """Execute the named legs in order; a leg's own trouble never aborts its siblings.

    ADR 0051: sequential on purpose (the account authority is a single
    writer, so parallelism buys no wall time and would make receipt order
    nondeterministic), explicit membership (exactly the named legs run), and
    derived per-leg idempotency identity (``{idempotency_key}:{sid}``) so a
    re-POST replays applied legs as no-ops and retries only released ones.
    The one early exit is account-scoped authority loss -- a fact about the
    account, not any leg -- where the batch ends with the attempted legs'
    outcomes and the rest absent (safe to re-POST under the same key).

    ``account_id`` must already be resolved by the caller: every cohort
    surface validates scope before it presents, and re-validating per batch
    would let a leg run against a different account than the one presented.
    """
    results: list[CohortLegResult] = []
    for leg in legs:
        leg_request = PanelActionRequest(
            action_id=leg.action_id,
            revision=leg.revision,
            concurrency_token=leg.concurrency_token,
            idempotency_key=f"{idempotency_key}:{leg.strategy_instance_id}",
            reason=reason,
        )
        try:
            result = await panel_data_source.run_action(
                broker,
                account_id,
                leg.strategy_instance_id,
                leg_request,
                operator_identity=operator_identity,
            )
        except ActionOutcomeUnknownError as error:
            results.append(
                _leg_error(
                    leg,
                    error,
                    outcome="unknown",
                    receipt_id=leg_request.idempotency_key,
                    reason_code=error.reason_code,
                )
            )
        except (ExecutionAuthorityLostError, AuthorityPoisonedError) as error:
            # Account-scoped authority loss: no later leg can succeed. Record
            # this leg's typed failure and end the batch early -- unattempted
            # legs are absent from the response and safe to re-POST under the
            # same cohort key once the account-scoped cure lands (ADR 0051).
            results.append(
                _leg_error(
                    leg,
                    error,
                    outcome="failed",
                    receipt_id=None,
                    reason_code=error.reason_code,
                )
            )
            break
        except ActionExecutionError as error:
            refused = isinstance(error, StaleRevisionError) or error.http_status == 409
            results.append(
                _leg_error(
                    leg,
                    error,
                    outcome="refused" if refused else "failed",
                    receipt_id=None,
                    reason_code=error.reason_code,
                )
            )
        except PanelDataError as error:
            results.append(
                _leg_error(
                    leg,
                    error,
                    outcome="failed",
                    receipt_id=None,
                    reason_code=None,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A performer can raise outside the typed hierarchies after real
            # work was attempted (e.g. a claim error escaping a recovery
            # EXIT). The batch contract still owes this leg a typed answer
            # and its siblings their turns -- translate to the same
            # outcome-unknown shape the per-bot surface reports, keeping the
            # derived leg key so the operator can inspect Clerk evidence for
            # exactly this identity.
            logger.exception(
                "cohort leg failed outside the typed action taxonomy",
                extra={
                    "action": f"cohort_{telemetry_kind}_leg_untyped_failure",
                    "account_id": account_id,
                    "strategy_instance_id": leg.strategy_instance_id,
                    "leg_action_id": leg.action_id,
                },
            )
            results.append(
                _leg_error(
                    leg,
                    error,
                    outcome="unknown",
                    receipt_id=leg_request.idempotency_key,
                    reason_code=None,
                )
            )
        else:
            results.append(
                CohortLegResult(
                    strategy_instance_id=leg.strategy_instance_id,
                    outcome="applied" if result.applied else "replayed",
                    result=result,
                    error=None,
                )
            )
    logger.info(
        "cohort %s executed",
        telemetry_kind,
        extra={
            "action": f"cohort_{telemetry_kind}_executed",
            "account_id": account_id,
            "receipt_id": idempotency_key,
            "leg_count": len(results),
            "applied": sum(1 for leg in results if leg.outcome == "applied"),
            "refused": sum(1 for leg in results if leg.outcome == "refused"),
            "failed": sum(1 for leg in results if leg.outcome in ("failed", "unknown")),
        },
    )
    return results


def count_outcomes(legs: Sequence[CohortLegResult]) -> dict[str, int]:
    """The four counts every cohort result reports, derived one way."""
    return {
        "applied_count": sum(1 for leg in legs if leg.outcome == "applied"),
        "replayed_count": sum(1 for leg in legs if leg.outcome == "replayed"),
        "refused_count": sum(1 for leg in legs if leg.outcome == "refused"),
        "failed_count": sum(1 for leg in legs if leg.outcome in ("failed", "unknown")),
    }

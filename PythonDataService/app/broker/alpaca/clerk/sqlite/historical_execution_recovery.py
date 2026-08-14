"""Paper-only recovery of one exact Alpaca execution missing from old custody.

The normal websocket path records an exact execution before any aggregate
recovery can make its identity ambiguous.  This module is deliberately the
only exception: it recovers a *known missing* exact slice from a bounded,
read-only Alpaca activity lookup, binds it into a short-lived signed plan, and
then delegates the append-only replacement to the existing coverage proof.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.sqlite.execution_coverage import CumulativeRecoveryFill
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import ExecutionCoverageResolutionReceipt
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    recheck_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_execution_coverage_api import (
    ExecutionCoverageResolutionUnavailable,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerAccountSnapshot, BrokerActivity
from app.broker.contract.ports import BrokerReadPort

_LOCAL_PLAN_KEY = b"learn-ai/local-historical-execution-recovery/v1"
_PLAN_TTL_MS = 120_000
_ACTIVITY_LIMIT = 100
_ACTIVITY_LOOKBACK_MS = 86_400_000


class HistoricalExecutionRecoveryRefused(Exception):
    """A typed, fail-closed refusal for historical exact-evidence recovery."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class HistoricalExecutionRecoveryPlan:
    """The complete, signed evidence set an operator may confirm once."""

    account_id: str
    strategy_instance_id: str
    uncertainty_id: str
    order_ref: str
    broker_order_id: str
    execution_id: str
    exact_symbol: str
    exact_quantity: float
    exact_price: float
    exact_side: str
    source_event_at_ms: int
    cumulative_fill_id: str
    cumulative_quantity: float
    cumulative_price: float
    cumulative_side: str
    authority_generation: int
    db_identity_token: str
    control_revision: int
    prepared_at_ms: int
    expires_at_ms: int
    confirmation_token: str


def _plan_key(*, control_secret: str, allow_unauthenticated_control: bool) -> bytes | None:
    configured = control_secret.strip().encode("utf-8")
    if configured:
        return hmac.new(
            configured,
            b"learn-ai/historical-execution-recovery/v1",
            hashlib.sha256,
        ).digest()
    return _LOCAL_PLAN_KEY if allow_unauthenticated_control else None


def _token(key: bytes, payload: dict[str, object]) -> str:
    return hmac.new(
        key,
        canonicalize(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _unsigned_plan_payload(plan: HistoricalExecutionRecoveryPlan) -> dict[str, object]:
    payload = asdict(plan)
    payload.pop("confirmation_token")
    return payload


def _require_plan_key(
    *,
    control_secret: str,
    allow_unauthenticated_control: bool,
) -> bytes:
    key = _plan_key(
        control_secret=control_secret,
        allow_unauthenticated_control=allow_unauthenticated_control,
    )
    if key is None:
        raise HistoricalExecutionRecoveryRefused(
            "RECOVERY_CONTROL_UNAVAILABLE",
            "Historical execution recovery requires configured control signing.",
        )
    return key


def _require_paper_account(*, account_id: str, observed: BrokerAccountSnapshot) -> None:
    if observed.account_id != account_id:
        raise HistoricalExecutionRecoveryRefused(
            "BROKER_ACCOUNT_MISMATCH",
            "The configured Alpaca account no longer matches this SQLite authority.",
        )
    if observed.account_mode != "paper":
        raise HistoricalExecutionRecoveryRefused(
            "LIVE_ACCOUNT_REFUSED",
            "Historical execution recovery is available only for the Alpaca paper account.",
        )


def _require_exact_activity(
    *,
    activities: list[BrokerActivity],
    execution_id: str,
    broker_order_id: str,
    symbol: str,
) -> BrokerActivity:
    matches = [activity for activity in activities if activity.activity_id == execution_id]
    if not matches:
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ACTIVITY_NOT_FOUND",
            "Alpaca did not return the retained exact execution in the bounded recovery window.",
        )
    if len(matches) != 1:
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ACTIVITY_AMBIGUOUS",
            "Alpaca returned more than one activity for the retained execution identity.",
        )
    activity = matches[0]
    if activity.native_order_id != broker_order_id:
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ORDER_MISMATCH",
            "The recovered execution belongs to a different Alpaca order.",
        )
    if activity.symbol is None or activity.symbol.upper() != symbol.upper():
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_SYMBOL_MISMATCH",
            "The recovered execution belongs to a different traded instrument.",
        )
    if activity.activity_type.upper() not in {"FILL", "PARTIAL_FILL"}:
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ACTIVITY_INVALID",
            "The retained Alpaca activity is not an exact fill execution.",
        )
    if activity.side is None or activity.quantity is None or activity.price is None:
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ACTIVITY_MALFORMED",
            "The retained Alpaca activity is missing exact side, quantity, or price evidence.",
        )
    if activity.occurred_at_ms is None:
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ACTIVITY_MALFORMED",
            "The retained Alpaca activity is missing its source timestamp.",
        )
    if (
        activity.side.upper() not in {"BUY", "SELL"}
        or not math.isfinite(activity.quantity)
        or activity.quantity <= 0
        or not math.isfinite(activity.price)
        or activity.price <= 0
    ):
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ACTIVITY_MALFORMED",
            "The retained Alpaca activity has invalid exact execution economics.",
        )
    return activity


def _require_matching_economics(
    *,
    activity: BrokerActivity,
    cumulative: CumulativeRecoveryFill,
) -> None:
    exact = ExecutionSliceFilledFacts(
        execution_id=activity.activity_id,
        symbol=activity.symbol or "unknown",
        side=(activity.side or "").upper(),
        slice_qty=activity.quantity or 0.0,
        slice_price=activity.price or 0.0,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="activity_recovery",
        source_event_at_ms=activity.occurred_at_ms or 0,
    )
    if (
        exact.side != cumulative.side
        or not math.isclose(exact.slice_qty, cumulative.quantity, abs_tol=1e-9, rel_tol=0)
        or not math.isclose(exact.slice_price, cumulative.price, abs_tol=1e-9, rel_tol=0)
    ):
        raise HistoricalExecutionRecoveryRefused(
            "EXECUTION_ECONOMICS_MISMATCH",
            "The exact Alpaca execution does not match the cumulative Clerk recovery total.",
        )


async def prepare_historical_execution_recovery(
    *,
    repo: ClerkSqliteRepository,
    read: BrokerReadPort,
    context: RecoveryPolicyContext,
    concurrency_token: str,
    control_secret: str,
    allow_unauthenticated_control: bool,
) -> HistoricalExecutionRecoveryPlan:
    """Read one bounded exact activity and return a signed no-write plan."""
    capability = recheck_recovery_action(
        context,
        action_id="recover_exact_execution_evidence",
        concurrency_token=concurrency_token,
    )
    if context.strategy_instance_id is None or capability.execution_ref is None:
        raise HistoricalExecutionRecoveryRefused(
            "BOT_SCOPE_REQUIRED",
            "Choose the affected bot before recovering historical execution evidence.",
        )
    key = _require_plan_key(
        control_secret=control_secret,
        allow_unauthenticated_control=allow_unauthenticated_control,
    )
    target = repo.historical_execution_recovery_target(capability.execution_ref)
    if target.strategy_instance_id != context.strategy_instance_id:
        raise HistoricalExecutionRecoveryRefused(
            "RECOVERY_SCOPE_CHANGED",
            "The selected conflict no longer belongs to this bot. Refresh the Clerk view.",
        )
    if target.order.broker_order_id is None:
        raise HistoricalExecutionRecoveryRefused(
            "BROKER_ORDER_ID_REQUIRED",
            "The Clerk cannot prove the broker order identity for this conflict.",
        )
    try:
        account = await read.get_account()
        _require_paper_account(account_id=context.account_id, observed=account)
        activities = await read.list_activities(
            after_ms=max(0, target.observed_at_ms - _ACTIVITY_LOOKBACK_MS),
            limit=_ACTIVITY_LIMIT,
        )
    except BrokerError as exc:
        raise HistoricalExecutionRecoveryRefused(
            "HISTORICAL_EVIDENCE_UNAVAILABLE",
            "Alpaca activity history is temporarily unavailable. Keep the exposure blocked and retry later.",
        ) from exc
    activity = _require_exact_activity(
        activities=activities,
        execution_id=target.execution_id,
        broker_order_id=target.order.broker_order_id,
        symbol=target.symbol,
    )
    _require_matching_economics(activity=activity, cumulative=target.cumulative)
    now_ms = repo.clock()
    unsigned = {
        "account_id": context.account_id,
        "strategy_instance_id": context.strategy_instance_id,
        "uncertainty_id": target.uncertainty_id,
        "order_ref": target.order_ref,
        "broker_order_id": target.order.broker_order_id,
        "execution_id": activity.activity_id,
        "exact_symbol": activity.symbol,
        "exact_quantity": activity.quantity,
        "exact_price": activity.price,
        "exact_side": activity.side.upper(),
        "source_event_at_ms": activity.occurred_at_ms,
        "cumulative_fill_id": target.cumulative.fill_id,
        "cumulative_quantity": target.cumulative.quantity,
        "cumulative_price": target.cumulative.price,
        "cumulative_side": target.cumulative.side,
        "authority_generation": context.authority_generation,
        "db_identity_token": context.db_identity_token,
        "control_revision": context.control_revision,
        "prepared_at_ms": now_ms,
        "expires_at_ms": now_ms + _PLAN_TTL_MS,
    }
    return HistoricalExecutionRecoveryPlan(
        **unsigned,
        confirmation_token=_token(key, unsigned),
    )


def confirm_historical_execution_recovery(
    *,
    repo: ClerkSqliteRepository,
    plan: HistoricalExecutionRecoveryPlan,
    confirmation_token: str,
    control_secret: str,
    allow_unauthenticated_control: bool,
    observed_account: BrokerAccountSnapshot,
) -> ExecutionCoverageResolutionReceipt:
    """Verify a plan, then append quarantine and closed resolution under one writer."""
    key = _require_plan_key(
        control_secret=control_secret,
        allow_unauthenticated_control=allow_unauthenticated_control,
    )
    expected = _token(key, _unsigned_plan_payload(plan))
    if not (
        hmac.compare_digest(plan.confirmation_token, expected)
        and hmac.compare_digest(confirmation_token, expected)
    ):
        raise HistoricalExecutionRecoveryRefused(
            "RECOVERY_PLAN_INVALID",
            "The historical execution recovery plan does not verify. Prepare a new plan.",
        )
    if repo.clock() > plan.expires_at_ms:
        raise HistoricalExecutionRecoveryRefused(
            "RECOVERY_PLAN_EXPIRED",
            "The historical execution recovery plan expired. Prepare fresh broker evidence.",
        )
    _require_paper_account(account_id=plan.account_id, observed=observed_account)
    exact = ExecutionSliceFilledFacts(
        execution_id=plan.execution_id,
        symbol=plan.exact_symbol,
        side=plan.exact_side,
        slice_qty=plan.exact_quantity,
        slice_price=plan.exact_price,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="activity_recovery",
        source_event_at_ms=plan.source_event_at_ms,
    )
    try:
        return repo.recover_historical_execution_coverage(
            uncertainty_id=plan.uncertainty_id,
            strategy_instance_id=plan.strategy_instance_id,
            expected_authority_generation=plan.authority_generation,
            expected_db_identity_token=plan.db_identity_token,
            expected_control_revision=plan.control_revision,
            expected_order_ref=plan.order_ref,
            expected_broker_order_id=plan.broker_order_id,
            expected_cumulative_fill_id=plan.cumulative_fill_id,
            exact=exact,
        )
    except ExecutionCoverageResolutionUnavailable as exc:
        raise HistoricalExecutionRecoveryRefused(
            "RECOVERY_PLAN_STALE",
            "The Clerk evidence changed after planning. Refresh and prepare a new recovery plan.",
        ) from exc


__all__ = [
    "HistoricalExecutionRecoveryPlan",
    "HistoricalExecutionRecoveryRefused",
    "confirm_historical_execution_recovery",
    "prepare_historical_execution_recovery",
]

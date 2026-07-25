"""Submit server-stamped manual paper orders through the Account Clerk."""

from __future__ import annotations

import logging

from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountClerkLeaseUnavailableError,
    read_active_accepting_account_clerk_generation,
)
from app.engine.live.account_clerk_rpc import (
    AccountClerkRpcClient,
    AccountClerkRpcError,
    clerk_rejection_reason,
)
from app.engine.live.account_owner import (
    MANUAL_OPERATOR_RUN_ID,
    MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
    MANUAL_ORDER_INTENT_KIND,
    AccountOwnerSubmitIntent,
)
from app.engine.live.order_identity import build_manual_order_namespace, parse_order_ref
from app.services.account_truth_refresh import account_truth_artifacts_root
from app.services.clerk_transaction_projection import project_account_journal_best_effort
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class ManualOrderClerkUnavailableError(RuntimeError):
    """The Clerk cannot accept a manual order at the present time."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class ManualOrderClerkRejectedError(RuntimeError):
    """The Clerk declined a manual order after durable admission checks."""

    def __init__(self, reason_code: str, retry_safe: bool) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_safe = retry_safe


class ManualOrderBrokerReceiptError(RuntimeError):
    """The Clerk response did not carry the matching broker acknowledgement."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def _build_manual_order_intent(
    spec: IbkrOrderSpec,
    *,
    account_id: str,
    clerk_generation: int,
    created_at_ms: int,
) -> AccountOwnerSubmitIntent:
    """Map the server-stamped ticket to the Account Clerk's WAL shape."""

    if not spec.manual_order or spec.order_ref is None:
        raise ValueError("manual Clerk submit requires a server-stamped manual order_ref")
    namespace, intent_id = parse_order_ref(spec.order_ref)
    if namespace != build_manual_order_namespace("operator"):
        raise ValueError("manual Clerk submit requires the operator manual namespace")
    return AccountOwnerSubmitIntent(
        trace_id=f"manual-order:{intent_id}",
        account_id=account_id,
        strategy_instance_id=MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
        run_id=MANUAL_OPERATOR_RUN_ID,
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=spec.order_ref,
        intent_kind=MANUAL_ORDER_INTENT_KIND,
        order_spec=spec.model_dump(mode="json"),
        owner_generation=clerk_generation,
        created_at_ms=created_at_ms,
    )


async def submit_manual_order_through_clerk(
    spec: IbkrOrderSpec,
    *,
    account_id: str | None,
) -> IbkrOrderAck:
    """Submit one manual ticket through the sole durable account write lane."""

    if account_id is None:
        raise ManualOrderClerkUnavailableError(
            "ACCOUNT_ID_UNAVAILABLE",
            "The paper account is not available for a manual order.",
        )

    artifacts_root = account_truth_artifacts_root()
    now_ms = now_ms_utc()
    try:
        clerk_generation = read_active_accepting_account_clerk_generation(
            artifacts_root,
            account_id,
            now_ms=now_ms,
        )
    except AccountClerkLeaseUnavailableError as exc:
        raise ManualOrderClerkUnavailableError(
            exc.reason,
            "The account Clerk is not ready to accept a manual order.",
        ) from exc
    if clerk_generation is None:
        raise ManualOrderClerkUnavailableError(
            "CLERK_NOT_ACCEPTING",
            "The account Clerk is not accepting new manual orders.",
        )

    intent = _build_manual_order_intent(
        spec,
        account_id=account_id,
        clerk_generation=clerk_generation.generation,
        created_at_ms=now_ms,
    )
    try:
        receipt = await AccountClerkRpcClient(
            artifacts_root=artifacts_root,
            account_id=account_id,
        ).submit(intent)
    except AccountClerkRpcError as exc:
        retry_safe, reason_code = clerk_rejection_reason(exc)
        raise ManualOrderClerkRejectedError(reason_code, retry_safe) from exc

    await project_account_journal_best_effort(
        artifacts_root=artifacts_root,
        account_id=account_id,
    )

    ack = receipt.broker_ack
    if ack is None:
        logger.error(
            "manual order Clerk acknowledgement omitted the broker receipt",
            extra={"account_id": account_id, "order_ref": spec.order_ref},
        )
        raise ManualOrderBrokerReceiptError(
            "CLERK_BROKER_RECEIPT_MISSING",
            "The Clerk accepted the manual order but did not return its broker receipt.",
        )
    if ack.account_id != account_id or ack.order_ref != spec.order_ref:
        logger.error(
            "manual order Clerk acknowledgement identity mismatch",
            extra={"account_id": account_id, "order_ref": spec.order_ref},
        )
        raise ManualOrderBrokerReceiptError(
            "CLERK_BROKER_RECEIPT_MISMATCH",
            "The Clerk returned a broker receipt for a different manual order.",
        )
    return ack

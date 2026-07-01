"""Non-submitting IBKR what-if previews for paper orders."""

from __future__ import annotations

import asyncio

from app.broker.ibkr.account import _coerce_float_or_none
from app.broker.ibkr.api_evidence import (
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.models import (
    IbkrApiRequestEvidence,
    IbkrApiResponseEvidence,
    IbkrOrderSpec,
    IbkrOrderWhatIfPreview,
    IbkrTradeEvidence,
)
from app.broker.ibkr.order_evidence import snapshot_contract, snapshot_order
from app.broker.ibkr.orders import (
    _build_contract,
    _build_order,
    _enforce_paper_account_context,
)
from app.utils.timestamps import now_ms_utc

_WHAT_IF_TIMEOUT_S = 15.0


async def preview_paper_order(
    client: IbkrClient,
    spec: IbkrOrderSpec,
) -> IbkrOrderWhatIfPreview:
    """Run IBKR's what-if path for a paper order without submitting it."""
    client.require_live()
    account_id = _enforce_paper_account_context(client, operation="preview what-if order")

    contract = _build_contract(spec)
    try:
        qualified = await asyncio.wait_for(
            client.ib.qualifyContractsAsync(contract),
            timeout=_WHAT_IF_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR contract qualification for what-if timed out after "
            f"{_WHAT_IF_TIMEOUT_S:.0f}s; preview unavailable."
        ) from exc
    if not qualified:
        raise BrokerError(
            f"IBKR could not qualify contract for {spec.symbol} ({spec.sec_type})."
        )
    qualified_contract = qualified[0]

    order = _build_order(spec)
    order.whatIf = True
    try:
        state = await asyncio.wait_for(
            client.ib.whatIfOrderAsync(qualified_contract, order),
            timeout=_WHAT_IF_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR whatIfOrderAsync timed out after {_WHAT_IF_TIMEOUT_S:.0f}s."
        ) from exc

    contract_snapshot = snapshot_contract(qualified_contract)
    order_snapshot = snapshot_order(order)
    request = IbkrApiRequestEvidence(
        call="whatIfOrderAsync",
        params={
            "contract": (
                contract_snapshot.model_dump(mode="json")
                if contract_snapshot is not None
                else {}
            ),
            "order": (
                order_snapshot.model_dump(mode="json")
                if order_snapshot is not None
                else {}
            ),
        },
    )
    response = IbkrApiResponseEvidence(
        callback="whatIfOrder",
        fields={
            "initMarginChange": getattr(state, "initMarginChange", None),
            "maintMarginChange": getattr(state, "maintMarginChange", None),
            "equityWithLoanChange": getattr(state, "equityWithLoanChange", None),
            "commission": getattr(state, "commission", None),
            "warningText": getattr(state, "warningText", None),
        },
    )
    evidence = IbkrTradeEvidence(
        request=request,
        response=response,
        contract=contract_snapshot,
        order=order_snapshot,
    )
    get_ibkr_api_evidence_recorder().record(
        source="order_previews.preview_paper_order",
        account_id=account_id,
        symbol=spec.symbol,
        request=request,
        response=response,
    )
    return IbkrOrderWhatIfPreview(
        account_id=account_id,
        is_paper=True,
        symbol=spec.symbol,
        action=spec.action,
        quantity=float(spec.quantity),
        order_type=spec.order_type,
        init_margin_change=_coerce_float_or_none(
            getattr(state, "initMarginChange", None)
        ),
        maint_margin_change=_coerce_float_or_none(
            getattr(state, "maintMarginChange", None)
        ),
        equity_with_loan_change=_coerce_float_or_none(
            getattr(state, "equityWithLoanChange", None)
        ),
        commission=_coerce_float_or_none(getattr(state, "commission", None)),
        warning_text=(
            str(state.warningText)
            if getattr(state, "warningText", None)
            else None
        ),
        order_ref=spec.order_ref,
        ibkr_evidence=evidence,
        previewed_at_ms=now_ms_utc(),
    )

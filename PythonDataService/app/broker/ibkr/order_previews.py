"""Non-submitting IBKR what-if previews for paper orders."""

from __future__ import annotations

import asyncio

from app.broker.ibkr.api_evidence import (
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient, _is_paper_account
from app.broker.ibkr.config import LIVE_PORTS
from app.broker.ibkr.models import (
    IbkrApiRequestEvidence,
    IbkrApiResponseEvidence,
    IbkrOrderSpec,
    IbkrOrderWhatIfPreview,
    IbkrTradeEvidence,
)
from app.broker.ibkr.order_evidence import snapshot_contract, snapshot_order
from app.broker.ibkr.orders import OrderRefusedError, _build_contract, _build_order
from app.utils.timestamps import now_ms_utc

_WHAT_IF_TIMEOUT_S = 15.0


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _enforce_paper_preview_safety(client: IbkrClient) -> str:
    """Run paper-preview safety checks without requiring submit confirmation."""
    settings = client.settings
    account_id = client.connected_account
    if account_id is None:
        raise OrderRefusedError("No account id on connected client.")
    if settings.readonly:
        raise OrderRefusedError(
            "Refusing what-if preview: IBKR_READONLY=true (operator lockdown)."
        )
    if settings.mode != "paper":
        raise OrderRefusedError(
            f"Refusing what-if preview: IBKR_MODE is {settings.mode!r}, must be 'paper'."
        )
    if settings.port in LIVE_PORTS:
        raise OrderRefusedError(
            f"Refusing what-if preview: connected port {settings.port} is a LIVE Gateway port."
        )
    if not _is_paper_account(account_id):
        raise OrderRefusedError(
            f"Refusing what-if preview: account {account_id!r} does NOT begin with 'DU'."
        )
    return account_id


async def preview_paper_order(
    client: IbkrClient,
    spec: IbkrOrderSpec,
) -> IbkrOrderWhatIfPreview:
    """Run IBKR's what-if path for a paper order without submitting it."""
    client.require_live()
    account_id = _enforce_paper_preview_safety(client)

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
        init_margin_change=_float_or_none(getattr(state, "initMarginChange", None)),
        maint_margin_change=_float_or_none(getattr(state, "maintMarginChange", None)),
        equity_with_loan_change=_float_or_none(
            getattr(state, "equityWithLoanChange", None)
        ),
        commission=_float_or_none(getattr(state, "commission", None)),
        warning_text=(
            str(state.warningText)
            if getattr(state, "warningText", None)
            else None
        ),
        order_ref=spec.order_ref,
        ibkr_evidence=evidence,
        previewed_at_ms=now_ms_utc(),
    )

"""Broker-v2 bot control panel routes (transport only).

``/api/brokers/{broker}/...`` — the panel contract surface (spec §3-§8, §11).
The router validates/parses the HTTP request, calls the panel data-source
facade, and translates typed panel errors to HTTP. No business logic lives here
(router-freeze discipline).

Account scope (§3): read/projection/action endpoints are account-scoped
(``/accounts/{account_id}/...``) and validate ``account_id`` against the
broker's account (mismatch → 404). The unscoped forms are kept as aliases for
the single-account case — no breaking rename.

Identity (§14): control mutations authenticate via the always-on data-plane
control secret (the router prefix carries it); the server attaches the
configured ``PANEL_OPERATOR_IDENTITY`` — operator identity is never a request
field.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.schemas.broker_v2_evidence import EvidencePage
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    ChartHistoryResponse,
    ChartLiveResponse,
    PanelAction,
    PanelActionRequest,
    PanelActionResult,
    PanelProfile,
)
from app.services.broker_v2_panel import panel_data_source as ds
from app.services.broker_v2_panel.action_execution_service import ActionExecutionError
from app.services.broker_v2_panel.chart_projection_service import (
    ChartPresetError,
    coerce_history_preset,
)
from app.services.broker_v2_panel.evidence_service import (
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX,
    read_evidence_page,
)
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["broker-v2-panel"])


def _raise_panel_error(error: ds.PanelDataError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={"message": str(error), "why": error.detail},
    )


def _raise_action_error(error: ActionExecutionError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={"message": str(error), "why": error.detail},
    )


# ── §4 Panel capability profile (broker-level) ───────────────────────────────


@router.get(
    "/{broker}/panel-profile",
    response_model=PanelProfile,
    summary="Closed panel capability profile for this broker (§4)",
)
async def get_panel_profile(broker: str) -> PanelProfile:
    profile = panel_profile_for(broker)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Broker '{broker}' has no panel capability profile.",
                "why": "Only Alpaca exposes the broker-v2 panel in phase 1.",
            },
        )
    return profile


# ── §5 Catalog (account-scoped + unscoped alias) ─────────────────────────────


async def _catalog(broker: str, account_id: str) -> list[BotCatalogView]:
    try:
        return await ds.get_catalog(broker, account_id)
    except ds.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/catalog",
    response_model=list[BotCatalogView],
    summary="Bots-list roster: status + slice-0 rollups (§5)",
)
async def get_catalog_scoped(broker: str, account_id: str) -> list[BotCatalogView]:
    return await _catalog(broker, account_id)


@router.get(
    "/{broker}/bots/catalog",
    response_model=list[BotCatalogView],
    summary="Bots-list roster (single-account alias of the scoped route) (§5)",
)
async def get_catalog_unscoped(broker: str) -> list[BotCatalogView]:
    account_id = await _resolve_default_account(broker)
    return await _catalog(broker, account_id)


# ── §7 Panel projection (account-scoped + unscoped alias) ────────────────────


async def _panel(
    broker: str, account_id: str, sid: str, transaction_ref: str | None
) -> BotPanelView:
    try:
        return await ds.get_panel(
            broker, account_id, sid, transaction_ref=transaction_ref
        )
    except ds.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/panel",
    response_model=BotPanelView,
    summary="5s-poll panel projection for one bot (§7)",
)
async def get_panel_scoped(
    broker: str,
    account_id: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
) -> BotPanelView:
    return await _panel(broker, account_id, sid, transaction_ref)


@router.get(
    "/{broker}/bots/{sid}/panel",
    response_model=BotPanelView,
    summary="Panel projection (single-account alias) (§7)",
)
async def get_panel_unscoped(
    broker: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
) -> BotPanelView:
    account_id = await _resolve_default_account(broker)
    return await _panel(broker, account_id, sid, transaction_ref)


# ── §11 Presented-action execution (account-scoped + unscoped alias) ─────────


async def _run_action(
    broker: str, account_id: str, sid: str, request: PanelActionRequest
) -> PanelActionResult:
    try:
        return await ds.run_action(
            broker,
            account_id,
            sid,
            request,
            operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        )
    except ds.PanelDataError as error:
        _raise_panel_error(error)
    except ActionExecutionError as error:
        _raise_action_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/bots/{sid}/actions",
    response_model=PanelActionResult,
    summary="Execute one presented action (revision-guarded, idempotent) (§11)",
)
async def run_action_scoped(
    broker: str, account_id: str, sid: str, request: PanelActionRequest
) -> PanelActionResult:
    return await _run_action(broker, account_id, sid, request)


@router.post(
    "/{broker}/bots/{sid}/actions",
    response_model=PanelActionResult,
    summary="Execute one presented action (single-account alias) (§11)",
)
async def run_action_unscoped(
    broker: str, sid: str, request: PanelActionRequest
) -> PanelActionResult:
    account_id = await _resolve_default_account(broker)
    return await _run_action(broker, account_id, sid, request)


# ── §8 Chart endpoints (account-scoped + unscoped alias) ─────────────────────


async def _live_chart(broker: str, account_id: str, sid: str) -> ChartLiveResponse:
    try:
        return await ds.get_live_chart(broker, account_id, sid)
    except ds.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/chart/live",
    response_model=ChartLiveResponse,
    summary="LIVE chart pane: today's merged bars + fill markers (§8)",
)
async def get_live_chart_scoped(
    broker: str, account_id: str, sid: str
) -> ChartLiveResponse:
    return await _live_chart(broker, account_id, sid)


@router.get(
    "/{broker}/bots/{sid}/chart/live",
    response_model=ChartLiveResponse,
    summary="LIVE chart pane (single-account alias) (§8)",
)
async def get_live_chart_unscoped(broker: str, sid: str) -> ChartLiveResponse:
    account_id = await _resolve_default_account(broker)
    return await _live_chart(broker, account_id, sid)


async def _history_chart(
    broker: str, account_id: str, sid: str, preset: str
) -> ChartHistoryResponse:
    try:
        coerced = coerce_history_preset(preset)
    except ChartPresetError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "why": None}) from None
    try:
        return await ds.get_history_chart(broker, account_id, sid, coerced)
    except ds.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/chart/history",
    response_model=ChartHistoryResponse,
    summary="HISTORY chart pane: bounded preset ladder (§8)",
)
async def get_history_chart_scoped(
    broker: str,
    account_id: str,
    sid: str,
    preset: str = Query(..., max_length=8),
) -> ChartHistoryResponse:
    return await _history_chart(broker, account_id, sid, preset)


@router.get(
    "/{broker}/bots/{sid}/chart/history",
    response_model=ChartHistoryResponse,
    summary="HISTORY chart pane (single-account alias) (§8)",
)
async def get_history_chart_unscoped(
    broker: str,
    sid: str,
    preset: str = Query(..., max_length=8),
) -> ChartHistoryResponse:
    account_id = await _resolve_default_account(broker)
    return await _history_chart(broker, account_id, sid, preset)


# ── §14 Operator-gated evidence (account-scoped + unscoped alias) ─────────────


async def _read_evidence(
    broker: str,
    account_id: str,
    sid: str,
    transaction_ref: str | None,
    cursor: int | None,
    page_size: int,
    client_hint: str | None,
) -> EvidencePage:
    try:
        await ds.validate_account_scope(broker, account_id, sid)
    except ds.PanelDataError as error:
        _raise_panel_error(error)
    # read_evidence_page is outside the try/except: it raises only OSError
    # (audit log write failure, which is logged-and-swallowed inside) and does
    # not raise PanelDataError, so wrapping it here would mask real I/O errors.
    return read_evidence_page(
        account_id=account_id,
        sid=sid,
        transaction_ref=transaction_ref,
        cursor=cursor,
        page_size=page_size,
        operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        client_hint=client_hint,
    )


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/evidence",
    response_model=EvidencePage,
    summary="Operator-gated raw evidence for one bot (bounded, paged, audit-logged) (§14)",
)
async def get_evidence_scoped(
    broker: str,
    account_id: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
    cursor: int | None = Query(default=None, ge=0),
    page_size: int = Query(
        default=PAGE_SIZE_DEFAULT, ge=1
    ),
    client_hint: str | None = Query(default=None, max_length=256),
) -> EvidencePage:
    return await _read_evidence(
        broker, account_id, sid, transaction_ref, cursor, page_size, client_hint
    )


@router.get(
    "/{broker}/bots/{sid}/evidence",
    response_model=EvidencePage,
    summary="Operator-gated raw evidence (single-account alias) (§14)",
)
async def get_evidence_unscoped(
    broker: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
    cursor: int | None = Query(default=None, ge=0),
    page_size: int = Query(
        default=PAGE_SIZE_DEFAULT, ge=1
    ),
    client_hint: str | None = Query(default=None, max_length=256),
) -> EvidencePage:
    account_id = await _resolve_default_account(broker)
    return await _read_evidence(
        broker, account_id, sid, transaction_ref, cursor, page_size, client_hint
    )


# ── Shared helpers ───────────────────────────────────────────────────────────


async def _resolve_default_account(broker: str) -> str:
    """Resolve the broker's single account for the unscoped alias routes.

    The unscoped forms serve the single-account case (§3); they resolve the
    real account and then delegate to the same validated path.
    """
    try:
        return await ds.resolve_account_id(broker)
    except ds.PanelDataError as error:
        _raise_panel_error(error)


# Re-export ``PanelAction`` for the OpenAPI schema (nested inside BotPanelView).
__all__ = ["PanelAction", "router"]

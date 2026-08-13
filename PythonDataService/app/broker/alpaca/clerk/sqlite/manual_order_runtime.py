"""Policy and freshness binding for the first SQLite manual-order tracer."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.manual_orders import (
    ManualOrderSubmission,
    manual_order_command_id,
    submit_manual_order,
)
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot, ManualOrderTicketResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError, require_manual_admission
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.contract.models import BrokerAccountSnapshot, BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

_LOCAL_PREVIEW_KEY = b"learn-ai/local-manual-preview/v1"


@dataclass(frozen=True)
class ManualOrderUnavailable:
    code: str
    message: str


@dataclass(frozen=True)
class ManualOrderCapability:
    available: bool
    unavailable: ManualOrderUnavailable | None = None


@dataclass(frozen=True)
class ManualOrderPreview:
    capability: ManualOrderCapability
    preview_token: str | None
    authority_generation: int | None
    db_identity_token: str | None
    control_revision: int | None
    subject_id: str | None


class ManualPreviewStaleError(ValueError):
    """The browser confirmation no longer matches current authoritative facts."""


def _preview_key(*, control_secret: str, allow_unauthenticated_control: bool) -> bytes | None:
    configured = control_secret.strip().encode("utf-8")
    if configured:
        return hmac.new(configured, b"learn-ai/sqlite-manual-preview/v1", hashlib.sha256).digest()
    return _LOCAL_PREVIEW_KEY if allow_unauthenticated_control else None


def _preview_token(
    *,
    key: bytes,
    account_id: str,
    operator_id: str,
    ticket_id: str,
    leg_id: str,
    leg: BrokerOrderLeg,
    meta: ControlMetaSnapshot,
) -> str:
    payload = canonicalize(
        {
            "account_id": account_id,
            "operator_id": operator_id,
            "ticket_id": ticket_id,
            "leg_id": leg_id,
            "leg": leg.model_dump(mode="json"),
            "authority_generation": meta.authority_generation,
            "db_identity_token": meta.db_identity_token,
            "control_revision": meta.control_revision,
        }
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _account_unavailable(account: BrokerAccountSnapshot, account_id: str) -> ManualOrderUnavailable | None:
    if account.account_id != account_id:
        return ManualOrderUnavailable(
            "BROKER_ACCOUNT_MISMATCH",
            "The configured Alpaca account no longer matches this SQLite authority.",
        )
    if account.account_mode != "paper":
        return ManualOrderUnavailable(
            "LIVE_ACCOUNT_REFUSED",
            "Manual orders are available only for the qualified Alpaca paper account.",
        )
    if account.account_status.upper() != "ACTIVE" or account.trading_blocked or account.account_blocked:
        return ManualOrderUnavailable(
            "PAPER_ACCOUNT_NOT_TRADABLE",
            "The Alpaca paper account is not active and tradable.",
        )
    return None


async def preview_manual_order(
    *,
    repo: ClerkSqliteRepository,
    read: BrokerReadPort,
    stream_health: StreamHealthGate | None,
    manual_trading_enabled: bool,
    control_secret: str,
    allow_unauthenticated_control: bool,
    account_id: str,
    operator_id: str,
    ticket_id: str,
    leg_id: str,
    leg: BrokerOrderLeg,
) -> ManualOrderPreview:
    """Build the backend-owned capability and opaque freshness token."""
    if (
        leg.side is not OrderSide.BUY
        or leg.order_type is not OrderType.MARKET
        or leg.time_in_force is not TimeInForce.DAY
    ):
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable(
                    "UNSUPPORTED_MANUAL_ORDER_SHAPE",
                    "The manual SQLite tracer supports one BUY market DAY equity leg.",
                ),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=None,
        )
    capability = await manual_order_capability(
        read=read,
        stream_health=stream_health,
        manual_trading_enabled=manual_trading_enabled,
        control_secret=control_secret,
        allow_unauthenticated_control=allow_unauthenticated_control,
        account_id=account_id,
        symbol=leg.symbol,
    )
    if not capability.available:
        return ManualOrderPreview(
            capability=capability,
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=None,
        )
    subject_id = manual_operator_subject_id(operator_id)
    try:
        require_manual_admission(repo, subject_id=subject_id)
    except AdmissionBlockedError as exc:
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable(exc.decision.reason_code or "ADMISSION_BLOCKED", exc.decision.why or str(exc)),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=subject_id,
        )
    meta = repo.control_meta_snapshot()
    key = _preview_key(
        control_secret=control_secret,
        allow_unauthenticated_control=allow_unauthenticated_control,
    )
    assert key is not None
    return ManualOrderPreview(
        capability=capability,
        preview_token=_preview_token(
            key=key,
            account_id=account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            leg_id=leg_id,
            leg=leg,
            meta=meta,
        ),
        authority_generation=meta.authority_generation,
        db_identity_token=meta.db_identity_token,
        control_revision=meta.control_revision,
        subject_id=subject_id,
    )


async def manual_order_capability(
    *,
    read: BrokerReadPort,
    stream_health: StreamHealthGate | None,
    manual_trading_enabled: bool,
    control_secret: str,
    allow_unauthenticated_control: bool,
    account_id: str,
    symbol: str | None = None,
) -> ManualOrderCapability:
    """Evaluate the policy facts shared by capability and preview endpoints."""
    if not manual_trading_enabled:
        return ManualOrderCapability(
            False,
            ManualOrderUnavailable(
                "MANUAL_TRADING_NOT_QUALIFIED",
                "Manual SQLite trading remains disabled until paper qualification is complete.",
            ),
        )
    key = _preview_key(
        control_secret=control_secret,
        allow_unauthenticated_control=allow_unauthenticated_control,
    )
    if key is None:
        return ManualOrderCapability(
            False,
            ManualOrderUnavailable(
                "CONTROL_AUTHENTICATION_UNAVAILABLE",
                "Manual SQLite trading requires the configured control-plane credential.",
            ),
        )
    account = await read.get_account()
    account_failure = _account_unavailable(account, account_id)
    if account_failure is not None:
        return ManualOrderCapability(False, account_failure)
    if stream_health is None:
        return ManualOrderCapability(
            False,
            ManualOrderUnavailable(
                "CHANNEL_HEALTH_UNAVAILABLE",
                "Manual orders require installed market-data and execution channel health evidence.",
            ),
        )
    unhealthy = tuple(health for health in stream_health.snapshot(symbol) if not health.healthy)
    if unhealthy:
        return ManualOrderCapability(
            False,
            ManualOrderUnavailable(
                "BROKER_CHANNEL_UNHEALTHY",
                "; ".join(f"{health.stream}: {health.reason}" for health in unhealthy),
            ),
        )
    return ManualOrderCapability(True)


async def submit_previewed_manual_order(
    *,
    repo: ClerkSqliteRepository,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    stream_health: StreamHealthGate | None,
    manual_trading_enabled: bool,
    control_secret: str,
    allow_unauthenticated_control: bool,
    account_id: str,
    operator_id: str,
    ticket_id: str,
    leg_id: str,
    leg: BrokerOrderLeg,
    preview_token: str,
) -> ManualOrderSubmission:
    """Recheck a fresh preview, except for a safe replay of existing custody."""
    if repo.get_command(manual_order_command_id(ticket_id, leg_id)) is None:
        preview = await preview_manual_order(
            repo=repo,
            read=read,
            stream_health=stream_health,
            manual_trading_enabled=manual_trading_enabled,
            control_secret=control_secret,
            allow_unauthenticated_control=allow_unauthenticated_control,
            account_id=account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            leg_id=leg_id,
            leg=leg,
        )
        if not preview.capability.available:
            assert preview.capability.unavailable is not None
            raise ManualPreviewStaleError(preview.capability.unavailable.message)
        if not hmac.compare_digest(preview.preview_token or "", preview_token):
            raise ManualPreviewStaleError(
                "The manual-order preview is stale. Refresh the ticket before confirming."
            )
    return await submit_manual_order(
        repo,
        account_id=account_id,
        operator_id=operator_id,
        ticket_id=ticket_id,
        leg_id=leg_id,
        leg=leg,
        trade=trade,
    )


def get_manual_ticket(repo: ClerkSqliteRepository, ticket_id: str) -> ManualOrderTicketResource | None:
    return repo.manual_order_ticket(ticket_id)


__all__ = [
    "ManualOrderCapability",
    "ManualOrderPreview",
    "ManualOrderUnavailable",
    "ManualPreviewStaleError",
    "get_manual_ticket",
    "manual_order_capability",
    "preview_manual_order",
    "submit_previewed_manual_order",
]

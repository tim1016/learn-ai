"""Policy and freshness binding for the first SQLite manual-order tracer."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.manual_orders import (
    ManualOrderSubmission,
    ManualPreviewRevision,
    ManualTicketContinuationError,
    ManualTicketLeg,
    manual_order_command_id,
    next_manual_ticket_leg,
    submit_manual_order,
)
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot, ManualOrderTicketResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    ReductionIntent,
    require_manual_admission,
    require_manual_reduction,
)
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerAccountSnapshot, OrderSide, OrderType, TimeInForce
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.identity import validate_strategy_instance_id

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

    @property
    def revision(self) -> ManualPreviewRevision | None:
        """Return the atomic authority revision represented by this preview."""
        if (
            self.authority_generation is None
            or self.db_identity_token is None
            or self.control_revision is None
        ):
            return None
        return ManualPreviewRevision(
            authority_generation=self.authority_generation,
            db_identity_token=self.db_identity_token,
            control_revision=self.control_revision,
        )


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
    legs: tuple[ManualTicketLeg, ...],
    meta: ControlMetaSnapshot,
) -> str:
    payload = canonicalize(
        {
            "account_id": account_id,
            "operator_id": operator_id,
            "ticket_id": ticket_id,
            "legs": [{"leg_id": leg.leg_id, "instruction": leg.instruction.model_dump(mode="json")} for leg in legs],
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


async def _manual_asset_unavailable(
    read: BrokerReadPort,
    *,
    symbol: str,
) -> ManualOrderUnavailable | None:
    """Require fresh broker-listed, tradable US-equity evidence before intent."""
    try:
        asset = await read.get_asset(symbol)
    except BrokerError:
        return ManualOrderUnavailable(
            "ASSET_EVIDENCE_UNAVAILABLE",
            "Manual order preview requires fresh broker asset eligibility evidence.",
        )
    if (
        asset is None
        or asset.asset_class.lower() != "us_equity"
        or asset.status.lower() != "active"
        or not asset.tradable
    ):
        return ManualOrderUnavailable(
            "UNSUPPORTED_MANUAL_ASSET",
            "The selected symbol is not an active, tradable US equity in Alpaca.",
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
    legs: tuple[ManualTicketLeg, ...],
    continuation: bool = False,
) -> ManualOrderPreview:
    """Build the backend-owned capability and opaque freshness token."""
    try:
        validate_strategy_instance_id(operator_id)
    except ValueError:
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable(
                    "INVALID_MANUAL_OPERATOR",
                    "Manual order submission requires a valid configured operator identity.",
                ),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=None,
        )
    if (
        not legs
        or len(legs) > 8
        or any(not leg.leg_id for leg in legs)
        or len({leg.leg_id for leg in legs}) != len(legs)
    ):
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable(
                    "INVALID_MANUAL_TICKET",
                    "A manual ticket requires between one and eight uniquely identified ordered legs.",
                ),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=None,
        )
    subject_id = manual_operator_subject_id(operator_id)
    observed_meta = repo.control_meta_snapshot()
    existing_ticket = repo.manual_order_ticket(ticket_id)
    continuation_ticket_id = (
        ticket_id if existing_ticket is not None and existing_ticket.subject_id == subject_id else None
    )
    try:
        active_leg_id = (
            next_manual_ticket_leg(repo, ticket_id=ticket_id).leg_id
            if continuation_ticket_id is not None
            else legs[0].leg_id
        )
    except ManualTicketContinuationError as exc:
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable("STALE_MANUAL_TICKET", str(exc)),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=subject_id,
        )
    if active_leg_id not in {leg.leg_id for leg in legs}:
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable(
                    "STALE_MANUAL_TICKET",
                    "The refreshed manual ticket no longer contains its next eligible leg.",
                ),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=subject_id,
        )
    for ticket_leg in legs:
        leg = ticket_leg.instruction
        if (
            leg.side not in {OrderSide.BUY, OrderSide.SELL}
            or leg.order_type not in {OrderType.MARKET, OrderType.LIMIT}
            or leg.time_in_force not in {TimeInForce.DAY, TimeInForce.GTC}
        ):
            return ManualOrderPreview(
                capability=ManualOrderCapability(
                    False,
                    ManualOrderUnavailable(
                        "UNSUPPORTED_MANUAL_ORDER_SHAPE",
                        "Manual tickets support only BUY or SELL market/limit DAY/GTC equity legs.",
                    ),
                ),
                preview_token=None,
                authority_generation=None,
                db_identity_token=None,
                control_revision=None,
                subject_id=subject_id,
            )
        if ticket_leg.leg_id != active_leg_id:
            continue
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
                subject_id=subject_id,
            )
        asset_failure = await _manual_asset_unavailable(read, symbol=leg.symbol)
        if asset_failure is not None:
            return ManualOrderPreview(
                capability=ManualOrderCapability(False, asset_failure),
                preview_token=None,
                authority_generation=None,
                db_identity_token=None,
                control_revision=None,
                subject_id=subject_id,
            )
        try:
            if leg.side is OrderSide.BUY:
                require_manual_admission(
                    repo,
                    subject_id=subject_id,
                    continuation_ticket_id=continuation_ticket_id,
                )
            else:
                require_manual_reduction(
                    repo,
                    subject_id=subject_id,
                    intent=ReductionIntent(symbol=leg.symbol, side=leg.side.value, quantity=leg.quantity),
                )
        except AdmissionBlockedError as exc:
            return ManualOrderPreview(
                capability=ManualOrderCapability(
                    False,
                    ManualOrderUnavailable(
                        exc.decision.reason_code or "ADMISSION_BLOCKED", exc.decision.why or str(exc)
                    ),
                ),
                preview_token=None,
                authority_generation=None,
                db_identity_token=None,
                control_revision=None,
                subject_id=subject_id,
            )
    meta = repo.control_meta_snapshot()
    if meta != observed_meta:
        return ManualOrderPreview(
            capability=ManualOrderCapability(
                False,
                ManualOrderUnavailable(
                    "STALE_MANUAL_TICKET",
                    "The Clerk changed while broker eligibility was observed. Refresh the ticket.",
                ),
            ),
            preview_token=None,
            authority_generation=None,
            db_identity_token=None,
            control_revision=None,
            subject_id=subject_id,
        )
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
            legs=legs,
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
    legs: tuple[ManualTicketLeg, ...],
    preview_token: str,
    continuation: bool = False,
) -> ManualOrderSubmission:
    """Recheck one ticket preview, then activate only its selected serial leg."""
    if not legs:
        raise ManualPreviewStaleError("The manual ticket has no leg to submit.")
    try:
        active_leg = next_manual_ticket_leg(repo, ticket_id=ticket_id) if continuation else legs[0]
    except ManualTicketContinuationError as exc:
        raise ManualPreviewStaleError(str(exc)) from exc
    if continuation and active_leg.leg_id not in {leg.leg_id for leg in legs}:
        raise ManualPreviewStaleError("The refreshed manual ticket no longer contains its next leg.")
    preview: ManualOrderPreview | None = None
    if repo.get_command(manual_order_command_id(ticket_id, active_leg.leg_id)) is None:
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
            legs=legs,
            continuation=continuation,
        )
        if not preview.capability.available:
            assert preview.capability.unavailable is not None
            raise ManualPreviewStaleError(preview.capability.unavailable.message)
        if not hmac.compare_digest(preview.preview_token or "", preview_token):
            raise ManualPreviewStaleError("The manual-order preview is stale. Refresh the ticket before confirming.")
    try:
        return await submit_manual_order(
            repo,
            account_id=account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            leg_id=active_leg.leg_id,
            leg=active_leg.instruction,
            trade=trade,
            ticket_legs=legs,
            continuation=continuation,
            expected_preview_revision=preview.revision if preview is not None else None,
        )
    except ManualTicketContinuationError as exc:
        raise ManualPreviewStaleError(str(exc)) from exc


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

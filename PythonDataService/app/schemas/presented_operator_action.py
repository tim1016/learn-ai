"""Dependency-light envelope for the surviving reconciliation action."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings

PresentedOperatorActionId = Literal["reconcile_now"]
PresentedOperatorEffectClass = Literal["EVIDENCE_REFRESH"]
PresentedOperatorActionAvailability = Literal["AVAILABLE", "UNAVAILABLE"]
PresentedOperatorActionDisposition = Literal["fix_here", "wait"]
_UNAUTHENTICATED_LOCAL_PRESENTATION_KEY = b"learn-ai/local-presented-action-envelope/v1"


class PresentedOperatorActionTarget(BaseModel):
    """The account identity bound into a reconciliation presentation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)


class PresentedOperatorActionPrecondition(BaseModel):
    """One server-owned fact that must still hold when an action executes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    expected_value: str = Field(min_length=1, max_length=160)


class PresentedOperatorActionInvocation(BaseModel):
    """The browser may return only this closed, signed-by-snapshot envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: PresentedOperatorActionId
    target: PresentedOperatorActionTarget
    snapshot_id: str = Field(min_length=16, max_length=64)
    snapshot_version: str = Field(min_length=16, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=160)
    issued_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    presentation_token: str = Field(min_length=64, max_length=64)


def issue_presented_operator_action_token(
    *,
    action_id: PresentedOperatorActionId,
    target: PresentedOperatorActionTarget,
    snapshot_id: str,
    snapshot_version: str,
    idempotency_key: str,
    issued_at_ms: int,
    expires_at_ms: int,
) -> str:
    """Sign one presentation lifetime without persisting during a read."""

    payload = json.dumps(
        {
            "action_id": action_id,
            "target": target.model_dump(mode="json"),
            "snapshot_id": snapshot_id,
            "snapshot_version": snapshot_version,
            "idempotency_key": idempotency_key,
            "issued_at_ms": issued_at_ms,
            "expires_at_ms": expires_at_ms,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(_presented_action_signing_key(), payload, hashlib.sha256).hexdigest()


def has_valid_presented_operator_action_token(
    invocation: PresentedOperatorActionInvocation,
) -> bool:
    """Verify an opaque presentation token before reading/replaying an attempt."""

    try:
        expected = issue_presented_operator_action_token(
            action_id=invocation.action_id,
            target=invocation.target,
            snapshot_id=invocation.snapshot_id,
            snapshot_version=invocation.snapshot_version,
            idempotency_key=invocation.idempotency_key,
            issued_at_ms=invocation.issued_at_ms,
            expires_at_ms=invocation.expires_at_ms,
        )
    except RuntimeError:
        return False
    return hmac.compare_digest(invocation.presentation_token, expected)


def presented_operator_action_signing_available() -> bool:
    """Whether this process can issue server-verifiable action presentations."""

    return bool(settings.DATA_PLANE_CONTROL_SECRET.strip()) or settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL


def _presented_action_signing_key() -> bytes:
    configured_secret = settings.DATA_PLANE_CONTROL_SECRET.strip().encode()
    if configured_secret:
        return hmac.new(
            configured_secret,
            b"learn-ai/presented-action-envelope/v1",
            hashlib.sha256,
        ).digest()
    if settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL:
        return _UNAUTHENTICATED_LOCAL_PRESENTATION_KEY
    raise RuntimeError("Presented action signing requires DATA_PLANE_CONTROL_SECRET.")

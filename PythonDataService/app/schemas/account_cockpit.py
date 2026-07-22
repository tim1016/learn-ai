"""Backend-authored Account Clerk cockpit contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.account_directory import AccountServiceStatusResponse
from app.schemas.operator_blocker import OperatorBlocker

AccountCockpitMode = Literal["NORMAL", "CLERK_DOWN", "DAEMON_DOWN", "DAEMON_UNREADABLE"]
AccountCockpitDaemonAvailability = Literal["AVAILABLE", "DOWN", "UNREADABLE"]


class AccountCockpitDaemon(BaseModel):
    """Host-daemon observation used only for honest cockpit guidance."""

    model_config = ConfigDict(frozen=True)

    availability: AccountCockpitDaemonAvailability
    reason_code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=512)
    observed_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)


class AccountCockpitResponse(BaseModel):
    """One display/control projection for a single account cockpit page."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    generated_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    mode: AccountCockpitMode
    clerk: AccountServiceStatusResponse
    daemon: AccountCockpitDaemon
    blockers: list[OperatorBlocker] = Field(default_factory=list)


class AccountClerkRestoreRequest(BaseModel):
    """Typed confirmation for the daemon-supervised Clerk restore operation."""

    model_config = ConfigDict(frozen=True)

    confirmation_token: Literal["RESTORE"]
    idempotency_key: str = Field(min_length=1, max_length=256)


class AccountClerkRestoreReceipt(BaseModel):
    """Durable account-event receipt for a completed Clerk restore."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    receipt_id: str = Field(min_length=1, max_length=320)
    account_id: str = Field(min_length=1, max_length=64)
    clerk_generation: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)

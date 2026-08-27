"""Wire model for the durable legacy stale-claim retirement receipt.

Every other model that used to live in this file (account reconciliation
receipts, triage, freeze, exposure-override, session-policy, and
presented-action request/response wire models — 26 classes) retired along
with the account authority they served: routers/account_reconciliation.py
and app/services/account_reconciliation.py (PR-A of #1813). This one class
survives because app.services.legacy_stale_claim_retirement.py's
retired_legacy_claim_keys() — still called by fleet_contamination.py —
needs it to deserialize the durable retirement-receipt artifact; it has no
other dependency on the retired schema tree.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LegacyStaleClaimRetirementReceipt(BaseModel):
    """Durable proof that one legacy per-run claim is no longer active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    receipt_id: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    claimed_quantity: int
    requested_by: str = Field(min_length=1, max_length=128)
    retired_at_ms: int = Field(ge=0)


__all__ = ["LegacyStaleClaimRetirementReceipt"]

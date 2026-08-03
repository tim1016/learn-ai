"""Read-only Clerk↔broker custody diagnosis (account-scoped).

Pure projection: reuse the existing divergence folds (``derive``/``exposure``)
to produce a structured, backend-authored diagnosis the Accounts page renders
verbatim. Never mutates the journal or the broker.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.broker.alpaca.clerk import derive, exposure, reconcile
from app.broker.alpaca.clerk.models import OrderJournalEntry
from app.broker.contract.models import BrokerOrder, BrokerPosition

CustodyDivergenceKind = Literal[
    "exposure_attribution_mismatch",
    "exposure_hold",
    "stale_reconciliation",
    "needs_review",
]
CustodyDivergenceState = Literal[
    "resolvable_now", "blocked_on_prerequisite", "needs_review"
]
CustodyActionId = Literal["reconcile_now", "record_inventory_baseline", "clear_hold"]


class CustodyPositionDelta(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    clerk_attributed_qty: float
    broker_observed_qty: float


class CustodyDivergence(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: CustodyDivergenceKind
    state: CustodyDivergenceState
    explanation: str
    possible_causes: tuple[str, ...]
    position_deltas: tuple[CustodyPositionDelta, ...] = ()
    resolution_step: CustodyActionId | None = None
    prerequisite_detail: str | None = None
    evidence_refs: tuple[str, ...] = ()


class CustodyResolutionStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: CustodyActionId
    scope: Literal["account", "bot", "broker"]
    mutates: bool


class CustodyDiagnosis(BaseModel):
    model_config = ConfigDict(frozen=True)
    broker: str
    account_id: str
    in_sync: bool
    observed_at_ms: int
    snapshot_version: str
    resolution_posture: Literal["paper", "live"] = "paper"
    resolvable: bool = False
    blocked_reason: str | None = None
    divergences: tuple[CustodyDivergence, ...] = ()
    resolution_plan: tuple[CustodyResolutionStep, ...] = ()


# ── Backend-authored copy (rendered verbatim by the client) ─────────────────
_CAUSES: dict[CustodyDivergenceKind, tuple[str, ...]] = {
    "exposure_attribution_mismatch": (
        "A bot process was terminated mid-run before its fill was journaled.",
        "An unclean shutdown interrupted the reconciliation sweep.",
        "A manual or foreign order changed the position outside bot custody.",
        "A broker fill landed after the Clerk's last durable snapshot.",
    ),
    "exposure_hold": (
        "An order this account did not submit appeared at the broker.",
        "A prior clear-hold was issued while the foreign order persisted, so the "
        "next sweep re-raised the hold.",
    ),
    "stale_reconciliation": (
        "The broker was unreachable during the last reconciliation sweep.",
        "The data-plane restarted before a fresh sweep completed.",
    ),
    "needs_review": (
        "The Clerk submitted an order the broker reports neither as working nor "
        "filled — its true outcome cannot be proven automatically.",
    ),
}
_EXPLANATION: dict[CustodyDivergenceKind, str] = {
    "exposure_attribution_mismatch": (
        "The broker holds exposure the Clerk cannot map to a recorded intent. "
        "Adopting the broker's observed inventory as the account baseline "
        "restores exact custody."
    ),
    "exposure_hold": (
        "The Clerk raised an exposure hold and is refusing new submissions until "
        "an operator confirms the account is safe."
    ),
    "stale_reconciliation": (
        "The Clerk could not establish current order and exposure truth from a "
        "fresh broker observation. Reconcile once the broker is reachable."
    ),
    "needs_review": (
        "An unresolved submission cannot be mapped to any broker outcome. This "
        "needs manual review before any custody cutover."
    ),
}


def custody_snapshot_version(
    entries: list[OrderJournalEntry],
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
) -> str:
    """Stable hash of the salient custody state (the resolve concurrency guard)."""
    payload = {
        "expected": exposure.project_expected_account_exposure(entries),
        "observed": {
            p.symbol.upper(): exposure.signed_broker_position_quantity(p)
            for p in positions
            if p.quantity != 0
        },
        "hold": derive.hold_state(entries).active,
        "working_orders": sorted(
            o.order_id
            for o in orders
            if o.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def diagnose_custody(
    entries: list[OrderJournalEntry],
    *,
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
    namespaces: frozenset[str],
) -> tuple[CustodyDivergence, ...]:
    """Project current Clerk↔broker divergences (pure; no mutation)."""
    divergences: list[CustodyDivergence] = []

    hold = derive.hold_state(entries)
    if hold.active:
        divergences.append(
            CustodyDivergence(
                kind="exposure_hold",
                state="resolvable_now",
                explanation=_EXPLANATION["exposure_hold"],
                possible_causes=_CAUSES["exposure_hold"],
                resolution_step="clear_hold",
                evidence_refs=tuple(sorted(derive.unexplained_order_ids(entries))),
            )
        )

    deltas = _attribution_deltas(entries, positions)
    if deltas:
        unresolved = derive.unresolved_intents(entries)
        working = [
            o
            for o in orders
            if o.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ]
        state: CustodyDivergenceState = "resolvable_now"
        prerequisite: str | None = None
        if working:
            state = "blocked_on_prerequisite"
            prerequisite = (
                f"{len(working)} working order(s) are open. Cancel or settle them "
                "before adopting a baseline."
            )
        elif unresolved:
            state = "blocked_on_prerequisite"
            prerequisite = (
                f"{len(unresolved)} unresolved submission(s) must reconcile before "
                "a baseline cutover."
            )
        divergences.append(
            CustodyDivergence(
                kind="exposure_attribution_mismatch",
                state=state,
                explanation=_EXPLANATION["exposure_attribution_mismatch"],
                possible_causes=_CAUSES["exposure_attribution_mismatch"],
                position_deltas=deltas,
                resolution_step="record_inventory_baseline",
                prerequisite_detail=prerequisite,
            )
        )

    return tuple(divergences)


def _attribution_deltas(
    entries: list[OrderJournalEntry], positions: list[BrokerPosition]
) -> tuple[CustodyPositionDelta, ...]:
    """Per-symbol (attributed vs observed) drift, skipping in-flight symbols.

    Delegates to ``exposure.account_exposure_deltas`` — the shared fold also
    used by ``derive.has_missing_intent``'s position-drift branch — and shapes
    the result into position-delta rows instead of a boolean.
    """
    inflight = derive.inflight_order_symbols(entries)
    deltas = exposure.account_exposure_deltas(
        entries, positions, inflight_symbols=inflight
    )
    return tuple(
        CustodyPositionDelta(
            symbol=symbol, clerk_attributed_qty=expected_qty, broker_observed_qty=observed_qty
        )
        for symbol, (expected_qty, observed_qty) in sorted(deltas.items())
    )


class CustodyResolutionStepResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: str
    message: str


class CustodyResolutionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)
    broker: str
    account_id: str
    resolved: bool
    receipt_id: str
    recorded_at_ms: int
    steps_executed: tuple[CustodyResolutionStepResult, ...] = ()
    in_sync: bool = False
    remaining_divergences: tuple[CustodyDivergence, ...] = ()


class CustodyResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=512)
    snapshot_version: str = Field(min_length=1, max_length=128)
    confirmation_token: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("reason")
    @classmethod
    def _reason_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


def resolution_plan(
    divergences: tuple[CustodyDivergence, ...],
) -> tuple[CustodyResolutionStep, ...]:
    """Ordered plan for the resolvable divergences: reconcile → baseline → clear-hold."""
    steps: list[CustodyResolutionStep] = []
    kinds = {d.kind for d in divergences if d.state == "resolvable_now"}
    if kinds & {"exposure_attribution_mismatch"}:
        steps.append(
            CustodyResolutionStep(action_id="reconcile_now", scope="account", mutates=False)
        )
        steps.append(
            CustodyResolutionStep(
                action_id="record_inventory_baseline", scope="account", mutates=True
            )
        )
    if "exposure_hold" in kinds:
        steps.append(
            CustodyResolutionStep(action_id="clear_hold", scope="account", mutates=True)
        )
    return tuple(steps)

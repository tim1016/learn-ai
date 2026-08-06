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
from app.broker.alpaca.clerk.models import EpochMs, OrderJournalEntry
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.engine.live.order_identity import order_ref_namespace_matches

CustodyDivergenceKind = Literal[
    "exposure_attribution_mismatch",
    "exposure_hold",
    "stale_reconciliation",
    "needs_review",
    "foreign_working_order",
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
    prerequisite_step: CustodyActionId | None = None
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
    observed_at_ms: EpochMs
    snapshot_version: str
    resolution_posture: Literal["paper", "live"] = "paper"
    resolvable: bool = False
    blocked_reason: str | None = None
    authority_kind: Literal["legacy", "sqlite"] = "legacy"
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
    "foreign_working_order": (
        "An order was submitted outside every namespace recorded by this Clerk.",
        "A manual or third-party broker client submitted directly to this account.",
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
    "foreign_working_order": (
        "The broker has a working order that this Clerk does not own. Cancel or "
        "settle it before changing the account custody baseline or clearing a hold."
    ),
}


def custody_snapshot_version(
    entries: list[OrderJournalEntry],
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
    *,
    namespaces: frozenset[str] = frozenset(),
    channel_fresh: bool = True,
    bot_running: bool = False,
) -> str:
    """Stable hash of the salient custody state (the resolve concurrency guard)."""
    latest_reconciliation = derive.latest_reconciliation(entries)
    payload = {
        "expected": exposure.project_expected_account_exposure(entries),
        "observed": {
            p.symbol.upper(): exposure.signed_broker_position_quantity(p)
            for p in positions
            if p.quantity != 0
        },
        "hold": derive.hold_state(entries).active,
        "freeze": derive.account_freeze_state(entries).category,
        "latest_reconciliation": (
            None
            if latest_reconciliation is None
            else {
                "verdict": latest_reconciliation.verdict,
                "recorded_at_ms": latest_reconciliation.recorded_at_ms,
            }
        ),
        "namespaces": sorted(namespaces),
        "channel_fresh": channel_fresh,
        "bot_running": bot_running,
        "unresolved_intents": sorted(
            entry.order_ref for entry in derive.unresolved_intents(entries)
        ),
        "working_orders": sorted(
            (o.order_id, o.client_order_id or "", o.status.lower())
            for o in orders
            if o.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def stale_reconciliation_divergence() -> CustodyDivergence:
    """The diagnosis-level short-circuit when the broker could not be read fresh."""
    return CustodyDivergence(
        kind="stale_reconciliation",
        state="resolvable_now",
        explanation=_EXPLANATION["stale_reconciliation"],
        possible_causes=_CAUSES["stale_reconciliation"],
        resolution_step="reconcile_now",
    )


def stale_custody_snapshot_version(entries: list[OrderJournalEntry]) -> str:
    """Snapshot hash for a diagnosis made without a fresh broker read.

    Structurally distinct from ``custody_snapshot_version``'s payload shape
    (that one always has ``observed``/``hold``/``working_orders`` keys; this
    one never does) so a stale snapshot version can never alias a real one —
    load-bearing for the `resolve_custody` 409 concurrency guard: if the
    broker becomes reachable again between diagnosis and resolve, the
    operator MUST be forced to re-confirm against the fresh (real) diagnosis,
    never silently proceed against a snapshot taken while the broker was
    down.
    """
    payload = {
        "stale_reconciliation": True,
        "expected": exposure.project_expected_account_exposure(entries),
        "hold": derive.hold_state(entries).active,
        "freeze": derive.account_freeze_state(entries).category,
        "unresolved_intents": sorted(
            entry.order_ref for entry in derive.unresolved_intents(entries)
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
    channel_fresh: bool = True,
    bot_running: bool = False,
) -> tuple[CustodyDivergence, ...]:
    """Project current Clerk↔broker divergences (pure; no mutation)."""
    divergences: list[CustodyDivergence] = []

    working = [
        order
        for order in orders
        if order.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
    ]
    foreign_working = [
        order
        for order in working
        if not order_ref_namespace_matches(order.client_order_id, namespaces)
    ]
    if foreign_working:
        divergences.append(
            CustodyDivergence(
                kind="foreign_working_order",
                state="blocked_on_prerequisite",
                explanation=_EXPLANATION["foreign_working_order"],
                possible_causes=_CAUSES["foreign_working_order"],
                prerequisite_detail=(
                    f"{len(foreign_working)} unowned working order(s) must be cancelled "
                    "or settled before custody can be resolved."
                ),
                evidence_refs=tuple(sorted(order.order_id for order in foreign_working)),
            )
        )

    unresolved = derive.unresolved_intents(entries)
    hold = derive.hold_state(entries)
    if hold.active:
        hold_prerequisites: list[str] = []
        if not channel_fresh:
            hold_prerequisites.append(
                "Restore both submission channels and reconcile before clearing the hold."
            )
        if foreign_working:
            hold_prerequisites.append(
                "Cancel or settle every unowned working order before clearing the hold."
            )
        if unresolved:
            hold_prerequisites.append(
                "Reconcile every unresolved submission before clearing the hold."
            )
        divergences.append(
            CustodyDivergence(
                kind="exposure_hold",
                state=("blocked_on_prerequisite" if hold_prerequisites else "resolvable_now"),
                explanation=_EXPLANATION["exposure_hold"],
                possible_causes=_CAUSES["exposure_hold"],
                resolution_step="clear_hold",
                prerequisite_detail=" ".join(hold_prerequisites) or None,
                evidence_refs=tuple(sorted(derive.unexplained_order_ids(entries))),
            )
        )

    deltas = _attribution_deltas(entries, positions)
    if deltas:
        state: CustodyDivergenceState = "resolvable_now"
        prerequisites: list[str] = []
        prerequisite_step: CustodyActionId | None = None
        if bot_running:
            prerequisites.append(
                "Stop every running bot before adopting an account inventory baseline."
            )
        if working:
            prerequisites.append(
                f"{len(working)} working order(s) are open. Cancel or settle them "
                "before adopting a baseline."
            )
        if unresolved:
            prerequisites.append(
                f"{len(unresolved)} unresolved submission(s) must reconcile before "
                "a baseline cutover."
            )
            prerequisite_step = "reconcile_now"
        if prerequisites:
            state = "blocked_on_prerequisite"
        divergences.append(
            CustodyDivergence(
                kind="exposure_attribution_mismatch",
                state=state,
                explanation=_EXPLANATION["exposure_attribution_mismatch"],
                possible_causes=_CAUSES["exposure_attribution_mismatch"],
                position_deltas=deltas,
                resolution_step="record_inventory_baseline",
                prerequisite_step=prerequisite_step,
                prerequisite_detail=" ".join(prerequisites) or None,
            )
        )

    if not deltas and unresolved:
        divergences.append(
            CustodyDivergence(
                kind="needs_review",
                state="needs_review",
                explanation=_EXPLANATION["needs_review"],
                possible_causes=_CAUSES["needs_review"],
                evidence_refs=tuple(sorted(e.order_ref for e in unresolved if e.order_ref)),
            )
        )

    freeze = derive.account_freeze_state(entries)
    if freeze.active and not any(
        divergence.resolution_step == "reconcile_now"
        or divergence.prerequisite_step == "reconcile_now"
        for divergence in divergences
    ):
        divergences.append(
            CustodyDivergence(
                kind="stale_reconciliation",
                state="resolvable_now",
                explanation=(
                    "The latest durable reconciliation still freezes account admission, "
                    "even though this read-only broker snapshot currently matches the Clerk."
                ),
                possible_causes=_CAUSES["stale_reconciliation"],
                resolution_step="reconcile_now",
                prerequisite_detail=freeze.next_step,
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
    recorded_at_ms: EpochMs
    steps_executed: tuple[CustodyResolutionStepResult, ...] = ()
    in_sync: bool = False
    remaining_divergences: tuple[CustodyDivergence, ...] = ()


class CustodySnapshotChangedError(Exception):
    """The custody snapshot changed since diagnosis; re-diagnose before resolving."""

    def __init__(self, message: str = "Account state changed since it was diagnosed.") -> None:
        super().__init__(message)
        self.detail = "Re-run the diagnosis and confirm the current state before resolving."


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


class CustodyConflictDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    why: str | None = None


class CustodyConflictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: CustodyConflictDetail


def resolution_plan(
    divergences: tuple[CustodyDivergence, ...],
) -> tuple[CustodyResolutionStep, ...]:
    """Ordered plan for the resolvable divergences: reconcile → baseline → clear-hold."""
    steps: list[CustodyResolutionStep] = []
    actionable = [d for d in divergences if d.state == "resolvable_now"]
    prerequisite_steps = {
        d.prerequisite_step for d in divergences if d.prerequisite_step is not None
    }
    kinds = {d.kind for d in actionable}
    if "reconcile_now" in prerequisite_steps or "stale_reconciliation" in kinds:
        steps.append(
            CustodyResolutionStep(action_id="reconcile_now", scope="account", mutates=False)
        )
    if kinds & {"exposure_attribution_mismatch"}:
        if not any(step.action_id == "reconcile_now" for step in steps):
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

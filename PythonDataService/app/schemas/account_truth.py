"""Wire models for the IBKR Account Truth projection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrTradeEvidence,
    OrderAction,
    OrderStatus,
    OrderType,
    SecType,
)

AccountTruthFinalVerdict = Literal["clean", "not_proven"]
AccountTruthSeverity = Literal["ok", "info", "warning", "critical"]
AccountTruthInvariantStatus = Literal["pass", "warn", "fail", "not_applicable"]
AccountTruthOwnerClass = Literal[
    "bot",
    "manual",
    "mixed_known",
    "foreign_or_unclaimed",
]
AccountTruthEvidenceTier = Literal[
    "bot_order_ref",
    "app_minted_manual",
    "adopted_manual",
    "mixed_known",
    "foreign_or_unclaimed",
]
AccountTruthOwnerBindingState = Literal["DEPLOYED", "ACTIVE", "RETIRED", "UNKNOWN"]
AccountTruthFactKind = Literal["open_order", "completed_order", "execution", "position"]
AccountTruthLifecycle = Literal["submitted", "acknowledged", "filled", "cancelled", "rejected", "limbo"]
AccountTruthOrderCancelReasonCode = Literal[
    "BROKER_NOT_PAPER_CONNECTED",
    "NOT_OPEN_ORDER",
    "FOREIGN_OR_UNCLAIMED",
    "ORDER_TERMINAL",
]


class AccountTruthMessage(BaseModel):
    """Backend-authored operator copy for blockers, caveats, and notes."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: AccountTruthSeverity
    title: str
    message: str
    forensic_facts: dict[str, JsonValue] = Field(default_factory=dict)


class AccountTruthInvariant(BaseModel):
    """One account-level proof row."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    status: AccountTruthInvariantStatus
    severity: AccountTruthSeverity
    headline: str
    narrative: str
    checked_at_ms: int
    evidence_count: int = Field(ge=0)


class AccountTruthOwnerSummary(BaseModel):
    """Roll-up by owner class and owner key."""

    model_config = ConfigDict(frozen=True)

    owner_class: AccountTruthOwnerClass
    owner_key: str
    owner_label: str
    evidence_tier: AccountTruthEvidenceTier
    evidence_label: str
    owner_binding_state: AccountTruthOwnerBindingState = "UNKNOWN"
    open_order_count: int = Field(ge=0)
    execution_count: int = Field(ge=0)
    position_count: int = Field(ge=0)
    gross_position_quantity: float = 0.0


class AccountTruthSymbolExposure(BaseModel):
    """Symbol × owner exposure row for Account Monitor."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    owner_class: AccountTruthOwnerClass
    owner_key: str
    owner_label: str
    quantity: float
    con_id: int | None = None


class AccountTruthFactOwner(BaseModel):
    """Owner classification shared by orders, executions, and positions."""

    model_config = ConfigDict(frozen=True)

    owner_class: AccountTruthOwnerClass
    owner_key: str
    owner_label: str
    evidence_tier: AccountTruthEvidenceTier
    evidence_label: str
    owner_binding_state: AccountTruthOwnerBindingState = "UNKNOWN"
    severity: AccountTruthSeverity


class AccountTruthOrderCancelAction(BaseModel):
    """Backend-authored cancel affordance for one broker order row."""

    model_config = ConfigDict(frozen=True)

    visible: bool
    enabled: bool
    reason_code: AccountTruthOrderCancelReasonCode | None = None
    label: str
    detail: str


class AccountTruthOrderRow(BaseModel):
    """Open or terminal broker order row grouped by broker lifecycle identity."""

    model_config = ConfigDict(frozen=True)

    fact_kind: Literal["open_order", "completed_order"]
    lifecycle_id: str
    lifecycle: AccountTruthLifecycle
    account_id: str
    order_id: int
    perm_id: int | None = None
    client_id: int
    con_id: int
    symbol: str
    sec_type: SecType
    action: OrderAction
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    status: OrderStatus
    cumulative_filled: float
    remaining: float
    avg_fill_price: float | None = None
    order_ref: str | None = None
    owner: AccountTruthFactOwner
    cancel_action: AccountTruthOrderCancelAction
    headline: str
    detail: str
    fetched_at_ms: int
    ibkr_evidence: IbkrTradeEvidence | None = None


class AccountTruthExecutionRow(BaseModel):
    """Execution row deduped by IBKR execId."""

    model_config = ConfigDict(frozen=True)

    fact_kind: Literal["execution"] = "execution"
    account_id: str
    exec_id: str
    order_id: int
    perm_id: int | None = None
    client_id: int | None = None
    con_id: int | None = None
    symbol: str | None = None
    side: OrderAction | None = None
    order_type: OrderType | None = None
    quantity: float | None = None
    price: float | None = None
    fee: float | None = None
    exec_time_ms: int | None = None
    observed_at_ms: int
    order_ref: str | None = None
    owner: AccountTruthFactOwner
    headline: str
    detail: str
    ibkr_evidence: IbkrTradeEvidence | None = None


class AccountTruthPositionRow(BaseModel):
    """Current position row with conservative ownership attribution."""

    model_config = ConfigDict(frozen=True)

    fact_kind: Literal["position"] = "position"
    account_id: str
    con_id: int
    symbol: str
    sec_type: SecType
    quantity: float
    avg_cost: float
    market_value: float | None = None
    owner: AccountTruthFactOwner
    headline: str
    detail: str
    fetched_at_ms: int


class AccountTruthEvidenceGap(BaseModel):
    """A broker source the projection could not collect."""

    model_config = ConfigDict(frozen=True)

    source: str
    severity: AccountTruthSeverity
    message: str


class AccountTruthResponse(BaseModel):
    """Backend-authored account-wide truth projection."""

    model_config = ConfigDict(frozen=True)

    account_id: str | None = None
    final_verdict: AccountTruthFinalVerdict
    final_severity: AccountTruthSeverity
    status_label: str
    status_detail: str
    generated_at_ms: int
    health: IbkrConnectionHealth
    account: IbkrAccountSummary | None = None
    known_bot_namespaces: list[str] = Field(default_factory=list)
    manual_namespaces_observed: list[str] = Field(default_factory=list)
    invariants: list[AccountTruthInvariant]
    blockers: list[AccountTruthMessage] = Field(default_factory=list)
    caveats: list[AccountTruthMessage] = Field(default_factory=list)
    owner_summaries: list[AccountTruthOwnerSummary] = Field(default_factory=list)
    symbol_exposures: list[AccountTruthSymbolExposure] = Field(default_factory=list)
    orders: list[AccountTruthOrderRow] = Field(default_factory=list)
    executions: list[AccountTruthExecutionRow] = Field(default_factory=list)
    positions: list[AccountTruthPositionRow] = Field(default_factory=list)
    evidence_gaps: list[AccountTruthEvidenceGap] = Field(default_factory=list)

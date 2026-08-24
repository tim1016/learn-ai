"""Request/response schemas for the broker-parameterized bot runner routes.

``/api/brokers/{broker}/bots/...`` (Alpaca Bot Control v2, S2 — #1260).
Views are projections of the durable lifecycle artifacts; the router never
derives state that is not artifact- or registry-backed.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.schemas.action_plan import ActionPlan
from app.schemas.bot_run_evidence import BotRunTerminalOutcomeView
from app.schemas.live_runs import BotDutyOutcomeView
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.strategy_params_schema import StrategyParamsSchema


def _validated_strategy_instance_id(value: str) -> str:
    from app.engine.live.identity import validate_strategy_instance_id

    return validate_strategy_instance_id(value)


def _validated_catalog_strategy_key(value: str) -> str:
    """Reject a strategy key the canonical registry does not define (#1703).

    Replaces the closed ``AlpacaPaperStrategyKey`` enum: any registered,
    catalog-visible registry key is accepted at the wire boundary — this is
    the "definition" facet check only. Whether the key is actually
    *selectable* (validated, runtime-backed, proof current) is a deploy-time
    admission decision made downstream against the composed catalog, not a
    422 at parse time.
    """
    from app.engine.strategy.registry import _STRATEGY_REGISTRY

    registration = _STRATEGY_REGISTRY.get(value)
    if registration is None or not registration.catalog_visible:
        raise ValueError(f"Unknown strategy key '{value}'.")
    return value


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


class BotProcessFact(BaseModel):
    """Process-registry observation for one strategy run.

    This fact reports only process presence. It never implies broker custody,
    exposure, order state, or permission to trade.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    run_id: str
    process_identity: str | None
    state: Literal["RUNNING", "STOPPING", "EXITED", "UNKNOWN"]
    registry_generation: str
    observed_at_ms: int = Field(ge=0)


class BotControlAuthorityFacts(BaseModel):
    """Independent process and Clerk facts for one bot control decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process: BotProcessFact
    clerk: ClerkCustodySnapshot


def _normalized_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if _SYMBOL_RE.fullmatch(normalized) is None:
        raise ValueError("symbol must start with a letter and contain only letters, numbers, periods, or hyphens")
    return normalized


class DeployBotRequest(BaseModel):
    """Deploy (and start) a bot bound to ``{broker}``."""

    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=12)
    use_rth: bool = True
    mode: Literal["log_only", "dry_run", "trade"] = "log_only"
    quantity: int = Field(default=1, ge=1, le=100)

    @field_validator("strategy_instance_id")
    @classmethod
    def _validate_strategy_instance_id(cls, value: str) -> str:
        # Canonical path-segment validation — same rule every artifact path
        # builder enforces, applied at the API boundary so a bad id fails as
        # 422 instead of a 500 from a path builder.
        return _validated_strategy_instance_id(value)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)


class AlpacaPaperSizingSelection(BaseModel):
    """One closed sizing choice for the Alpaca paper canary workflow."""

    model_config = ConfigDict(extra="forbid")

    preset: Literal["safe_canary", "custom"] = "safe_canary"
    quantity: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _safe_canary_is_one_share(self) -> AlpacaPaperSizingSelection:
        if self.preset == "safe_canary" and self.quantity != 1:
            raise ValueError("safe_canary sizing is fixed at exactly one share")
        return self


class AlpacaPaperEvidenceOverride(BaseModel):
    """Explicit operator acceptance of an evidence-only deployment risk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    acknowledgement: Literal["I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _require_substantive_reason(cls, value: str) -> str:
        reason = value.strip()
        if len(reason) < 10:
            raise ValueError("evidence override reason must contain at least 10 characters")
        if len(reason) > 500:
            raise ValueError("evidence override reason must contain at most 500 characters")
        return reason


class AlpacaPaperDeployRequest(BaseModel):
    """Closed account-scoped command for the production Alpaca deploy page."""

    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    # #1703: was the closed `AlpacaPaperStrategyKey` enum. Any registry-
    # defined, catalog-visible key is now accepted at the wire boundary —
    # see `_validated_catalog_strategy_key` for exactly what "defined" means.
    strategy_key: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=12)
    sizing: AlpacaPaperSizingSelection = Field(default_factory=AlpacaPaperSizingSelection)
    execution_mode: Literal["paper", "dry_run"] = "paper"
    carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID"
    evidence_override: AlpacaPaperEvidenceOverride | None = None
    # Every tunable the strategy author exposed (EMA gap, RSI range, ADX
    # thresholds, indicator periods, bar resolution, ...), validated by the
    # strategy's own registered param_schema — the same schema Engine Lab and
    # Strategy Lab already use. Never contains `symbol`: the deploy request's
    # own `symbol` field above is authoritative and is injected separately.
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("strategy_instance_id")
    @classmethod
    def _validate_strategy_instance_id(cls, value: str) -> str:
        # Path safety first (shared, <=128), then the tighter broker-ownership
        # cap: every order this bot ever submits carries
        # ``learn-ai/{sid}/v1:{intent_id}`` (35 fixed chars) under the
        # ``order_ref`` cap, so a name that cannot fit must be refused HERE —
        # at first order it is an OrderRefTooLongError crash instead
        # (ceremony-spy-strategy-c-0824, 2026-08-24). Read models keep the
        # loose validator: existing long-named bots must stay readable.
        from app.engine.live.order_identity import validate_broker_owned_instance_id

        return validate_broker_owned_instance_id(_validated_strategy_instance_id(value))

    @field_validator("strategy_key")
    @classmethod
    def _validate_strategy_key(cls, value: str) -> str:
        return _validated_catalog_strategy_key(value)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @model_validator(mode="after")
    def _dry_run_cannot_carry_broker_exposure(self) -> AlpacaPaperDeployRequest:
        if self.execution_mode == "dry_run" and self.carryover_policy == "ALLOW":
            raise ValueError("dry_run requires carryover_policy=FORBID")
        return self


class AlpacaPaperDeployEligibility(BaseModel):
    """Backend-authored launch verdict rendered verbatim by Angular."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: bool
    reason_code: str
    headline: str
    explanation: str
    next_action: str


class AlpacaPaperDeployStrategy(BaseModel):
    """Trader-facing option for one accepted, evidence-only, or blocked strategy.

    A validated strategy whose recorded proof no longer re-verifies is
    demoted to ``evidence_status="blocked"`` rather than removed from the
    row set: the operator's validation flag always guarantees a row.
    ``selectable`` is the server's own Paper-launchability fact — a blocked
    row is always present but never Paper-selectable.

    A validated strategy with no registered runtime (#1703) is composed the
    same way — visible, ``evidence_status="blocked"``, ``selectable=False``
    — with a ``blocked_explanation`` naming the missing runtime rather than
    a stale proof, so "not built yet" reads differently from "not
    validated". It is the one case that also admits neither execution mode;
    see ``admissible_modes`` below.

    ``admissible_modes`` (#1702) is the mode-explicit fact the deploy form
    actually needs: gates are tiered by execution mode, so a row can be
    Dry-Run-admissible without being Paper-selectable. A runtime-backed row
    always admits ``"dry_run"``; ``"paper"`` is present iff ``selectable``
    is ``True`` — the two facts are kept in sync by the invariant below, not
    merged into one, because ``selectable`` already has a settled meaning
    ("is this row currently Paper-admissible") that this change does not
    repurpose. A no-runtime row admits neither mode (Dry Run itself
    requires a registered runtime).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_key: str
    label: str
    explanation: str
    validation_case_symbol: str
    evidence_status: Literal["accepted", "evidence_only", "blocked"]
    # Account-scoped approval state for sealed Signal Programs. This is
    # explicit wire data so the UI never has to infer an available action by
    # parsing backend-authored blocker prose.
    paper_access_state: Literal["not_required", "blocked", "available", "enabled"]
    selectable: bool
    admissible_modes: tuple[Literal["dry_run", "paper"], ...]
    override_explanation: str | None = None
    blocked_explanation: str | None = None
    # This strategy's registered tunables as JSON schema — the same schema
    # Engine Lab and Strategy Lab already render (`GET /api/engine/strategies`).
    # `symbol` is never present: it is deploy-authoritative, carried on the
    # request's own `symbol` field instead of as a tunable. Typed (not
    # `dict[str, Any]`) so the OpenAPI-generated frontend type is real,
    # not the codegen's blanket `Record<string, never>` for untyped dicts.
    params_schema: StrategyParamsSchema = Field(default_factory=StrategyParamsSchema)

    @model_validator(mode="after")
    def _evidence_status_invariants(self) -> AlpacaPaperDeployStrategy:
        if self.evidence_status == "blocked":
            if self.selectable:
                raise ValueError("A blocked strategy row cannot be selectable.")
            if self.blocked_explanation is None:
                raise ValueError("A blocked strategy row must carry a blocked_explanation.")
            if self.override_explanation is not None:
                raise ValueError("A blocked strategy row cannot carry an override_explanation.")
            return self
        if not self.selectable and self.paper_access_state != "available":
            raise ValueError(f"A {self.evidence_status} strategy row must be selectable.")
        if self.selectable and self.blocked_explanation is not None:
            raise ValueError(f"A {self.evidence_status} strategy row cannot carry a blocked_explanation.")
        if self.paper_access_state == "available" and self.blocked_explanation is None:
            raise ValueError("An available Paper-access row must explain that approval is still required.")
        if self.evidence_status == "evidence_only" and self.override_explanation is None:
            raise ValueError("An evidence_only strategy row must carry an override_explanation.")
        if self.evidence_status == "accepted" and self.override_explanation is not None:
            raise ValueError("An accepted strategy row cannot carry an override_explanation.")
        return self

    @model_validator(mode="after")
    def _admissible_modes_invariants(self) -> AlpacaPaperDeployStrategy:
        # A selectable row is always runtime-backed and admits exactly both
        # modes. A non-selectable row is either runtime-backed but blocked
        # (stale proof, gating divergence, ...) — Dry Run ignores validation
        # and behavioral evidence entirely, so it stays dry_run-admissible —
        # or has no registered runtime at all (#1703), in which case it
        # admits neither mode: Dry Run itself requires a registered runtime
        # (see `_dry_run_eligibility`'s runtime check). This relaxation only
        # widens what a *non-selectable* row may look like; a selectable
        # row's guarantee (always both modes) is unchanged from #1702.
        if self.selectable:
            if self.admissible_modes != ("dry_run", "paper"):
                raise ValueError("A selectable strategy row must admit exactly dry_run and paper.")
            return self
        if self.admissible_modes not in ((), ("dry_run",)):
            raise ValueError("A non-selectable strategy row may admit only dry_run, or neither mode.")
        return self


class AlpacaPaperDeployReadinessCheck(BaseModel):
    """One production-backed predicate in the current Deploy admission decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str
    label: str
    ready: bool
    scope: Literal["strategy", "account", "broker"]
    authority: str
    headline: str
    explanation: str
    evidence_summary: str
    evidence: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    recovery: str | None


class AlpacaPaperExecutionMode(BaseModel):
    """Broker-authored execution capability for the shared Deploy page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["paper", "dry_run", "live"]
    label: str
    availability: Literal["available", "planned"]
    explanation: str


class AlpacaPaperSizingOption(BaseModel):
    """Backend-authored sizing choice and its bounded quantity contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preset: Literal["safe_canary", "custom"]
    label: str
    explanation: str
    min_quantity: int = Field(ge=1)
    max_quantity: int = Field(ge=1)
    default_quantity: int = Field(ge=1)


class AlpacaPaperDeployView(BaseModel):
    """All semantics needed to render the Alpaca paper deploy workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker: Literal["alpaca"]
    account_id: str
    account_mode: Literal["paper"]
    account_label: str
    evaluated_at_ms: int = Field(ge=0)
    # Paper eligibility (name kept as-is: renaming would ripple across the
    # existing test suite for no behavioral gain). `dry_run_eligibility`
    # (#1702) is the parallel, deliberately narrower verdict for the Dry Run
    # tier — see `_dry_run_eligibility` for exactly which gates it omits.
    eligibility: AlpacaPaperDeployEligibility
    dry_run_eligibility: AlpacaPaperDeployEligibility
    readiness_checks: tuple[AlpacaPaperDeployReadinessCheck, ...]
    execution_modes: tuple[AlpacaPaperExecutionMode, ...]
    strategies: tuple[AlpacaPaperDeployStrategy, ...]
    sizing_options: tuple[AlpacaPaperSizingOption, ...]
    action_plan_explanation: str
    carryover_available: bool
    carryover_label: str
    carryover_explanation: str
    allowed_actions: tuple[Literal["deploy"], ...]


class StopBotRequest(BaseModel):
    """Button-Rule exit: stop a running bot (durable desired-state first)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=256)


class BotStatusView(BaseModel):
    """One bot's roster row: broker-tagged binding + artifact-derived state."""

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    strategy_key: str = "deployment_validation"
    # The active SQLite roster sources this immutable operator-facing label
    # from ``bot_config.display_name``. Legacy process-backed rows have no
    # durable display-name record yet, so only they may leave it absent.
    strategy_label: str | None = None
    broker: str
    symbol: str
    mode: Literal["log_only", "dry_run", "trade"]
    quantity: int | None
    carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID"
    evidence_override: AlpacaPaperEvidenceOverride | None = None
    carryover_account_policy_enabled: bool = False
    carryover_checkpoint_exposure: dict[str, float] = Field(default_factory=dict)
    carryover_checkpoint_config_matches: bool = False
    running: bool
    phase: Literal["OFF_DUTY", "ON_DUTY", "RETIRED"]
    desired_state: Literal["RUNNING", "PAUSED", "STOPPED"]
    active_run_id: str | None
    duty_outcome: BotDutyOutcomeView | None
    binding_created_at_ms: int
    last_transition_at_ms: int | None


class BotRunView(BaseModel):
    """Read-only launch and terminal evidence for one strategy run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    run_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    launch_reason: Literal["deploy", "resume", "legacy"]
    started_at_ms: int = Field(ge=0)
    is_current: bool
    process: BotProcessFact | None
    terminal_outcome: BotRunTerminalOutcomeView | None


class BotRunHistoryPage(BaseModel):
    """One bounded page of previous runs; current run has its own endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[BotRunView, ...]
    next_cursor: str | None


class BotRunReadBrokerErrorDetail(BaseModel):
    """Broker-registry failure detail returned by a bot-run read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker: str
    message: str
    why: str | None


class BotRunReadRunnerErrorDetail(BaseModel):
    """Runner failure detail returned by a bot-run read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str
    why: str | None
    admission: RunAdmissionDecision | None


class BotRunReadNotFoundResponse(BaseModel):
    """404 envelope for an unknown broker or strategy-instance run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: BotRunReadBrokerErrorDetail | BotRunReadRunnerErrorDetail


class BotRunReadRunnerErrorResponse(BaseModel):
    """422 envelope emitted by the bot runner for an invalid run read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: BotRunReadRunnerErrorDetail


class BotRunReadValidationIssue(BaseModel):
    """One FastAPI request-validation issue for a run-history query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loc: tuple[str | int, ...]
    msg: str
    type: str


class BotRunHistoryUnprocessableResponse(BaseModel):
    """422 envelope for a runner error or an invalid history query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: BotRunReadRunnerErrorDetail | tuple[BotRunReadValidationIssue, ...]


class AlpacaPaperDeployReceipt(BaseModel):
    """Backend-authored terminal receipt for one accepted deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["deployed"]
    outcome: Literal["success"] = "success"
    receipt_id: str
    recorded_at_ms: int = Field(ge=0)
    message: str
    explanation: str
    next_action: str
    panel_path: str
    account_id: str
    execution_mode: Literal["paper", "dry_run"] = "paper"
    sizing: AlpacaPaperSizingSelection
    carryover_policy: Literal["FORBID", "ALLOW"]
    evidence_override: AlpacaPaperEvidenceOverride | None = None
    action_plan: ActionPlan
    admission: RunAdmissionDecision
    bot: BotStatusView
    # The full resolved parameter set bound to this immutable instance
    # (registered defaults merged with the request's overrides). Informational
    # only — `parameters_diverge_from_defaults` names the fields that differ
    # from the strategy's registered validated defaults; neither ever gates.
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameters_diverge_from_defaults: tuple[str, ...] = ()

"""Backend-authored OperatorBlocker contract.

This is the shared atom for deploy-preflight and bot-control blockers. The
disposition/move invariant prevents a blocker from rendering without an honest
recovery move.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Disposition = Literal["fix_here", "fix_elsewhere", "wait", "terminal"]
OperatorHost = Literal[
    "bot_cockpit",
    "deploy_preflight",
    "fleet_roster",
    "account_monitor",
    "account_desk",
]
ConditionScope = Literal["bot", "account", "broker", "fleet", "host", "strategy"]
OperatorBlockerAnchorKind = Literal[
    "surface",
    "verdict",
    "lease",
    "clerk",
    "reconciliation",
    "holdings_row",
    "event",
    "cure_tools",
]
OperatorBlockerAudience = Literal["trader", "operator", "both"]

_SUBJECT_KEY_ANCHOR_KINDS: frozenset[OperatorBlockerAnchorKind] = frozenset(
    {"holdings_row", "event"}
)


class OperatorBlockerAnchor(BaseModel):
    """Semantic Account-desk location for one blocker projection.

    ``subject_key`` is an opaque routing token. It is never display text and
    must therefore pass through unchanged to the host that owns the anchor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: OperatorBlockerAnchorKind
    subject_key: str | None

    @model_validator(mode="after")
    def _subject_key_matches_anchor_kind(self) -> OperatorBlockerAnchor:
        if self.kind in _SUBJECT_KEY_ANCHOR_KINDS and not self.subject_key:
            raise ValueError(f"{self.kind} anchor requires a subject_key")
        if self.kind not in _SUBJECT_KEY_ANCHOR_KINDS and self.subject_key is not None:
            raise ValueError(f"{self.kind} anchor must not carry a subject_key")
        return self


SURFACE_ANCHOR = OperatorBlockerAnchor(kind="surface", subject_key=None)


class NavigateAction(BaseModel):
    """Move: navigate to another operator page."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["navigate"]
    route: str
    fragment: str | None = None


class ConfirmInFormAction(BaseModel):
    """Move: resolve inline on the current form."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirm_in_form"]
    anchor: str


class OpenRunbookAction(BaseModel):
    """Move: open an operator runbook by backend-authored slug."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["open_runbook"]
    slug: str


class RetireReplaceAction(BaseModel):
    """Move: retire this bot and start a fresh deploy flow with lineage kept."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["retire_replace"]


class RemoveAction(BaseModel):
    """Move: soft-delete this bot from the operator catalog."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["remove"]


OperatorAction = Annotated[
    NavigateAction | ConfirmInFormAction | OpenRunbookAction | RetireReplaceAction | RemoveAction,
    Field(discriminator="kind"),
]


class OperatorConfirmationCopy(BaseModel):
    """Backend-authored copy for dangerous operator confirmations."""

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str
    consequence: str
    confirm_label: str
    required_token: str = ""


class OperatorMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    action: OperatorAction
    target: str | None = None
    confirmation: OperatorConfirmationCopy | None = None


class OperatorCondition(BaseModel):
    """Surface-neutral identity authored once from evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Literal["blocking", "warning"]
    scope: ConditionScope
    evidence: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class OperatorBlocker(BaseModel):
    """Host-scoped, backend-authored guidance for one operator condition.

    Audience is presentational routing and confers no permission. Frontends
    render this backend-authored guidance and must never infer a cure from a
    reason code.
    """

    model_config = ConfigDict(extra="forbid")

    condition: OperatorCondition
    host: OperatorHost
    anchor: OperatorBlockerAnchor
    audience: OperatorBlockerAudience
    disposition: Disposition
    headline: str
    detail: str | None = None
    primary_move: OperatorMove | None = None
    secondary_moves: list[OperatorMove] = Field(default_factory=list)
    applies_to: Literal["deploy", "run", "both"]

    @classmethod
    def for_host(
        cls,
        *,
        condition_id: str,
        scope: ConditionScope,
        host: OperatorHost,
        anchor: OperatorBlockerAnchor,
        audience: OperatorBlockerAudience,
        disposition: Disposition,
        headline: str,
        detail: str | None,
        applies_to: Literal["deploy", "run", "both"],
        primary_move: OperatorMove | None = None,
        secondary_moves: list[OperatorMove] | None = None,
        severity: Literal["blocking", "warning"] = "blocking",
        evidence: dict[str, str | int | float | bool | None] | None = None,
    ) -> OperatorBlocker:
        return cls(
            condition=OperatorCondition(
                id=condition_id,
                severity=severity,
                scope=scope,
                evidence=evidence or {},
            ),
            host=host,
            anchor=anchor,
            audience=audience,
            disposition=disposition,
            headline=headline,
            detail=detail,
            primary_move=primary_move,
            secondary_moves=secondary_moves or [],
            applies_to=applies_to,
        )

    @model_validator(mode="after")
    def _disposition_move_pairing(self) -> OperatorBlocker:
        if self.disposition in {"fix_here", "fix_elsewhere"} and self.primary_move is None:
            raise ValueError(f"{self.disposition} blocker requires a primary_move")
        if self.disposition == "wait" and self.primary_move is not None:
            raise ValueError("wait blocker must not carry a primary_move")
        if self.disposition == "terminal" and self.primary_move is None and not self.secondary_moves:
            raise ValueError("terminal blocker requires at least one move")
        return self


class DeployPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    blockers: list[OperatorBlocker]

"""Backend-authored OperatorBlocker contract.

This is the shared atom for deploy-preflight now and bot-control blockers in a
later slice. The disposition/move invariant prevents a blocker from rendering
without an honest recovery move.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Disposition = Literal["fix_here", "fix_elsewhere", "wait", "terminal"]


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


OperatorAction = Annotated[
    NavigateAction | ConfirmInFormAction,
    Field(discriminator="kind"),
]


class OperatorMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    action: OperatorAction
    target: str | None = None


class OperatorBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Literal["blocking", "warning"]
    disposition: Disposition
    headline: str
    detail: str | None = None
    primary_move: OperatorMove | None = None
    secondary_moves: list[OperatorMove] = Field(default_factory=list)
    applies_to: Literal["deploy", "run", "both"]

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

"""Closed read-contract values for account-authority projection aggregation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

AuthorityKind = Literal["real_paper", "synthetic"]


class AuthorityScopedRow(BaseModel):
    """A projection row that identifies the account authority that authored it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    authority_kind: AuthorityKind


class SingleAuthorityAggregate(BaseModel):
    """An aggregate whose inputs must originate from one exact authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    authority_kind: AuthorityKind
    rows: tuple[AuthorityScopedRow, ...]

    @model_validator(mode="after")
    def rows_match_selected_authority(self) -> SingleAuthorityAggregate:
        if any(
            row.account_id != self.account_id or row.authority_kind != self.authority_kind
            for row in self.rows
        ):
            raise ValueError("aggregate inputs must belong to one exact account authority")
        return self


__all__ = ["AuthorityKind", "AuthorityScopedRow", "SingleAuthorityAggregate"]

"""Closed read-contract values for account-authority projection aggregation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

AuthorityKind = Literal["real_paper", "synthetic"]


def _validate_account_authority(account_id: str, authority_kind: AuthorityKind) -> None:
    """Keep account namespace and authority kind inseparable at the wire boundary."""
    is_synthetic_account = account_id.startswith("sim:")
    if is_synthetic_account and authority_kind != "synthetic":
        raise ValueError("sim: account ids require synthetic authority")
    if not is_synthetic_account and authority_kind != "real_paper":
        raise ValueError("real-paper account ids require real_paper authority")


class AuthorityScopedRow(BaseModel):
    """A projection row that identifies the account authority that authored it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    authority_kind: AuthorityKind

    @model_validator(mode="after")
    def account_namespace_matches_authority_kind(self) -> AuthorityScopedRow:
        _validate_account_authority(self.account_id, self.authority_kind)
        return self


class SingleAuthorityAggregate(BaseModel):
    """An aggregate whose inputs must originate from one exact authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    authority_kind: AuthorityKind
    rows: tuple[AuthorityScopedRow, ...]

    @model_validator(mode="after")
    def rows_match_selected_authority(self) -> SingleAuthorityAggregate:
        _validate_account_authority(self.account_id, self.authority_kind)
        if any(
            row.account_id != self.account_id or row.authority_kind != self.authority_kind
            for row in self.rows
        ):
            raise ValueError("aggregate inputs must belong to one exact account authority")
        return self


__all__ = ["AuthorityKind", "AuthorityScopedRow", "SingleAuthorityAggregate"]

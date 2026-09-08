"""Closed read-contract values for account-authority projection aggregation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

AuthorityKind = Literal["real_paper", "real_live", "shadow", "synthetic"]

# The two worlds whose fills are synthesized; every panel row they author is
# `simulated`.
SIMULATED_AUTHORITY_KINDS: frozenset[AuthorityKind] = frozenset({"synthetic", "shadow"})

# The worlds a *primary* authority can custody in — the subset of
# `AuthorityKind` a boot can select for the account a binding files its
# evidence against. Narrower than `AuthorityKind` on both ends: the isolated
# `synthetic` world is never a primary selection, and `real_live` is not a
# custody world until the arming ceremony lands (ADR 0059 slice 7).
CustodyWorld = Literal["real_paper", "shadow"]


def _validate_account_authority(account_id: str, authority_kind: AuthorityKind) -> None:
    """Keep account namespace and authority kind inseparable at the wire boundary."""
    if account_id.startswith("sim:"):
        if authority_kind != "synthetic":
            raise ValueError("sim: account ids require synthetic authority")
        if account_id == "sim:":
            raise ValueError("sim: account identity must name one account")
        return
    if account_id.startswith("shadow:"):
        if authority_kind != "shadow":
            raise ValueError("shadow: account ids require shadow authority")
        if account_id == "shadow:":
            raise ValueError("shadow: account identity must name one account")
        return
    if authority_kind not in ("real_paper", "real_live"):
        raise ValueError("real account ids require real_paper or real_live authority")


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


__all__ = [
    "SIMULATED_AUTHORITY_KINDS",
    "AuthorityKind",
    "AuthorityScopedRow",
    "CustodyWorld",
    "SingleAuthorityAggregate",
]

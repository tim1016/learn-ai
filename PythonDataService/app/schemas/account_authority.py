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
# evidence against. The isolated `synthetic` world is never a primary
# selection. `real_live` joined in ADR 0059 slice 7.
CustodyWorld = Literal["real_paper", "shadow", "real_live"]

# The one account mode each world may custody (ADR 0059 D1, slice 7 R8 #13).
# The shadow world reads the real-money account it was activated for, so it
# admits only `live`; the real-live world custodies that same account for
# real. Written once, here, so no gate re-derives it.
_MODE_ADMITTED_BY_WORLD: dict[CustodyWorld, Literal["paper", "live"]] = {
    "real_paper": "paper",
    "shadow": "live",
    "real_live": "live",
}


def admitted_account_mode_for_world(custody_world: CustodyWorld) -> Literal["paper", "live"]:
    """The one account mode ``custody_world`` may custody.

    The public half of the same closed table :func:`world_admits_account_mode`
    judges against, so a surface that must *name* the admitted mode -- an
    operator refusal, an authority-kind derivation -- reads it here instead of
    re-deriving a second world-to-mode rule that can disagree.
    """
    return _MODE_ADMITTED_BY_WORLD[custody_world]


def world_admits_account_mode(world: CustodyWorld, account_mode: str | None) -> bool:
    """Whether ``world`` may custody an account the broker reports in ``account_mode``.

    ``None`` — no observation — admits nothing: an unobserved account is not
    a paper account and not a live one.
    """
    return account_mode is not None and _MODE_ADMITTED_BY_WORLD[world] == account_mode


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


def account_authority_agrees(
    account_id: str | None, authority_kind: AuthorityKind | None
) -> bool:
    """Whether the id's namespace and the kind name the same world.

    The predicate form of :func:`_validate_account_authority`, for callers
    that branch on the pairing instead of refusing at a wire boundary — so
    the namespace-to-kind table stays written exactly once.
    """
    if account_id is None or authority_kind is None:
        return False
    try:
        _validate_account_authority(account_id, authority_kind)
    except ValueError:
        return False
    return True


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
    "account_authority_agrees",
    "admitted_account_mode_for_world",
    "world_admits_account_mode",
]

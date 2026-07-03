"""Account recovery state shared by Account Truth and broker actions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountFreezeEvidence,
    read_account_freeze,
)

AccountRecoveryStatus = Literal["clear", "frozen", "unreadable"]


@dataclass(frozen=True)
class AccountRecoveryState:
    status: AccountRecoveryStatus
    account_id: str | None
    freeze: AccountFreezeEvidence | None = None
    unreadable_error: str | None = None

    @staticmethod
    def clear(account_id: str | None = None) -> AccountRecoveryState:
        return AccountRecoveryState(status="clear", account_id=account_id)

    @staticmethod
    def frozen(freeze: AccountFreezeEvidence) -> AccountRecoveryState:
        return AccountRecoveryState(
            status="frozen",
            account_id=freeze.account_id,
            freeze=freeze,
        )

    @staticmethod
    def unreadable(account_id: str, exc: Exception) -> AccountRecoveryState:
        return AccountRecoveryState(
            status="unreadable",
            account_id=account_id,
            unreadable_error=str(exc),
        )


def read_account_recovery_state(
    *,
    artifacts_root: Path,
    account_id: str | None,
) -> AccountRecoveryState:
    if account_id is None:
        return AccountRecoveryState.clear()
    try:
        freeze = read_account_freeze(artifacts_root, account_id)
    except (AccountArtifactError, OSError, ValueError) as exc:
        return AccountRecoveryState.unreadable(account_id, exc)
    if freeze is None:
        return AccountRecoveryState.clear(account_id)
    return AccountRecoveryState.frozen(freeze)


__all__ = [
    "AccountRecoveryState",
    "AccountRecoveryStatus",
    "read_account_recovery_state",
]

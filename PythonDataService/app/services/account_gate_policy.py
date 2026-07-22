"""Mutation facade for account-level gate policy and promotion evidence."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.account_session_policy import (
    AccountSessionPolicy,
    write_account_session_policy,
)
from app.services.account_gate_promotion import (
    ClerkRestartSmokeEvidence,
    record_clerk_restart_smoke,
)
from app.utils.timestamps import now_ms_utc


class AccountGatePolicyService:
    """Keep account-gate mutations out of transport routers."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def update_session_policy(
        self,
        *,
        account_id: str,
        allow_outside_live_session: bool,
    ) -> AccountSessionPolicy:
        """Persist the account's explicit outside-session decision."""

        return write_account_session_policy(
            self._artifacts_root,
            account_id=account_id,
            allow_outside_live_session=allow_outside_live_session,
            updated_at_ms=now_ms_utc(),
        )

    def record_restart_smoke(
        self,
        *,
        account_id: str,
        confirmation: str,
    ) -> ClerkRestartSmokeEvidence:
        """Record the typed current-Clerk smoke after an operator restart check."""

        return record_clerk_restart_smoke(
            self._artifacts_root,
            account_id=account_id,
            confirmation=confirmation,
            recorded_at_ms=now_ms_utc(),
        )


__all__ = ["AccountGatePolicyService"]

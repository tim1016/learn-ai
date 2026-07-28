"""Shared construction of account safety reads outside the account router."""

from __future__ import annotations

from pathlib import Path

from app.broker.ibkr.client import NotConnectedError, _is_paper_account, get_client
from app.broker.ibkr.config import get_settings
from app.services.account_directory import AccountDirectoryService, CurrentBrokerAccount
from app.services.account_safety_snapshot import AccountSafetySnapshotService


def current_broker_account() -> CurrentBrokerAccount | None:
    """Return the connected account without turning a disconnected broker into proof."""

    try:
        client = get_client()
    except NotConnectedError:
        return None
    if not client.is_connected() or client.connected_account is None:
        return None
    return CurrentBrokerAccount(
        account_id=client.connected_account,
        is_paper=_is_paper_account(client.connected_account),
    )


def account_safety_snapshot_service(*, artifacts_root: Path) -> AccountSafetySnapshotService:
    """Build the read-only safety composition for any HTTP boundary."""

    return AccountSafetySnapshotService(
        artifacts_root=artifacts_root,
        directory=AccountDirectoryService(
            artifacts_root=artifacts_root,
            current_account=current_broker_account(),
            requested_account_gate_authority=get_settings().account_gate_authority,
        ),
    )

"""One deployment topology for the supported account-custody fleet.

The deterministic qualification campaign and the Clerk host must use this same
configuration.  Keeping it in one module prevents a passing laboratory run
from describing a fleet shape that the production host cannot admit.
"""

from __future__ import annotations

from app.engine.live.account_clerk_journal_models import AccountClerkAsyncCustodyConfig

SUPPORTED_ACCOUNT_CUSTODY_FLEET_SIZE = 8
SUPPORTED_ACCOUNT_CUSTODY_ENTRY_CAPACITY = 8
SUPPORTED_ACCOUNT_CUSTODY_RISK_REDUCING_CAPACITY = 0


def supported_account_custody_config() -> AccountClerkAsyncCustodyConfig:
    """Return the sole Clerk configuration qualified for deployment.

    The broker exposes no externally reachable atomic reduction primitive yet,
    so the production topology fails closed instead of reserving an unproven
    asynchronous lane.
    """

    return AccountClerkAsyncCustodyConfig(
        entry_capacity=SUPPORTED_ACCOUNT_CUSTODY_ENTRY_CAPACITY,
        risk_reducing_capacity=SUPPORTED_ACCOUNT_CUSTODY_RISK_REDUCING_CAPACITY,
    )

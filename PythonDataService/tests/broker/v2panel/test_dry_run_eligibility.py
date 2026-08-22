"""Pure-projection tests for the Dry Run deploy verdict (#1702).

``_dry_run_eligibility`` is deliberately narrower than Paper's ``_eligibility``:
per the mode-tiered gate table, Dry Run requires only a registered runtime and
a healthy market-data channel. Account posture, Clerk freeze, Clerk hold,
outstanding intents, and the execution channel are all "not applicable" and
must never appear in this verdict.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import AccountFreezeState, ChannelHealth, ClerkStatus, HoldState
from app.schemas.operator_blocker import AccountOperatorPosture
from app.services.broker_v2_panel.paper_deploy_service import _dry_run_eligibility, _strategy_views
from app.services.strategy_validation_manifest import load_strategy_validation_entries, strategy_registry_seeds
from tests.broker.v2panel.fixtures import ACCT

_NOW = 1_700_000_000_000
_HEALTHY_POSTURE = AccountOperatorPosture(
    condition=None,
    account_desk=None,
    fleet_roster=None,
    status_headline="Account Clerk custody is healthy",
    status_detail=None,
)


def _strategies() -> tuple:
    entry = next(
        entry
        for entry in load_strategy_validation_entries(strategy_registry_seeds())
        if entry.strategy_key == "ema_crossover_signal"
    )
    # Dry Run is never canary-gated (#1730) -- the account id here is
    # arbitrary and does not need canary admission for these tests to
    # exercise real Dry Run eligibility.
    return _strategy_views([entry], account_id=ACCT)


def _clerk_status(
    *,
    market_data_healthy: bool = True,
    hold_active: bool = False,
    freeze_active: bool = False,
    outstanding_intents: int = 0,
) -> ClerkStatus:
    freeze = (
        AccountFreezeState(
            active=True,
            category="ACCOUNT_STATE_UNPROVABLE",
            explanation="Fresh order and exposure truth is unavailable.",
            next_step="Restore broker observation, then reconcile.",
            observed_at_ms=_NOW,
        )
        if freeze_active
        else AccountFreezeState()
    )
    return ClerkStatus(
        broker="alpaca",
        account_id=ACCT,
        hold=HoldState(active=hold_active, reason_code="UNEXPLAINED_ORDER_HOLD" if hold_active else None),
        freeze=freeze,
        outstanding_intents=outstanding_intents,
        observed_at_ms=_NOW,
        channel_healths=[
            ChannelHealth(stream="market_data", healthy=market_data_healthy, observed_at_ms=_NOW),
        ],
        operator_posture=_HEALTHY_POSTURE,
    )


def test_dry_run_eligible_with_runtime_and_healthy_market_data() -> None:
    verdict = _dry_run_eligibility(_strategies(), _clerk_status(), now_ms=_NOW)

    assert verdict.eligible is True
    assert verdict.reason_code == "ALPACA_DRY_RUN_READY"


def test_dry_run_ineligible_with_no_runtime_backed_strategy() -> None:
    verdict = _dry_run_eligibility((), _clerk_status(), now_ms=_NOW)

    assert verdict.eligible is False
    assert verdict.reason_code == "STRATEGY_NOT_ACCEPTED_FOR_DEPLOY"
    assert "strategy validation" in verdict.next_action.lower()


def test_dry_run_ineligible_with_unhealthy_market_data() -> None:
    verdict = _dry_run_eligibility(_strategies(), _clerk_status(market_data_healthy=False), now_ms=_NOW)

    assert verdict.eligible is False
    assert verdict.reason_code == "CLERK_CHANNEL_UNHEALTHY"


def test_dry_run_ignores_clerk_hold_freeze_and_outstanding_intents() -> None:
    """Account posture, freeze, hold, and intent custody are "not applicable"
    to Dry Run per the gate table — none of them may block this verdict."""
    verdict = _dry_run_eligibility(
        _strategies(),
        _clerk_status(hold_active=True, freeze_active=True, outstanding_intents=7),
        now_ms=_NOW,
    )

    assert verdict.eligible is True

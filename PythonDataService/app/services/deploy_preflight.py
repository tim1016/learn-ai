"""Author deploy-context operator blockers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.operator_blocker import NavigateAction, OperatorBlocker, OperatorMove

BrokerDeployConnectionState = Literal[
    "connected",
    "soft_lost",
    "subscriptions_stale",
    "degraded_data_farm",
    "reconnecting",
    "recovering",
    "hard_down",
    "disconnected",
    "disabled",
    "unknown",
]

_BROKER_DOWN_STATES = frozenset({"hard_down", "disconnected", "disabled", "unknown"})
_BROKER_WAIT_STATES = frozenset({"reconnecting", "recovering", "soft_lost"})


class DeployPreflightSignals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daemon_reachable: bool
    broker_connection_state: BrokerDeployConnectionState | None
    account_frozen: bool
    account_proven: bool
    fleet_blocks_starts: bool
    strategy_deployable: bool
    instance_already_running: bool


def _nav(label: str, route: str, fragment: str | None = None) -> OperatorMove:
    return OperatorMove(
        label=label,
        action=NavigateAction(kind="navigate", route=route, fragment=fragment),
    )


def author_deploy_blockers(signals: DeployPreflightSignals) -> list[OperatorBlocker]:
    """Return the deploy blockers standing between the operator and a runnable bot."""

    blockers: list[OperatorBlocker] = []

    if not signals.daemon_reachable:
        blockers.append(
            OperatorBlocker(
                id="daemon_down",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Live engine unavailable",
                detail="Start the engine on this machine, then recheck.",
                primary_move=_nav("Start the engine", "/engine"),
                applies_to="both",
            )
        )

    broker_state = signals.broker_connection_state
    if broker_state is None or broker_state in _BROKER_DOWN_STATES:
        blockers.append(
            OperatorBlocker(
                id="broker_disconnected",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Broker disconnected",
                detail="Connect the IBKR session before deploying.",
                primary_move=_nav("Connect the broker", "/broker"),
                applies_to="both",
            )
        )
    elif broker_state in _BROKER_WAIT_STATES:
        blockers.append(
            OperatorBlocker(
                id="broker_reconnecting",
                severity="blocking",
                disposition="wait",
                headline="Broker reconnecting",
                detail="Waiting for the broker session to reconnect.",
                applies_to="both",
            )
        )
    elif broker_state == "degraded_data_farm":
        blockers.append(
            OperatorBlocker(
                id="broker_data_farm_degraded",
                severity="blocking",
                disposition="wait",
                headline="IBKR data farm degraded",
                detail="Waiting for IBKR market-data evidence to recover.",
                applies_to="both",
            )
        )
    elif broker_state == "subscriptions_stale":
        blockers.append(
            OperatorBlocker(
                id="broker_subscriptions_stale",
                severity="blocking",
                disposition="wait",
                headline="Broker subscriptions stale",
                detail="Waiting for market-data subscriptions to refresh.",
                applies_to="both",
            )
        )

    if signals.account_frozen:
        blockers.append(
            OperatorBlocker(
                id="account_frozen",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Account frozen",
                detail="Resolve the account sick-bay condition before deploying.",
                primary_move=_nav(
                    "Open account monitor",
                    "/broker/account-monitor",
                    "account-reconciliation-action",
                ),
                applies_to="both",
            )
        )
    elif not signals.account_proven:
        blockers.append(
            OperatorBlocker(
                id="account_not_proven",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Account not proven",
                detail="Run account reconcile to prove the account is clean before deploying.",
                primary_move=_nav(
                    "Open account monitor",
                    "/broker/account-monitor",
                    "account-reconciliation-action",
                ),
                applies_to="both",
            )
        )

    if signals.fleet_blocks_starts:
        blockers.append(
            OperatorBlocker(
                id="fleet_contaminated",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Fleet state blocks new deploys",
                detail="Clear the account fleet state before deploying.",
                primary_move=_nav("Open account monitor", "/broker/account-monitor"),
                applies_to="both",
            )
        )

    if not signals.strategy_deployable:
        blockers.append(
            OperatorBlocker(
                id="strategy_not_validated",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Strategy not validated",
                detail="Promote the strategy in Strategy Validation before deploying.",
                primary_move=_nav("Open Strategy Validation", "/strategy-validation"),
                applies_to="deploy",
            )
        )

    if signals.instance_already_running:
        blockers.append(
            OperatorBlocker(
                id="instance_already_running",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Deployment name already running",
                detail="A bot with this name is already live. Go to it, or choose a different name.",
                primary_move=_nav("Go to the running bot", "/broker/bots"),
                applies_to="deploy",
            )
        )

    return blockers

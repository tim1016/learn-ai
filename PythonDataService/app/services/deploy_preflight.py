"""Author deploy-context operator blockers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.ibkr.config import get_settings
from app.broker.runtime_snapshot import BrokerRuntimeSnapshot, snapshot_data_plane_broker
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import read_account_freeze
from app.engine.live.account_observation_lease import assess_account_observation_lease
from app.schemas.operator_blocker import (
    NavigateAction,
    OperatorBlocker,
    OperatorMove,
)
from app.services.account_truth_snapshot import (
    AccountTruthSnapshot,
    assess_account_truth,
    get_account_truth_snapshot_provider,
)
from app.services.fleet_contamination import compute_account_fleet_contamination
from app.services.strategy_validation_manifest import (
    load_strategy_validation_entries,
    strategy_registry_seeds,
)

BrokerDeployConnectionState = Literal[
    "connected",
    "soft_lost",
    "subscriptions_stale",
    "degraded_data_farm",
    "disconnected",
]

_BROKER_DEPLOY_STATES: frozenset[BrokerDeployConnectionState] = frozenset(
    {
        "connected",
        "soft_lost",
        "subscriptions_stale",
        "degraded_data_farm",
        "disconnected",
    }
)
_LIVE_PROCESS_STATES = frozenset({"running", "stopping"})


def _now_ms() -> int:
    """Current wall-clock as int64 ms UTC for account-truth freshness checks."""

    return int(datetime.now(UTC).timestamp() * 1000)


class DeployPreflightSignals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daemon_reachable: bool
    broker_connection_state: BrokerDeployConnectionState | None
    account_frozen: bool
    account_proven: bool
    fleet_blocks_starts: bool
    strategy_deployable: bool
    instance_already_running: bool


def _runtime_connection_state_value(
    snapshot: BrokerRuntimeSnapshot,
) -> BrokerDeployConnectionState | None:
    """Deploy preflight uses the data-plane ``IbkrClient`` state only.

    Broader cockpit states such as ``hard_down``, ``disabled``, or
    ``reconnecting`` are authored by broker-health overlays. They are not
    observable at this boundary, so keeping them out of this type prevents dead
    blocker policy from drifting away from real inputs.
    """

    state = snapshot.connection_state
    return state if state in _BROKER_DEPLOY_STATES else None


def _strategy_is_deployable(strategy_key: str) -> bool:
    entries = load_strategy_validation_entries(strategy_registry_seeds())
    return any(entry.strategy_key == strategy_key and entry.deployable for entry in entries)


async def _instance_is_running_or_stopping(instance_id: str) -> bool:
    settings = get_settings()
    _result, daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
    if daemon is None:
        return False
    for inst in daemon.get("instances", []):
        if inst.get("strategy_instance_id") != instance_id:
            continue
        process = inst.get("process")
        if isinstance(process, dict) and process.get("state") in _LIVE_PROCESS_STATES:
            return True
    return False


def _account_proof_is_current(
    *,
    authority: Literal["account_truth", "observation_lease"],
    artifacts_root: Path,
    account_id: str,
    account_truth: AccountTruthSnapshot | None,
    now_ms: int,
) -> bool:
    if authority == "observation_lease":
        return (
            assess_account_observation_lease(
                artifacts_root,
                account_id,
                now_ms=now_ms,
            ).state
            == "VERIFIED"
        )
    return assess_account_truth(account_truth, now_ms=now_ms).status == "pass"


async def gather_deploy_preflight_signals(
    strategy_key: str,
    account_id: str,
    instance_id: str,
) -> DeployPreflightSignals:
    """Resolve deploy preconditions server-side for the blocker author."""

    settings = get_settings()
    root = Path(settings.live_runs_root)
    artifacts_root = root.parent

    daemon_result, _health = await host_daemon_client.fetch_health(settings.live_runner_daemon_url)
    account_freeze = read_account_freeze(artifacts_root, account_id)
    account_truth = get_account_truth_snapshot_provider().get(account_id)
    now_ms = _now_ms()
    account_proven = _account_proof_is_current(
        authority=settings.account_gate_authority,
        artifacts_root=artifacts_root,
        account_id=account_id,
        account_truth=account_truth,
        now_ms=now_ms,
    )
    fleet = await compute_account_fleet_contamination(root, account_id=account_id)

    return DeployPreflightSignals(
        daemon_reachable=daemon_result.kind == "CONNECTED",
        broker_connection_state=_runtime_connection_state_value(snapshot_data_plane_broker()),
        account_frozen=account_freeze is not None,
        account_proven=account_proven,
        fleet_blocks_starts=fleet.policy_blocks_starts,
        strategy_deployable=_strategy_is_deployable(strategy_key),
        instance_already_running=await _instance_is_running_or_stopping(instance_id),
    )


def _nav(label: str, route: str, fragment: str | None = None) -> OperatorMove:
    return OperatorMove(
        label=label,
        action=NavigateAction(kind="navigate", route=route, fragment=fragment),
    )


def author_fleet_contamination_blocker() -> OperatorBlocker:
    """Author the one account-monitor remedy for a dirty Clerk verdict."""

    return OperatorBlocker.for_host(
        condition_id="fleet_contaminated",
        scope="fleet",
        host="deploy_preflight",
        disposition="fix_elsewhere",
        headline="Fleet state blocks new deploys",
        detail="Clear the account fleet state before deploying or starting a bot.",
        primary_move=_nav("Open account monitor", "/broker/account-monitor"),
        applies_to="both",
    )


def author_deploy_blockers(signals: DeployPreflightSignals) -> list[OperatorBlocker]:
    """Return the deploy blockers standing between the operator and a runnable bot."""

    blockers: list[OperatorBlocker] = []

    if not signals.daemon_reachable:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="daemon_down",
                scope="host",
                host="deploy_preflight",
                disposition="fix_elsewhere",
                headline="Live engine unavailable",
                detail="Start the engine on this machine, then recheck.",
                primary_move=_nav("Start the engine", "/engine"),
                applies_to="both",
            )
        )

    broker_state = signals.broker_connection_state
    if broker_state is None or broker_state == "disconnected":
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="broker_disconnected",
                scope="broker",
                host="deploy_preflight",
                disposition="fix_elsewhere",
                headline="Broker disconnected",
                detail="Connect the IBKR session before deploying or starting this bot.",
                primary_move=_nav("Connect the broker", "/broker"),
                applies_to="both",
            )
        )
    elif broker_state == "soft_lost":
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="broker_soft_lost",
                scope="broker",
                host="deploy_preflight",
                disposition="wait",
                headline="Broker connection temporarily lost",
                detail="Waiting for the broker session to recover.",
                applies_to="both",
            )
        )
    elif broker_state == "degraded_data_farm":
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="broker_data_farm_degraded",
                scope="broker",
                host="deploy_preflight",
                disposition="wait",
                headline="IBKR data farm degraded",
                detail="Waiting for IBKR market-data evidence to recover.",
                applies_to="both",
            )
        )
    elif broker_state == "subscriptions_stale":
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="broker_subscriptions_stale",
                scope="broker",
                host="deploy_preflight",
                disposition="wait",
                headline="Broker subscriptions stale",
                detail="Waiting for market-data subscriptions to refresh.",
                applies_to="both",
            )
        )

    if signals.account_frozen:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="account_frozen",
                scope="account",
                host="deploy_preflight",
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
            OperatorBlocker.for_host(
                condition_id="account_not_proven",
                scope="account",
                host="deploy_preflight",
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
        blockers.append(author_fleet_contamination_blocker())

    if not signals.strategy_deployable:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="strategy_not_validated",
                scope="strategy",
                host="deploy_preflight",
                disposition="fix_elsewhere",
                headline="Strategy not validated",
                detail="Promote the strategy in Strategy Validation before deploying.",
                primary_move=_nav("Open Strategy Validation", "/strategy-validation"),
                applies_to="deploy",
            )
        )

    if signals.instance_already_running:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="instance_already_running",
                scope="bot",
                host="deploy_preflight",
                disposition="fix_elsewhere",
                headline="Deployment name already running",
                detail="A bot with this name is already live. Go to it, or choose a different name.",
                primary_move=_nav("Go to the running bot", "/broker/bots"),
                applies_to="deploy",
            )
        )

    return blockers

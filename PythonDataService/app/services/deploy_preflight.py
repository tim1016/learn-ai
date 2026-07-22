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
    SURFACE_ANCHOR,
    NavigateAction,
    OperatorBlocker,
    OperatorMove,
)
from app.services.account_truth_snapshot import (
    AccountTruthSnapshot,
    assess_account_truth,
    get_account_truth_snapshot_provider,
)
from app.services.daily_session_schedule import start_boundary_verdict
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
    daemon_code_current: bool
    broker_connection_state: BrokerDeployConnectionState | None
    account_frozen: bool
    account_proven: bool
    fleet_blocks_starts: bool
    strategy_deployable: bool
    instance_already_running: bool
    session_in_start_window: bool


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
    _result, process = await host_daemon_client.fetch_instance_process(
        settings.live_runner_daemon_url,
        instance_id,
    )
    return isinstance(process, dict) and process.get("state") in _LIVE_PROCESS_STATES


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
    live_config: dict | None = None,
) -> DeployPreflightSignals:
    """Resolve deploy preconditions server-side for the blocker author."""

    settings = get_settings()
    root = Path(settings.live_runs_root)
    artifacts_root = root.parent

    daemon_result, health = await host_daemon_client.fetch_startability_health(
        settings.live_runner_daemon_url
    )
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
        daemon_code_current=health is not None and not health.code_stale,
        broker_connection_state=_runtime_connection_state_value(snapshot_data_plane_broker()),
        account_frozen=account_freeze is not None,
        account_proven=account_proven,
        fleet_blocks_starts=fleet.policy_blocks_starts,
        strategy_deployable=_strategy_is_deployable(strategy_key),
        instance_already_running=await _instance_is_running_or_stopping(instance_id),
        session_in_start_window=start_boundary_verdict(now_ms, live_config).allowed,
    )


def _nav(label: str, route: str, fragment: str | None = None) -> OperatorMove:
    return OperatorMove(
        label=label,
        action=NavigateAction(kind="navigate", route=route, fragment=fragment),
    )


def author_fleet_contamination_blocker(account_id: str | None = None) -> OperatorBlocker:
    """Author the one Accounts route for a dirty Clerk verdict."""

    return OperatorBlocker.for_host(
        condition_id="fleet_contaminated",
        scope="fleet",
        host="deploy_preflight",
        anchor=SURFACE_ANCHOR,
        audience="operator",
        disposition="fix_elsewhere",
        headline="Fleet state blocks new deploys",
        detail="Clear the account fleet state before deploying or starting a bot.",
        primary_move=_nav(
            "Open account recovery",
            _account_recovery_route(account_id),
            "account-desk-recovery-controls" if account_id is not None else None,
        ),
        applies_to="both",
    )


def author_deploy_blockers(
    signals: DeployPreflightSignals,
    *,
    account_id: str | None = None,
) -> list[OperatorBlocker]:
    """Return the deploy blockers standing between the operator and a runnable bot."""

    blockers: list[OperatorBlocker] = []

    if not signals.daemon_reachable:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="daemon_down",
                scope="host",
                host="deploy_preflight",
                anchor=SURFACE_ANCHOR,
                audience="operator",
                disposition="fix_elsewhere",
                headline="Live engine unavailable",
                detail="Start the engine on this machine, then recheck.",
                primary_move=_nav("Start the engine", "/engine"),
                applies_to="both",
            )
        )
    elif not signals.daemon_code_current:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="daemon_code_stale",
                scope="host",
                host="deploy_preflight",
                anchor=SURFACE_ANCHOR,
                audience="operator",
                disposition="fix_elsewhere",
                headline="Live engine restart required",
                detail="The running engine does not contain the current deployed code. Restart it, then recheck.",
                primary_move=_nav("Open live engine recovery", "/engine"),
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
                disposition="fix_elsewhere",
                headline="Account frozen",
                detail="Resolve the account sick-bay condition before deploying.",
                primary_move=_nav(
                    "Open account recovery",
                    _account_recovery_route(account_id),
                    "account-desk-recovery-controls" if account_id is not None else None,
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
                disposition="fix_elsewhere",
                headline="Account not proven",
                detail="Run account reconcile to prove the account is clean before deploying.",
                primary_move=_nav(
                    "Open account proof",
                    _account_recovery_route(account_id),
                    "account-desk-operations-proof" if account_id is not None else None,
                ),
                applies_to="both",
            )
        )

    if signals.fleet_blocks_starts:
        blockers.append(author_fleet_contamination_blocker(account_id))

    if not signals.strategy_deployable:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="strategy_not_validated",
                scope="strategy",
                host="deploy_preflight",
                anchor=SURFACE_ANCHOR,
                audience="operator",
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
                anchor=SURFACE_ANCHOR,
                audience="operator",
                disposition="fix_elsewhere",
                headline="Deployment name already running",
                detail="A bot with this name is already live. Go to it, or choose a different name.",
                primary_move=_nav("Go to the running bot", "/broker/bots"),
                applies_to="deploy",
            )
        )

    if not signals.session_in_start_window:
        blockers.append(
            OperatorBlocker.for_host(
                condition_id="session_start_window_closed",
                scope="bot",
                host="deploy_preflight",
                anchor=SURFACE_ANCHOR,
                audience="operator",
                disposition="wait",
                headline="Outside the session start window",
                detail="The session stop has passed. Deploy the bot now and it will start at the next session open.",
                applies_to="run",
            )
        )

    return blockers


def _account_recovery_route(account_id: str | None) -> str:
    return "/broker/accounts" if account_id is None else f"/broker/accounts/{account_id}"

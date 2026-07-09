"""Service helpers for account/fleet contamination projections."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.broker.ibkr.config import IbkrSettings
from app.engine.live.fleet import compute_fleet_contamination
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.schemas.live_runs import FleetContamination, InstanceBrokerView

logger = logging.getLogger(__name__)


def scan_runs_by_instance(root: Path) -> dict[str, list[dict]]:
    """Group run dirs by ``strategy_instance_id`` from their ledgers, newest first."""

    out: dict[str, list[dict]] = {}
    if not root.is_dir():
        return out
    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            ledger = json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = ledger.get("strategy_instance_id") or ""
        if not sid:
            continue
        out.setdefault(sid, []).append(
            {
                "run_id": str(ledger.get("run_id") or run_dir.name),
                "run_dir": str(run_dir),
                "created_at_ms": ledger.get("created_at_ms") or 0,
            }
        )
    for runs in out.values():
        runs.sort(key=lambda r: r["created_at_ms"], reverse=True)
    return out


def read_instance_live_state(root: Path, sid: str) -> LiveStateEnvelope | None:
    artifacts_root = root.parent
    try:
        sidecar_path = stable_live_state_path(artifacts_root, sid)
    except ValueError:
        return None
    try:
        return LiveStateSidecarRepo(
            sidecar_path, trusted_root=artifacts_root / "live_state"
        ).read()
    except (LiveStateSidecarCorruptError, OSError):
        return None


def instance_broker(root: Path, sid: str) -> InstanceBrokerView | None:
    """Read an instance's namespace-attributed broker slice from live state."""

    envelope = read_instance_live_state(root, sid)
    if envelope is None:
        return None
    return InstanceBrokerView(
        bot_order_namespace=envelope.bot_order_namespace,
        owned_positions=dict(envelope.expected_position_by_symbol),
        pending_order_count=len(envelope.pending_intents),
    )


def collect_fleet_position_explanations(root: Path) -> dict[str, dict[str, int]]:
    explained: dict[str, dict[str, int]] = {}
    for sid in scan_runs_by_instance(root):
        broker = instance_broker(root, sid)
        if broker is not None and broker.owned_positions:
            explained[sid] = broker.owned_positions
    return explained


async def fetch_net_positions() -> dict[str, int] | None:
    """Best-effort net account position by symbol from the broker."""

    try:
        from app.broker.ibkr import account as ibkr_account
        from app.routers.broker_dependencies import require_connected_client

        client = require_connected_client()
        snapshot = await ibkr_account.fetch_positions(client)
    except Exception as exc:
        logger.info("fleet net-position fetch unavailable: %s", exc)
        return None
    net: dict[str, int] = {}
    for pos in snapshot.positions:
        symbol = str(pos.symbol).upper()
        net[symbol] = net.get(symbol, 0) + int(pos.quantity)
    return net


async def compute_account_fleet_contamination(
    settings: IbkrSettings,
    root: Path,
) -> FleetContamination:
    result = compute_fleet_contamination(
        await fetch_net_positions(),
        collect_fleet_position_explanations(root),
        policy_blocks_starts=settings.fleet_dirty_blocks_starts,
    )
    return FleetContamination(**result)

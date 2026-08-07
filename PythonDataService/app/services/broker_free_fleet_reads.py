"""Account-level fleet projection over durable run identity and broker truth."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.schemas.live_runs import FleetAccountSummary
from app.services.account_fleet_read_context import compose_fleet_account_read_summary
from app.services.account_truth_snapshot import AccountTruthSnapshotProvider

type RunRecord = dict[str, object]
type VisibleRuns = dict[str, list[RunRecord]]


@dataclass(frozen=True, slots=True)
class BrokerFreeFleetReadDependencies:
    """Typed source boundary for the retained account-summary projection."""

    visible_runs_by_instance: Callable[[Path], VisibleRuns]
    run_dir_account_id: Callable[[Path], str | None]
    get_account_truth_snapshot_provider: Callable[[], AccountTruthSnapshotProvider]
    now_ms: Callable[[], int]
    fetch_broker_connected_account: Callable[[], Awaitable[tuple[str | None, bool]]]


class BrokerFreeFleetReadService:
    """Build the account summary without triggering a broker refresh."""

    def __init__(self, dependencies: BrokerFreeFleetReadDependencies) -> None:
        self._d = dependencies

    async def account_summary(
        self,
        root: Path,
        *,
        requested_account_id: str | None,
    ) -> FleetAccountSummary:
        """Project account identity and contamination from cached broker truth."""

        by_instance = await asyncio.to_thread(self._d.visible_runs_by_instance, root)
        instance_account_ids = {
            sid: self._account_id_for_sid(by_instance, sid) for sid in by_instance
        }
        if requested_account_id is not None:
            instance_account_ids = {
                sid: account_id
                for sid, account_id in instance_account_ids.items()
                if account_id == requested_account_id
            }
        broker_account, broker_known = await self._d.fetch_broker_connected_account()
        return await asyncio.to_thread(
            compose_fleet_account_read_summary,
            root,
            requested_account_id=requested_account_id,
            instance_account_ids=instance_account_ids,
            broker_connected_account=broker_account,
            broker_account_known=broker_known,
            snapshot_provider=self._d.get_account_truth_snapshot_provider(),
            observed_at_ms=self._d.now_ms(),
        )

    def _account_id_for_sid(self, by_instance: VisibleRuns, sid: str) -> str | None:
        runs = by_instance.get(sid, [])
        if not runs:
            return None
        return self._d.run_dir_account_id(Path(str(runs[0]["run_dir"])))

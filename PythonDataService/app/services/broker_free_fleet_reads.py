"""Catalog and roll-call projections over one broker-free fleet read."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.engine.live.bot_lifecycle_state import BotLifecyclePhase, BotLifecycleStateRecord
from app.schemas.live_runs import BotCatalogResponse, BotCatalogRow, BotRollCallResponse, LiveInstanceStatus
from app.services.account_fleet_read_context import (
    AccountFleetReadContext,
    AccountFleetReadContexts,
    build_account_fleet_read_contexts,
)
from app.services.account_truth_snapshot import AccountTruthSnapshotProvider
from app.services.bot_catalog_projection import (
    TradingMode,
    compose_bot_catalog_row,
    trading_mode_from_configured_mode,
)
from app.services.bot_roll_call import (
    attendance_for_instance,
    ensure_roll_call_offer,
    evening_report_from_rows,
    roll_call_offer_schema,
    roll_call_summary_from_rows,
)
from app.services.daily_session_schedule import start_boundary_verdict

type RunRecord = dict[str, object]
type VisibleRuns = dict[str, list[RunRecord]]
type DaemonPayload = dict[str, object]


class FleetReadSettings(Protocol):
    """The settings required to project a fleet read."""

    live_runner_daemon_url: str
    mode: object


class FleetStatusResolver(Protocol):
    """Compose one status from an already-observed daemon process."""

    def __call__(
        self,
        sid: str,
        root: Path,
        settings: FleetReadSettings,
        daemon_process: DaemonPayload | None,
        *,
        runs_by_instance: VisibleRuns | None = None,
        account_fleet_read_context: AccountFleetReadContext | None = None,
    ) -> Awaitable[LiveInstanceStatus]: ...


@dataclass(frozen=True, slots=True)
class BrokerFreeFleetReadDependencies:
    """Typed source boundary for one broker-free fleet read."""

    visible_runs_by_instance: Callable[[Path], VisibleRuns]
    sid_has_soft_deletion: Callable[[Path, str], bool]
    resolve_bot_lifecycle_state: Callable[[Path, str], BotLifecycleStateRecord | None]
    run_dir_account_id: Callable[[Path], str | None]
    fetch_instances: Callable[[str], Awaitable[tuple[object, DaemonPayload | None]]]
    fetch_instance_process: Callable[[str, str], Awaitable[tuple[object, DaemonPayload | None]]]
    daemon_process_from_instance: Callable[[DaemonPayload | None], DaemonPayload | None]
    resolve_status_from_process: FleetStatusResolver
    get_account_truth_snapshot_provider: Callable[[], AccountTruthSnapshotProvider]
    now_ms: Callable[[], int]
    read_sidecar: Callable[[Path], object | None]
    live_config_for_run_dir: Callable[[Path], Mapping[str, object] | None]
    status_is_roll_call_eligible: Callable[[LiveInstanceStatus], bool]


@dataclass(frozen=True, slots=True)
class FleetReadSnapshot:
    """One request's durable and daemon observations, captured once."""

    root: Path
    settings: FleetReadSettings
    by_instance: VisibleRuns
    daemon_by_sid: dict[str, DaemonPayload]
    sids: tuple[str, ...]
    observed_at_ms: int
    account_contexts: AccountFleetReadContexts


class BrokerFreeFleetReadService:
    """Build read-side fleet surfaces without triggering IBKR calls."""

    def __init__(self, dependencies: BrokerFreeFleetReadDependencies) -> None:
        self._d = dependencies

    async def catalog(self, settings: FleetReadSettings, root: Path) -> BotCatalogResponse:
        snapshot = await self._read_snapshot(settings, root)
        sids = [
            sid
            for sid in snapshot.sids
            if sid in snapshot.by_instance or not self._d.sid_has_soft_deletion(root.parent, sid)
        ]
        trading_mode = trading_mode_from_configured_mode(settings.mode)
        rows = list(
            await asyncio.gather(
                *(self._catalog_row(sid, snapshot, trading_mode) for sid in sids)
            )
        )
        rows.sort(key=lambda row: (row.created_at_ms or row.last_run_at_ms or 0, row.name), reverse=True)
        return BotCatalogResponse(
            bots=rows,
            roll_call=roll_call_summary_from_rows(rows, now_ms=snapshot.observed_at_ms),
            evening_report=evening_report_from_rows(rows, now_ms=snapshot.observed_at_ms),
        )

    async def roll_call(self, settings: FleetReadSettings, root: Path) -> BotRollCallResponse:
        snapshot = await self._read_snapshot(settings, root)
        candidate_sids: list[str] = []
        retired_count = 0
        for sid in snapshot.sids:
            lifecycle_state = self._d.resolve_bot_lifecycle_state(root, sid)
            if lifecycle_state is not None and lifecycle_state.phase == BotLifecyclePhase.RETIRED:
                retired_count += 1
                continue
            candidate_sids.append(sid)

        trading_mode = trading_mode_from_configured_mode(settings.mode)
        status_views = await asyncio.gather(
            *(self._resolve_status(sid, snapshot) for sid in candidate_sids)
        )
        rows: list[BotCatalogRow] = []
        offers = []
        summary_session_date: str | None = None
        summary_effective_stop_ms: int | None = None
        for sid, status_view in zip(candidate_sids, status_views, strict=True):
            rows.append(compose_bot_catalog_row(status_view, trading_mode))
            if not self._d.status_is_roll_call_eligible(status_view):
                continue
            runs = snapshot.by_instance.get(sid, [])
            if not runs:
                continue
            run_dir = Path(str(runs[0]["run_dir"]))
            if self._d.run_dir_account_id(run_dir) is None:
                continue
            boundary = start_boundary_verdict(
                snapshot.observed_at_ms,
                self._d.live_config_for_run_dir(run_dir),
            )
            if not boundary.allowed or boundary.effective_stop_ms is None or boundary.session_date is None:
                continue
            offer = ensure_roll_call_offer(
                root,
                sid=sid,
                run_id=(
                    status_view.operator_surface.host_process.start_capability.run_id
                    or str(runs[0]["run_id"])
                ),
                session_date=boundary.session_date,
                issued_at_ms=snapshot.observed_at_ms,
                expires_at_ms=boundary.effective_stop_ms,
                evidence_snapshot={
                    "readiness_verdict": (
                        status_view.readiness.verdict if status_view.readiness is not None else None
                    ),
                    "process_state": status_view.process.state,
                    "display_status": status_view.daily_lifecycle.display_status,
                },
            )
            offers.append(roll_call_offer_schema(offer))
            summary_session_date = summary_session_date or boundary.session_date
            summary_effective_stop_ms = (
                boundary.effective_stop_ms
                if summary_effective_stop_ms is None
                else min(summary_effective_stop_ms, boundary.effective_stop_ms)
            )

        return BotRollCallResponse(
            summary=roll_call_summary_from_rows(rows, now_ms=snapshot.observed_at_ms).model_copy(
                update={
                    "ready": len(offers),
                    "retired": retired_count,
                    "session_date": summary_session_date,
                    "effective_stop_ms": summary_effective_stop_ms,
                }
            ),
            offers=offers,
        )

    async def _read_snapshot(self, settings: FleetReadSettings, root: Path) -> FleetReadSnapshot:
        by_instance = await asyncio.to_thread(self._d.visible_runs_by_instance, root)
        _result, daemon = await self._d.fetch_instances(settings.live_runner_daemon_url)
        daemon_by_sid = {
            sid: instance
            for instance in (daemon or {}).get("instances", [])
            if isinstance(instance, dict)
            and isinstance((sid := instance.get("strategy_instance_id")), str)
            and sid
        }
        sids = tuple(sorted(set(by_instance) | set(daemon_by_sid)))
        observed_at_ms = self._d.now_ms()
        account_ids = [self._account_id_for_sid(by_instance, sid) for sid in sids]
        account_contexts = await asyncio.to_thread(
            build_account_fleet_read_contexts,
            root,
            account_ids,
            snapshot_provider=self._d.get_account_truth_snapshot_provider(),
            observed_at_ms=observed_at_ms,
        )
        return FleetReadSnapshot(
            root=root,
            settings=settings,
            by_instance=by_instance,
            daemon_by_sid=daemon_by_sid,
            sids=sids,
            observed_at_ms=observed_at_ms,
            account_contexts=account_contexts,
        )

    def _account_id_for_sid(self, by_instance: VisibleRuns, sid: str) -> str | None:
        runs = by_instance.get(sid, [])
        if not runs:
            return None
        return self._d.run_dir_account_id(Path(str(runs[0]["run_dir"])))

    async def _catalog_row(
        self,
        sid: str,
        snapshot: FleetReadSnapshot,
        trading_mode: TradingMode,
    ) -> BotCatalogRow:
        status_view = await self._resolve_status(sid, snapshot)
        row = compose_bot_catalog_row(status_view, trading_mode)
        return row.model_copy(
            update={
                "attendance": attendance_for_instance(
                    runs=snapshot.by_instance.get(sid, []),
                    lifecycle_state=self._d.resolve_bot_lifecycle_state(snapshot.root, sid),
                    read_sidecar=self._d.read_sidecar,
                )
            }
        )

    async def _resolve_status(self, sid: str, snapshot: FleetReadSnapshot) -> LiveInstanceStatus:
        daemon_process = self._d.daemon_process_from_instance(snapshot.daemon_by_sid.get(sid))
        if daemon_process is None:
            _result, daemon_process = await self._d.fetch_instance_process(
                snapshot.settings.live_runner_daemon_url,
                sid,
            )
        return await self._d.resolve_status_from_process(
            sid,
            snapshot.root,
            snapshot.settings,
            daemon_process,
            runs_by_instance=snapshot.by_instance,
            account_fleet_read_context=snapshot.account_contexts.get(
                self._account_id_for_sid(snapshot.by_instance, sid)
            ),
        )

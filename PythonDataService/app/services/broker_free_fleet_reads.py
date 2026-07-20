"""Catalog and roll-call orchestration over request-scoped account contexts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.engine.live.bot_lifecycle_state import BotLifecyclePhase
from app.schemas.live_runs import BotCatalogResponse, BotCatalogRow, BotRollCallResponse, LiveInstanceStatus
from app.services.account_fleet_read_context import (
    AccountFleetReadContext,
    AccountFleetReadContexts,
    build_account_fleet_read_contexts,
)
from app.services.bot_catalog_projection import compose_bot_catalog_row, trading_mode_from_configured_mode
from app.services.bot_roll_call import (
    attendance_for_instance,
    ensure_roll_call_offer,
    evening_report_from_rows,
    roll_call_offer_schema,
    roll_call_summary_from_rows,
)
from app.services.daily_session_schedule import start_boundary_verdict


@dataclass(frozen=True, slots=True)
class BrokerFreeFleetReadDependencies:
    """Router-owned readers needed to build fleet read projections."""

    visible_runs_by_instance: Callable[..., dict[str, list[dict]]]
    sid_has_soft_deletion: Callable[..., bool]
    resolve_bot_lifecycle_state: Callable[..., Any]
    run_dir_account_id: Callable[[Path], str | None]
    fetch_instances: Callable[..., Any]
    fetch_instance_process: Callable[..., Any]
    daemon_process_from_instance: Callable[[dict | None], dict | None]
    resolve_status_from_process: Callable[..., Any]
    get_account_truth_snapshot_provider: Callable[..., Any]
    now_ms: Callable[[], int]
    read_sidecar: Callable[..., Any]
    live_config_for_run_dir: Callable[[Path], Any]
    status_is_roll_call_eligible: Callable[[LiveInstanceStatus], bool]


class BrokerFreeFleetReadService:
    """Build read-side fleet surfaces without triggering IBKR calls."""

    def __init__(self, dependencies: BrokerFreeFleetReadDependencies) -> None:
        self._d = dependencies

    async def catalog(self, settings: Any, root: Path) -> BotCatalogResponse:
        by_instance = await asyncio.to_thread(self._d.visible_runs_by_instance, root)
        daemon_by_sid = await self._daemon_by_sid(settings)
        sids = [
            sid
            for sid in sorted(set(by_instance) | set(daemon_by_sid))
            if sid in by_instance or not self._d.sid_has_soft_deletion(root.parent, sid)
        ]
        observed_at_ms = self._d.now_ms()
        contexts = await self._contexts(root, by_instance, sids, observed_at_ms)
        trading_mode = trading_mode_from_configured_mode(getattr(settings, "mode", None))
        rows = list(
            await asyncio.gather(
                *(
                    self._catalog_row(
                        sid,
                        root,
                        settings,
                        daemon_by_sid.get(sid),
                        by_instance,
                        trading_mode,
                        contexts.get(self._account_id_for_sid(by_instance, sid)),
                    )
                    for sid in sids
                )
            )
        )
        rows.sort(key=lambda row: (row.created_at_ms or row.last_run_at_ms or 0, row.name), reverse=True)
        return BotCatalogResponse(
            bots=rows,
            roll_call=roll_call_summary_from_rows(rows, now_ms=observed_at_ms),
            evening_report=evening_report_from_rows(rows, now_ms=observed_at_ms),
        )

    async def roll_call(self, settings: Any, root: Path) -> BotRollCallResponse:
        by_instance = await asyncio.to_thread(self._d.visible_runs_by_instance, root)
        daemon_by_sid = await self._daemon_by_sid(settings)
        candidate_sids: list[str] = []
        retired_count = 0
        for sid in sorted(set(by_instance) | set(daemon_by_sid)):
            lifecycle_state = self._d.resolve_bot_lifecycle_state(root, sid)
            if lifecycle_state is not None and lifecycle_state.phase == BotLifecyclePhase.RETIRED:
                retired_count += 1
                continue
            candidate_sids.append(sid)

        now_ms = self._d.now_ms()
        contexts = await self._contexts(root, by_instance, candidate_sids, now_ms)
        trading_mode = trading_mode_from_configured_mode(getattr(settings, "mode", None))
        status_views = await asyncio.gather(
            *(
                self._resolve_status(
                    sid,
                    root,
                    settings,
                    daemon_by_sid.get(sid),
                    by_instance,
                    contexts.get(self._account_id_for_sid(by_instance, sid)),
                )
                for sid in candidate_sids
            )
        )
        rows: list[BotCatalogRow] = []
        offers = []
        summary_session_date: str | None = None
        summary_effective_stop_ms: int | None = None
        for sid, status_view in zip(candidate_sids, status_views, strict=True):
            rows.append(compose_bot_catalog_row(status_view, trading_mode))
            if not self._d.status_is_roll_call_eligible(status_view):
                continue
            runs = by_instance.get(sid, [])
            if not runs:
                continue
            run_dir = Path(runs[0]["run_dir"])
            if self._d.run_dir_account_id(run_dir) is None:
                continue
            boundary = start_boundary_verdict(now_ms, self._d.live_config_for_run_dir(run_dir))
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
                issued_at_ms=now_ms,
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
            summary=roll_call_summary_from_rows(rows, now_ms=now_ms).model_copy(
                update={
                    "ready": len(offers),
                    "retired": retired_count,
                    "session_date": summary_session_date,
                    "effective_stop_ms": summary_effective_stop_ms,
                }
            ),
            offers=offers,
        )

    async def _daemon_by_sid(self, settings: Any) -> dict[str, dict]:
        _result, daemon = await self._d.fetch_instances(settings.live_runner_daemon_url)
        if daemon is None:
            return {}
        return {
            sid: instance
            for instance in daemon.get("instances", [])
            if isinstance((sid := instance.get("strategy_instance_id")), str) and sid
        }

    async def _contexts(
        self,
        root: Path,
        by_instance: dict[str, list[dict]],
        sids: list[str],
        observed_at_ms: int,
    ) -> AccountFleetReadContexts:
        account_ids = [self._account_id_for_sid(by_instance, sid) for sid in sids]
        return await asyncio.to_thread(
            build_account_fleet_read_contexts,
            root,
            account_ids,
            snapshot_provider=self._d.get_account_truth_snapshot_provider(),
            observed_at_ms=observed_at_ms,
        )

    def _account_id_for_sid(self, by_instance: dict[str, list[dict]], sid: str) -> str | None:
        runs = by_instance.get(sid, [])
        if not runs:
            return None
        return self._d.run_dir_account_id(Path(runs[0]["run_dir"]))

    async def _catalog_row(
        self,
        sid: str,
        root: Path,
        settings: Any,
        daemon_instance: dict | None,
        by_instance: dict[str, list[dict]],
        trading_mode: Any,
        account_context: AccountFleetReadContext | None,
    ) -> BotCatalogRow:
        status_view = await self._resolve_status(
            sid,
            root,
            settings,
            daemon_instance,
            by_instance,
            account_context,
        )
        row = compose_bot_catalog_row(status_view, trading_mode)
        return row.model_copy(
            update={
                "attendance": attendance_for_instance(
                    runs=by_instance.get(sid, []),
                    lifecycle_state=self._d.resolve_bot_lifecycle_state(root, sid),
                    read_sidecar=self._d.read_sidecar,
                )
            }
        )

    async def _resolve_status(
        self,
        sid: str,
        root: Path,
        settings: Any,
        daemon_instance: dict | None,
        by_instance: dict[str, list[dict]],
        account_context: AccountFleetReadContext | None,
    ) -> LiveInstanceStatus:
        daemon_process = self._d.daemon_process_from_instance(daemon_instance)
        if daemon_process is None:
            _result, daemon_process = await self._d.fetch_instance_process(
                settings.live_runner_daemon_url,
                sid,
            )
        return await self._d.resolve_status_from_process(
            sid,
            root,
            settings,
            daemon_process,
            runs_by_instance=by_instance,
            account_fleet_read_context=account_context,
        )

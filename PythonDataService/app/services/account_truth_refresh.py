"""Account Truth refresh boundary with snapshot-cache side effects."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from app.broker.ibkr.account_truth import (
    AccountTruthCollectionContext,
    build_account_truth_collection_context,
    fetch_account_truth,
)
from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor, get_monitor
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.health import build_broker_health
from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.account_truth import AccountTruthResponse
from app.services.account_truth_snapshot import (
    DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS,
    AccountTruthSnapshotProvider,
    get_account_truth_snapshot_provider,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS = 15_000


class AccountTruthRefreshCallable(Protocol):
    async def __call__(
        self,
        client: IbkrClient,
        *,
        health: IbkrConnectionHealth,
        collection_context: AccountTruthCollectionContext,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
    ) -> AccountTruthResponse: ...


class AccountTruthCollectionContextBuilder(Protocol):
    def __call__(
        self,
        *,
        artifacts_root: Path,
        account_id: str | None,
        context: str,
    ) -> AccountTruthCollectionContext: ...


class BrokerHealthBuilder(Protocol):
    def __call__(
        self,
        client: IbkrClient,
        monitor: AutoReconnectMonitor | None,
    ) -> IbkrConnectionHealth: ...


class MonitorProvider(Protocol):
    def __call__(self) -> AutoReconnectMonitor | None: ...


def validate_account_truth_refresh_cadence(
    interval_ms: int,
    *,
    hard_ttl_ms: int = DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS,
) -> None:
    """Assert refresh cadence has real margin under the readiness TTL."""

    if interval_ms <= 0:
        raise ValueError("Account Truth refresh interval must be positive.")
    if hard_ttl_ms <= 0:
        raise ValueError("Account Truth readiness TTL must be positive.")
    if interval_ms * 2 >= hard_ttl_ms:
        raise ValueError(
            "Account Truth refresh interval must be less than half the readiness TTL "
            f"(interval_ms={interval_ms}, hard_ttl_ms={hard_ttl_ms})."
        )


validate_account_truth_refresh_cadence(DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS)


async def refresh_account_truth_and_update_cache(
    client: IbkrClient,
    *,
    health: IbkrConnectionHealth,
    collection_context: AccountTruthCollectionContext,
    snapshot_provider: AccountTruthSnapshotProvider | None = None,
) -> AccountTruthResponse:
    """Fetch Account Truth and keep the readiness cache in sync with the attempt."""

    provider = snapshot_provider or get_account_truth_snapshot_provider()
    try:
        truth = await fetch_account_truth(
            client,
            health=health,
            collection_context=collection_context,
        )
    except BrokerError as exc:
        provider.mark_refresh_failed(
            health.account_id,
            detail=str(exc),
        )
        raise
    provider.remember(truth)
    return truth


class AccountTruthRefreshLoop:
    """Account-scoped background refresh loop for the Account Truth cache."""

    def __init__(
        self,
        *,
        client: IbkrClient,
        artifacts_root: Path,
        interval_ms: int = DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
        refresh: AccountTruthRefreshCallable = refresh_account_truth_and_update_cache,
        health_builder: BrokerHealthBuilder = build_broker_health,
        monitor_provider: MonitorProvider = get_monitor,
        collection_context_builder: AccountTruthCollectionContextBuilder = build_account_truth_collection_context,
    ) -> None:
        validate_account_truth_refresh_cadence(interval_ms)
        self._client = client
        self._artifacts_root = artifacts_root
        self._interval_ms = interval_ms
        self._snapshot_provider = snapshot_provider or get_account_truth_snapshot_provider()
        self._refresh = refresh
        self._health_builder = health_builder
        self._monitor_provider = monitor_provider
        self._collection_context_builder = collection_context_builder
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._last_account_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the singleton background task. Idempotent."""

        if self.is_running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="account-truth-refresh-loop",
        )

    async def stop(self) -> None:
        """Cancel the background task and wait briefly for shutdown."""

        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.CancelledError, TimeoutError):
            logger.debug("account truth refresh loop stopped")
        finally:
            self._task = None

    async def refresh_once(self) -> AccountTruthResponse | None:
        """Perform one account-scoped refresh attempt."""

        async with self._refresh_lock:
            health = self._health_builder(self._client, self._monitor_provider())
            account_id = health.account_id or self._last_account_id
            if account_id is not None:
                self._last_account_id = account_id
            if health.account_id is None or not health.connected or health.connection_state != "connected":
                self._mark_refresh_unavailable(
                    account_id,
                    detail=(
                        "Account Truth refresh requires a connected broker session; "
                        f"current broker state is {health.connection_state}."
                    ),
                    attempted_at_ms=health.fetched_at_ms,
                )
                return None

            collection_context = self._collection_context_builder(
                artifacts_root=self._artifacts_root,
                account_id=health.account_id,
                context="account truth refresh loop",
            )
            try:
                return await self._refresh(
                    self._client,
                    health=health,
                    collection_context=collection_context,
                    snapshot_provider=self._snapshot_provider,
                )
            except BrokerError as exc:
                logger.warning(
                    "account truth refresh failed",
                    extra={"account_id": health.account_id, "exception": repr(exc)},
                )
                return None
            except Exception as exc:
                self._mark_refresh_unavailable(
                    health.account_id,
                    detail=f"Account Truth refresh failed unexpectedly: {exc}",
                )
                logger.exception(
                    "account truth refresh failed unexpectedly",
                    extra={"account_id": health.account_id},
                )
                return None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            await self.refresh_once()
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._interval_ms / 1000,
                )
            except TimeoutError:
                continue

    def _mark_refresh_unavailable(
        self,
        account_id: str | None,
        *,
        detail: str,
        attempted_at_ms: int | None = None,
    ) -> None:
        if account_id is None:
            return
        self._snapshot_provider.mark_refresh_failed(
            account_id,
            detail=detail,
            attempted_at_ms=attempted_at_ms if attempted_at_ms is not None else now_ms_utc(),
        )


__all__ = [
    "DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS",
    "AccountTruthRefreshLoop",
    "refresh_account_truth_and_update_cache",
    "validate_account_truth_refresh_cadence",
]
